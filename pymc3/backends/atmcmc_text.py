"""
Text file trace backend modified from pymc3 to work efficiently with
ATMCMC

Store sampling values as CSV files.

File format
-----------

Sampling values for each chain are saved in a separate file (under a
directory specified by the `name` argument).  The rows correspond to
sampling iterations.  The column names consist of variable names and
index labels.  For example, the heading

  x,y__0_0,y__0_1,y__1_0,y__1_1,y__2_0,y__2_1

represents two variables, x and y, where x is a scalar and y has a
shape of (3, 2).
"""
from glob import glob

import cPickle as pickle
import os
import pandas as pd

import pymc3 as pm
from ..model import modelcontext
from ..backends import base, ndarray
from . import tracetab as ttab
from ..blocking import DictToArrayBijection, ArrayOrdering,\
                       ListArrayOrdering, ListToArrayBijection
from ..step_methods.arraystep import BlockedStep

import Queue
import multiprocessing
import traceback
import errno


def worker(q_in, q_out, function, eprintignore, pshared):
    kwargs = {}
    if pshared is not None:
        kwargs['pshared'] = pshared

    while True:
        i, args = q_in.get()
        if i is None:
            break

        r, e = None, None
        try:
            r = function(*args, **kwargs)
        except Exception as e:
            if eprintignore is not None and not isinstance(e, eprintignore):
                traceback.print_exc()

        q_out.put((i, r, e))


def parimap(function, *iterables, **kwargs):
    """
    More efficient Queueing compared to Pool in multiprocessing.
    Adapted from the pyrocko package. http://www.pyrocko.org
    """
    assert all(
        k in ('nprocs', 'eprintignore', 'pshared') for k in kwargs.keys())

    nprocs = kwargs.get('nprocs', None)
    eprintignore = kwargs.get('eprintignore', 'all')
    pshared = kwargs.get('pshared', None)

    if eprintignore == 'all':
        eprintignore = None

    if nprocs == 1:
        iterables = map(iter, iterables)
        kwargs = {}
        if pshared is not None:
            kwargs['pshared'] = pshared

        while True:
            args = [next(it) for it in iterables]
            yield function(*args, **kwargs)

        return

    if nprocs is None:
        nprocs = multiprocessing.cpu_count()

    q_in = multiprocessing.Queue(1)
    q_out = multiprocessing.Queue()

    procs = []

    results = []
    nrun = 0
    nwritten = 0
    iout = 0
    all_written = False
    error_ahead = False
    iterables = map(iter, iterables)
    while True:
        if nrun < nprocs and not all_written and not error_ahead:
            args = tuple(it.next() for it in iterables)
            if len(args) == len(iterables):
                if len(procs) < nrun + 1:
                    p = multiprocessing.Process(
                        target=worker,
                        args=(q_in, q_out, function, eprintignore, pshared))
                    p.daemon = True
                    p.start()
                    procs.append(p)

                q_in.put((nwritten, args))
                nwritten += 1
                nrun += 1
            else:
                all_written = True
                [q_in.put((None, None)) for p in procs]
                q_in.close()

        try:
            while nrun > 0:
                if nrun < nprocs and not all_written and not error_ahead:
                    results.append(q_out.get_nowait())
                else:
                    while True:
                        try:
                            results.append(q_out.get())
                            break
                        except IOError as e:
                            if e.errno != errno.EINTR:
                                raise

                nrun -= 1

        except Queue.Empty:
            pass

        if results:
            results.sort()
            # check for error ahead to prevent further enqueuing
            if any(e for (_, _, e) in results):
                error_ahead = True

            while results:
                (i, r, e) = results[0]
                if i == iout:
                    results.pop(0)
                    if e:
                        if not all_written:
                            [q_in.put((None, None)) for p in procs]
                            q_in.close()
                        raise e
                    else:
                        yield r

                    iout += 1
                else:
                    break

        if all_written and nrun == 0:
            break

    [p.join() for p in procs]


class ArrayStepSharedLLK(BlockedStep):
    """
    Modified ArrayStepShared To handle returned larger point including the
    likelihood values.
    Takes additionally a list of output vars including the likelihoods.

    Parameters
    ----------

    vars : list
        variables to be sampled
    out_vars : list
        variables to be stored in the traces
    shared : dict
        theano variable -> shared variables
    blocked : boolen
        (default True)
    """

    def __init__(self, vars, out_vars, shared, blocked=True):
        self.vars = vars
        self.ordering = ArrayOrdering(vars)
        self.lordering = ListArrayOrdering(out_vars, intype='tensor')
        lpoint = [var.tag.test_value for var in out_vars]
        self.shared = {var.name: shared for var, shared in shared.items()}
        self.blocked = blocked
        self.bij = DictToArrayBijection(self.ordering, self.population[0])
        self.lij = ListToArrayBijection(self.lordering, lpoint)

    def __getstate__(self):
        return self.__dict__

    def __setstate__(self, state):
        self.__dict__.update(state)

    def step(self, point):
        for var, share in self.shared.items():
            share.container.storage[0] = point[var]

        apoint, alist = self.astep(self.bij.map(point))

        return self.bij.rmap(apoint), alist


