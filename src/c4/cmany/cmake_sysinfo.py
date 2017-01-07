
import re
import os

from collections import OrderedDict as odict

from .conf import CMANY_DIR
from .util import cacheattr, setcwd, runsyscmd
from . import util

_cache_entry = r'^(.*?)(:.*?)=(.*)$'


def setcachevar(builddir, var, value):
    setcachevars(builddir, odict([(var, value)]))


def getcachevar(builddir, var):
    v = getcachevars(builddir, [var])
    return v[var]


def setcachevars(builddir, varvalues):
    with setcwd(builddir, silent=True):
        with open('CMakeCache.txt', 'r') as f:
            ilines = f.readlines()
            olines = []
        for l in ilines:
            for k, v in varvalues.items():
                if l.startswith(k + ':'):
                    n = re.sub(_cache_entry, r'\1\2=' + v, l)
                    l = n
            olines.append(l)
        with open('CMakeCache.txt', 'w') as f:
            f.writelines(olines)


def getcachevars(builddir, varlist):
    vlist = [v + ':' for v in varlist]
    values = odict()
    with setcwd(builddir, silent=True):
        with open('CMakeCache.txt') as f:
            for line in f:
                for v in vlist:
                    if line.startswith(v):
                        ls = line.strip()
                        vt = re.sub(_cache_entry, r'\1', ls)
                        values[vt] = re.sub(_cache_entry, r'\3', ls)
    return values


def loadvars(builddir):
    """if builddir does not exist or does not have a cache, returns an
    empty odict"""
    v = odict()
    if builddir is None or not os.path.exists(builddir):
        return v
    c = os.path.join(builddir, 'CMakeCache.txt')
    if os.path.exists(c):
        with open(c, 'r') as f:
            for line in f:
                # print("loadvars0", line.strip())
                if not re.match(_cache_entry, line):
                    continue
                ls = line.strip()
                name = re.sub(_cache_entry, r'\1', ls)
                vartype = re.sub(_cache_entry, r'\2', ls)[1:]
                value = re.sub(_cache_entry, r'\3', ls)
                # print("loadvars1", name, vartype, value)
                v[name] = CMakeCache.Var(name, value, vartype)
    return v


# -----------------------------------------------------------------------------
class CMakeCache(odict):

    def __init__(self, builddir=None):
        super().__init__(loadvars(builddir))
        self.dirty = False

    def getvars(self, names):
        out = odict()
        for n in names:
            v = self.get(n)
            out[n] = v
        return out

    def b(self, name, val, **kwargs):
        """set a boolean"""
        return self.setvar(name, val, "BOOL", **kwargs)

    def s(self, name, val, **kwargs):
        """set a string"""
        return self.setvar(name, val, "STRING", **kwargs)

    def p(self, name, val, **kwargs):
        """set a path to a dir"""
        if util.in_windows():
            val = re.sub(r'\\', r'/', val)
        return self.setvar(name, val, "PATH", **kwargs)

    def f(self, name, val, **kwargs):
        """set a path to a file"""
        if util.in_windows():
            val = re.sub(r'\\', r'/', val)
        return self.setvar(name, val, "FILEPATH", **kwargs)

    def i(self, name, val, **kwargs):
        """set a cmake internal var"""
        return self.setvar(name, val, "INTERNAL", **kwargs)

    def setvar(self, name, val, vartype, **kwargs):
        v = self.get(name)
        if v is not None:
            changed = v.reset(val, vartype, **kwargs)
            self.dirty |= changed
            return changed
        else:
            assert vartype is not None
            v = CMakeCache.Var(name, val, vartype, dirty=True)
            self[name] = v
            self.dirty = True
            return True

    def commit(self, builddir):
        if (not self.dirty
            or builddir is None
            or not os.path.exists(builddir)
            or not os.path.exists(os.path.join(builddir, 'CMakeCache.txt'))):
            return False
        tmp = odict()
        for _, v in self.items():
            if not v.dirty:
                continue
            tmp[v.name] = v.val
        setcachevars(builddir, tmp)
        for _, v in self.items():
            v.dirty = False
        self.dirty = False
        return True

    # -------------------------------------------------------------------------
    class Var:

        def __init__(self, name, val, vartype="STRING", dirty=False, from_input=False):
            self.name = name
            self.val = val
            self.vartype = vartype
            self.dirty = dirty
            self.from_input = from_input

        def reset(self, val, vartype, **kwargs):
            """

            :param val:
            :param vartype:
            :param kwargs:
                force_dirty, defaults to False
                from_input, defaults to None
            :return:
            """
            force_dirty = kwargs.get('force_dirty', False)
            from_input = kwargs.get('from_input')
            if from_input is not None:
                self.from_input = from_input
            if val != self.val or vartype != self.vartype:
                self.val = val
                self.vartype = vartype
                self.dirty = True
                return True
            if force_dirty:
                self.dirty = True
            return force_dirty

        def __repr__(self):
            return self.name + ':' + self.vartype + '=' + self.val

        def __str__(self):
            return self.name + ':' + self.vartype + '=' + self.val