class BaseATMCMCTrace(object):
    """Base ATMCMC trace object

    Parameters
    ----------

    name : str
        Name of backend
    model : Model
        If None, the model is taken from the `with` context.
    vars : list of variables
        Sampling values will be stored for these variables. If None,
        `model.unobserved_RVs` is used.
    """

    def __init__(self, name, model=None, vars=None):
        self.name = name
        model = modelcontext(model)
        self.model = model

        if vars is None:
            vars = model.unobserved_RVs

        self.vars = vars
        self.varnames = [var.name for var in vars]

        ## Get variable shapes. Most backends will need this
        ## information.

        self.var_shapes_list = [var.tag.test_value.shape for var in vars]
        self.var_dtypes_list = [var.tag.test_value.dtype for var in vars]

        self.var_shapes = {var: shape
            for var, shape in zip(self.varnames, self.var_shapes_list)}
        self.var_dtypes = {var: dtype
            for var, dtype in zip(self.varnames, self.var_dtypes_list)}

        self.chain = None

    def __getitem__(self, idx):
        if isinstance(idx, slice):
            return self._slice(idx)

        try:
            return self.point(int(idx))
        except (ValueError, TypeError):  # Passed variable or variable name.
            raise ValueError('Can only index with slice or integer')

    def __getstate__(self):
        return self.__dict__

    def __setstate__(self, state):
        self.__dict__.update(state)


class Text(BaseATMCMCTrace):
    """
    Text trace object

    Parameters
    ----------

    name : str
        Name of directory to store text files
    model : Model
        If None, the model is taken from the `with` context.
    vars : list of variables
        Sampling values will be stored for these variables. If None,
        `model.unobserved_RVs` is used.
    """

    def __init__(self, name, model=None, vars=None):
        if not os.path.exists(name):
            os.mkdir(name)
        super(Text, self).__init__(name, model, vars)

        self.flat_names = {v: ttab.create_flat_names(v, shape)
                           for v, shape in self.var_shapes.items()}

        self.filename = None
        self.df = None
        self.corrupted_flag = False

    ## Sampling methods

    def setup(self, draws, chain):
        """
        Perform chain-specific setup.

        Parameters
        ----------
        draws : int
            Expected number of draws
        chain : int
            Chain number
        """

        self.chain = chain
        self.filename = os.path.join(self.name, 'chain-{}.csv'.format(chain))

        cnames = [fv for v in self.varnames for fv in self.flat_names[v]]

        if os.path.exists(self.filename):
            os.remove(self.filename)

        with open(self.filename, 'w') as fh:
            fh.write(','.join(cnames) + '\n')

    def record(self, lpoint):
        """
        Record results of a sampling iteration.

        Parameters
        ----------
        lpoint : List of variable values
            Values mapped to variable names
        """

        vals = {}
        for varname, value in zip(self.varnames, lpoint):

            vals[varname] = value.ravel()

        columns = [str(val) for var in self.varnames for val in vals[var]]

        with open(self.filename, 'a') as fh:
            fh.write(','.join(columns) + '\n')

    def _load_df(self):
        if self.df is None:
            try:
                self.df = pd.read_csv(self.filename)
            except pd.parser.EmptyDataError:
                pm._log.warn('Trace %s is empty and needs to be resampled!' % \
                    self.filename)
                os.remove(self.filename)
                self.corrupted_flag = True
            except pd.io.common.CParserError:
                pm._log.warn('Trace %s has wrong size!' % \
                    self.filename)
                self.corrupted_flag = True
                os.remove(self.filename)

    def __len__(self):
        if self.filename is None:
            return 0

        self._load_df()

        if self.df is None:
            return 0
        else:
            return self.df.shape[0]

    def get_values(self, varname, burn=0, thin=1):
        """
        Get values from trace.

        Parameters
        ----------
        varname : str
            Variable name for which values are to be retrieved.
        burn : int
            Burn-in samples from trace. This is the number of samples to be
            thrown out from the start of the trace
        thin : int
            Nuber of thinning samples. Throw out every 'thin' sample of the
            trace.

        Returns
        -------

        :class:`numpy.array`
        """
        self._load_df()
        var_df = self.df[self.flat_names[varname]]
        shape = (self.df.shape[0],) + self.var_shapes[varname]
        vals = var_df.values.ravel().reshape(shape)
        return vals[burn::thin]

    def _slice(self, idx):
        if idx.stop is not None:
            raise ValueError('Stop value in slice not supported.')
        return ndarray._slice_as_ndarray(self, idx)

    def point(self, idx):
        """
        Get point of current chain with variables names as keys.

        Parameters
        ----------
        idx : int
            Index of the nth step of the chain

        Returns
        -------
        dictionary of point values
        """
        idx = int(idx)
        self._load_df()
        pt = {}
        for varname in self.varnames:
            vals = self.df[self.flat_names[varname]].iloc[idx]
            pt[varname] = vals.reshape(self.var_shapes[varname])
        return pt


def get_highest_sampled_stage(homedir, return_final=False):
    """
    Return stage number of stage that has been sampled before the final stage.

    Paramaeters
    -----------
    homedir : str
        Directory to the sampled stage results

    Returns
    -------
    stage number : int
    """
    stages = glob(os.path.join(homedir, 'stage_*'))

    stagenumbers = []
    for s in stages:
        stage_ending = os.path.splitext(s)[0].rsplit('_', 1)[1]
        try:
            stagenumbers.append(int(stage_ending))
        except ValueError:
            pm._log.debug('string - Thats the final stage!')
            if return_final:
                return stage_ending

    stagenumbers.sort()

    return stagenumbers[-1]


def check_multitrace(mtrace, draws, n_chains):
    """
    Check multitrace for incomplete sampling and return indexes from chains
    that need to be resampled.

    Parameters
    ----------
    mtrace : :class:`pymc3.backend.base.MultiTrace`
        Mutlitrace object containing the sampling traces
    draws : int
        Number of steps (i.e. chain length for each Marcov Chain)
    n_chains : int
        Number of Marcov Chains

    Returns
    -------
    list of indexes for chains that need to be resampled
    """

    not_sampled_idx = []

    for chain in range(n_chains):
        if chain in mtrace.chains:
            if len(mtrace._straces[chain]) != draws:
                pm._log.warn('Trace number %i incomplete' % chain)
                mtrace._straces[chain].corrupted_flag = True

        else:
            not_sampled_idx.append(chain)

    flag_bool = [
        mtrace._straces[chain].corrupted_flag for chain in mtrace.chains]

    corrupted_idx = [i for i, x in enumerate(flag_bool) if x]

    return corrupted_idx + not_sampled_idx


def load(name, model=None):
    """
    Load Text database.

    Parameters
    ----------
    name : str
        Name of directory with files (one per chain)
    model : Model
        If None, the model is taken from the `with` context.

    Returns
    -------

    A :class:`pymc3.backend.base.MultiTrace` instance
    """
    files = glob(os.path.join(name, 'chain-*.csv'))

    straces = []
    for f in files:
        chain = int(os.path.splitext(f)[0].rsplit('-', 1)[1])
        strace = Text(name, model=model)
        strace.chain = chain
        strace.filename = f
        straces.append(strace)
    return base.MultiTrace(straces)


def dump(name, trace, chains=None):
    """
    Store values from NDArray trace as CSV files.

    Parameters
    ----------
    name : str
        Name of directory to store CSV files in
    trace : :class:`pymc3.backend.base.MultiTrace` of NDArray traces
        Result of MCMC run with default NDArray backend
    chains : list
        Chains to dump. If None, all chains are dumped.
    """

    if not os.path.exists(name):
        os.mkdir(name)
    if chains is None:
        chains = trace.chains

    var_shapes = trace._straces[chains[0]].var_shapes
    flat_names = {v: ttab.create_flat_names(v, shape)
                  for v, shape in var_shapes.items()}

    for chain in chains:
        filename = os.path.join(name, 'chain-{}.csv'.format(chain))
        df = ttab.trace_to_dataframe(
            trace, chains=chain, flat_names=flat_names)
        df.to_csv(filename, index=False)


def dump_objects(outpath, outlist):
    """
    Dump objects in outlist into pickle file.

    Parameters
    ----------
    outpath : str
        absolute path and file name for the file to be stored
    outlist : list
        of objects to save pickle
    """

    with open(outpath, 'w') as f:
        pickle.dump(outlist, f)


def load_objects(loadpath):
    """
    Load (unpickle) saved (pickled) objects from specified loadpath.

    Parameters
    ----------
    loadpath : absolute path and file name to the file to be loaded

    Returns
    -------
    objects : list
        of saved objects
    """

    try:
        objects = pickle.load(open(loadpath, 'rb'))
    except IOError:
        raise Exception(
            'File %s does not exist! Data already imported?' % loadpath)
    return objects


def load_atmip_params(project_dir, stage_number, mode):
    """
    Load saved parameters from given ATMIP stage.

    Parameters
    ----------
    project_dir : str
        absolute path to directory of BEAT project
    stage number : string
        of stage number or 'final' for last stage
    mode : str
        problem mode that has been solved ('geometry', 'static', 'kinematic')
    """

    stage_path = os.path.join(project_dir, mode, 'stage_%s' % stage_number,
        'atmip.params')
    step = load_objects(stage_path)
    return step


def split_off_list(l, off_length):
    """
    Split a list with length 'off_length' from the beginning of an input
    list l.
    Modifies input list!

    Parameters
    ----------
    l : list
        of objects to be seperated
    off_length : int
        number of elements from l to be split off

    Returns
    -------
    list
    """

    return [l.pop(0) for i in range(off_length)]