# -----------------------------------------------------------------------------
# -----------------------------------------------------------------------------
# -----------------------------------------------------------------------------
class CMakeSysInfo:
    """encapsulates the results returned from
    `cmake [-G <which_generator>][-T <toolset>] --system-information`.
    This is used for selecting default values for system, compiler,
    generator, etc."""

    @staticmethod
    def generator():
        return cacheattr(__class__, '_generator_default',
                         lambda: __class__._getstr('CMAKE_GENERATOR', 'default'))

    @staticmethod
    def system_name(which_generator="default"):
        return __class__.var('CMAKE_SYSTEM_NAME', which_generator, lambda v: v.lower())

    @staticmethod
    def architecture(which_generator="default"):
        return __class__.var('CMAKE_SYSTEM_PROCESSOR', which_generator, lambda v: v.lower())

    @staticmethod
    def cxx_compiler(which_generator="default"):
        return __class__.var('CMAKE_CXX_COMPILER', which_generator)

    @staticmethod
    def c_compiler(which_generator="default"):
        return __class__.var('CMAKE_C_COMPILER', which_generator)

    @staticmethod
    def var(var_name, which_generator="default", transform_fn=lambda x: x):
        gs = __class__._getstr
        return cacheattr(__class__, '_{}_{}'.format(var_name, _genid(which_generator)),
                         lambda: transform_fn(gs(var_name, which_generator)))

    @staticmethod
    def info(which_generator="default"):
        return cacheattr(__class__, '_info_' + _genid(which_generator),
                         lambda: __class__.system_info(which_generator))

    @staticmethod
    def _getstr(var_name, which_generator):
        regex = r'^' + var_name + r' "(.*)"'
        for l in __class__.info(which_generator):
            if l.startswith(var_name):
                l = l.strip("\n").lstrip(" ").rstrip(" ")
                # print(var_name, "startswith :", l)
                if re.match(regex, l):
                    s = re.sub(regex, r'\1', l)
                    # print(var_name, "result: '" + s + "'")
                    return s
        err = "could not find variable {} in the output of `cmake --system-information` for generator " + which_generator
        raise Exception(err.format(var_name))

    @staticmethod
    def system_info(gen):
        """generator can be a string or a cmany.Generator object"""
        from .cmany import Generator
        # print("CMakeSystemInfo: asked info for", which_generator)
        p = _genid(gen)
        d = os.path.join(CMANY_DIR, 'cmake_info', p)
        p = os.path.join(d, 'info')
        if os.path.exists(p):
            # print("CMakeSystemInfo: asked info for", which_generator, "... found", p)
            with open(p, "r") as f:
                i = f.readlines()
        else:
            if isinstance(gen, Generator):
                cmd = ['cmake'] + gen.configure_args() + ['--system-information']
            else:
                if gen == "default" or gen == "":
                    cmd = ['cmake', '--system-information']
                else:
                    if gen.startswith('vs') or gen.startswith('Visual Studio'):
                        from . import vsinfo
                        gen = vsinfo.to_gen(gen)
                    cmd = ['cmake', '-G', str(gen), '--system-information']

            if not os.path.exists(d):
                os.makedirs(d)
            print("\ncmany: CMake information for generator '{}' was not found. Creating and storing...".format(gen))
            with setcwd(d):
                out = runsyscmd(cmd, echo_output=False, capture_output=True)
            print("cmany: finished generating information for generator '{}'\n".format(gen))
            with open(p, "w") as f:
                f.write(out)
            i = out.split("\n")
        return i

# -----------------------------------------------------------------------------
# -----------------------------------------------------------------------------
# -----------------------------------------------------------------------------
def _genid(gen):
    from .cmany import Generator
    p = gen.full_name if isinstance(gen, Generator) else gen
    p = re.sub(r'[() ]', '_', p)
    return p
