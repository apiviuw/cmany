#!/usr/bin/env python3

import os
import subprocess
import sys
import re
import glob
import json
import argparse
from datetime import datetime as datetime
from collections import OrderedDict as odict
from multiprocessing import cpu_count as cpu_count


PYCMAKE_DIR = os.path.expanduser("~/.pycmake/")


def which(cmd):
    """look for an executable in the current PATH environment variable"""
    if os.path.exists(cmd):
        return cmd
    exts = ("",".exe",".bat") if System.default_str() == "windows" else ""
    for path in os.environ["PATH"].split(os.pathsep):
        for e in exts:
            j = os.path.join(path, cmd+e)
            if os.path.exists(j):
                return j
    return None


def chkf(*args):
    """join the args as a path and check whether that path exists"""
    f = os.path.join(*args)
    if not os.path.exists(f):
        raise Exception("path does not exist: " + f + ". Current dir=" + os.getcwd())
    return f


def runsyscmd(arglist, echo_cmd=True, echo_output=True, capture_output=False, as_bytes_string=False, ):
    """run a system command. Note that stderr is interspersed with stdout"""
    s = " ".join(arglist)
    if echo_cmd:
        print("running command:", s)
    if as_bytes_string:
        assert not echo_output
        result = subprocess.run(s, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        result.check_returncode()
        if capture_output:
            return str(result.stdout)
    elif not echo_output:
        result = subprocess.run(s, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                universal_newlines=True)
        result.check_returncode()
        if capture_output:
            return str(result.stdout)
    elif echo_output:
        # http://stackoverflow.com/a/4417735
        popen = subprocess.Popen(s, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                 universal_newlines=True)
        out = ""
        for stdout_line in iter(popen.stdout.readline, ""):
            print(stdout_line, end="")
            if capture_output:
                out += stdout_line
        popen.stdout.close()
        return_code = popen.wait()
        if return_code != 0:
            raise subprocess.CalledProcessError(return_code, s)
        if capture_output:
            return out


def cmember(obj, name, function):
    """add and cache an object member which is the result of a given function.
    This is for implementing lazy getters when the function call is expensive."""
    if hasattr(obj, name):
        val = getattr(obj, name)
    else:
        val = function()
        setattr(obj, name, val)
    return val


def ctor(cls, args):
    if not isinstance(args, list):
        args = [args]
    l = []
    for i in args:
        l.append(cls(i))
    return l


# -----------------------------------------------------------------------------
class cwd_back:
    """temporarily change into a directory inside a with block"""

    def __init__(self, dir_):
        self.dir = dir_

    def __enter__(self):
        self.old = os.getcwd()
        if self.old == self.dir:
            return
        print("Entering directory", self.dir, "(was in {})".format(self.old))
        chkf(self.dir)
        os.chdir(self.dir)

    def __exit__(self, exc_type, exc_value, traceback):
        if self.old == self.dir:
            return
        print("Returning to directory", self.old, "(currently in {})".format(self.dir))
        chkf(self.old)
        os.chdir(self.old)


# -----------------------------------------------------------------------------
class CMakeSystemInformation:
    """encapsulates the results returned from `cmake [-G <which_generator>] --system-information`.
    This is used for selecting default values for system, compiler, generator, etc."""

    @staticmethod
    def generator():
        return cmember(__class__, '_generator_default',
                       lambda: __class__._getstr('CMAKE_GENERATOR', 'default'))

    @staticmethod
    def system_name(which_generator="default"):
        return cmember(__class__, '_system_name_'+which_generator,
                       lambda: __class__._getstr('CMAKE_SYSTEM_NAME', which_generator).lower())

    @staticmethod
    def architecture(which_generator="default"):
        return cmember(__class__, '_architecture_'+which_generator,
                       lambda: __class__._getstr('CMAKE_SYSTEM_PROCESSOR', which_generator).lower())

    @staticmethod
    def cxx_compiler(which_generator="default"):
        return cmember(__class__, '_cxx_compiler_'+which_generator,
                       lambda: __class__._getpath('CMAKE_CXX_COMPILER', which_generator))

    @staticmethod
    def c_compiler(which_generator="default"):
        return cmember(__class__, '_c_compiler_'+which_generator,
                       lambda: __class__._getpath('CMAKE_C_COMPILER', which_generator))

    @staticmethod
    def info(which_generator="default"):
        return cmember(__class__, '_info'+which_generator,
                       lambda: __class__.system_info(which_generator))

    @staticmethod
    def _getpath(var_name, which_generator):
        s = __class__._getstr(var_name, which_generator)
        # s = re.sub(r'\\', '/', s)
        return s

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
        err = "could not find variable {} in the output of `cmake --system-information`"
        raise Exception(err.format(var_name))

    @staticmethod
    def system_info(gen):
        # print("CMakeSystemInfo: asked info for", which_generator)
        p = re.sub(r'[() ]', '_', gen)
        d = os.path.join(PYCMAKE_DIR, 'cmake_info', p)
        p = os.path.join(d, 'info')
        if os.path.exists(p):
            # print("CMakeSystemInfo: asked info for", which_generator, "... found", p)
            with open(p, "r") as f:
                i = f.readlines()
        else:
            if gen == "default":
                cmd = ['cmake', '--system-information']
            else:
                gen = Generator.resolve_alias(gen)
                cmd = ['cmake', '-G', '"{}"'.format(gen), '--system-information']
            if not os.path.exists(d):
                os.makedirs(d)
            print("pycmake: CMake information for generator '{}' was not found. Creating and storing...".format(gen))
            with cwd_back(d):
                out = runsyscmd(cmd, echo_output=False, capture_output=True)
            print("pycmake: finished generating information for generator '{}'".format(gen))
            with open(p, "w") as f:
                f.write(out)
            i = out.split("\n")
        return i


# -----------------------------------------------------------------------------
class BuildItem:

    def __init__(self, name):
        self.name = name
        self.preload = None

    def __repr__(self):
        return self.name

    def __str__(self):
        return self.name

    def preload(self):
        """return the name of a script to populate the cmake cache, via -C"""
        return self.preload


# -----------------------------------------------------------------------------
class BuildType(BuildItem):
    """Specifies a build type, ie, one of Release, Debug, etc"""

    @staticmethod
    def default():
        return BuildType("Release")


# -----------------------------------------------------------------------------
class System(BuildItem):
    """Specifies an operating system"""

    @staticmethod
    def default():
        """return the current operating system"""
        return System(__class__.default_str())

    @staticmethod
    def default_str():
        s = CMakeSystemInformation.system_name()
        if s == "mac os x" or s == "Darwin":
            s = "mac"
        return s
        # if not hasattr(System, "_current"):
        #     if sys.platform == "linux" or sys.platform == "linux2":
        #         System._current = System("linux")
        #     elif sys.platform == "darwin":
        #         System._current = System("mac")
        #     elif sys.platform == "win32":
        #         System._current = System("windows")
        #     else:
        #         raise Exception("unknown system")
        # return System._current


# -----------------------------------------------------------------------------
class Architecture(BuildItem):
    """Specifies a processor architecture"""

    @staticmethod
    def default():
        """return the architecture of the current machine"""
        return Architecture(__class__.default_str())

    @staticmethod
    def default_str():
        s = CMakeSystemInformation.architecture()
        if s == "amd64":
            s = "x86_64"
        return s
        # # http://stackoverflow.com/a/12578715/5875572
        # import platform
        # machine = platform.machine()
        # if machine.endswith('64'):
        #     return Architecture('x86_64')
        # elif machine.endswith('86'):
        #     return Architecture('x32')
        # raise Exception("unknown architecture")

    @property
    def is64(self):
        def fn():
            s = re.search('64', self.name)
            return s is not None
        return cmember(self, "_is64", fn)

    @property
    def is32(self):
        return not self.is64

    @property
    def is_arm(self):
        return "arm" in self.name.lower()


# -----------------------------------------------------------------------------
class CompileOptions:

    def __init__(self, name=""):
        self.name = name
        self.cmake_flags = []
        self.cflags = []
        self.lflags = []
        self.macros = []


# -----------------------------------------------------------------------------


def _opt(name, gcclike, vs, expl):
        return name, CompileOption(name, gcclike, vs, expl)


class CompileOption:

    def __init__(self, name, gcclike, vs, expl):
        self.name = name
        self.gcclike = gcclike
        self.vs = vs
        self.expl = expl

    def get(self, compiler):
        if compiler.is_msvc:
            return self.vs
        else:
            return self.gcclike


option_presets = odict([
    #    name                   gcclike                  vs                explanation
    _opt('cpp11'              , '-std=c++11'           , ''              , 'enable C++11 mode'),  # nopep8
    _opt('cpp14'              , '-std=c++14'           , ''              , 'enable C++14 mode'),  # nopep8
    _opt('cpp1z'              , '-std=c++1z'           , ''              , 'enable C++1z mode'),  # nopep8
    _opt('wall'               , '-Wall'                , '/Wall'         , 'enable full warnings'),  # nopep8
    _opt('pedantic'           , '-Wpedantic'           , '/W4'           , 'compile in pedantic mode'),  # nopep8
    _opt('strict_aliasing'    , '-fstrict-aliasing'    , ''              , 'enable strict aliasing'),  # nopep8
    _opt('no_strict_aliasing' , '-fno-strict-aliasing' , ''              , 'disable strict aliasing'),  # nopep8
    _opt('fast_math'          , '-ffast-math'          , '/fp:fast'      , 'enable fast math http://stackoverflow.com/a/22135559'),  # nopep8
    _opt('no_rtti'            , '-fno-rtti'            , '/GR-'          , 'disable run-time type information'),  # nopep8
    _opt('no_exceptions'      , '-fno-exceptions'      , '/EHsc-'        , 'disable exceptions'),  # nopep8
    _opt('no_stdlib'          , '-fnostdlib'           , '/NODEFAULTLIB' , 'disable standard library'),  # nopep8
    _opt('thread'             , '-pthread'             , ''              , 'enable threads'),  # nopep8
    _opt('g'                  , '-g'                   , ''              , 'add debug information'),  # nopep8
    _opt('g3'                 , '-g3'                  , ''              , 'add full debug information'),  # nopep8
])

del _opt


def get_compile_option_preset(name):
    opt = option_presets.get(name)
    if opt is None:
        raise Exception("could not find compile option preset: " + name)
    return opt


# -----------------------------------------------------------------------------
class Compiler(BuildItem):
    """Specifies a compiler"""

    @staticmethod
    def default():
        return Compiler(__class__.default_str())

    @staticmethod
    def default_str():
        if str(System.default()) != "windows":
            cpp = CMakeSystemInformation.cxx_compiler()
        else:
            vs = VisualStudioInfo.find_any()
            cpp = vs.name if vs is not None else CMakeSystemInformation.cxx_compiler()
        return cpp

    def __init__(self, path):
        if path.startswith("vs") or path.startswith("Visual Studio"):
            vs = VisualStudioInfo(path)
            self.vs = vs
            path = vs.cxx_compiler
        else:
            p = which(path)
            if p is None:
                raise Exception("compiler not found: " + path)
            if p != path:
                print("compiler: selected {} for {}".format(p, path))
            path = os.path.abspath(p)
        name, version, version_full = self.get_version(path)
        self.shortname = name
        self.gcclike = self.shortname in ('gcc', 'clang', 'icc')
        self.is_msvc = self.shortname.startswith('vs')
        if not self.is_msvc:
            name += version
        self.path = path
        self.version = version
        self.version_full = version_full
        super().__init__(name)
        self.c_compiler = __class__.get_c_compiler(self.shortname, self.path)

    @staticmethod
    def get_c_compiler(shortname, cxx_compiler):
        # if cxx_compiler.endswith("c++") or cxx_compiler.endswith('c++.exe'):
        #     cc = re.sub(r'c\+\+', r'cc', cxx_compiler)
        if shortname == "icc":
            cc = re.sub(r'icpc', r'icc', cxx_compiler)
        elif shortname == "gcc":
            cc = re.sub(r'g\+\+', r'gcc', cxx_compiler)
        elif shortname == "clang":
            cc = re.sub(r'clang\+\+', r'clang', cxx_compiler)
        else:
            cc = cxx_compiler
        return cc

    def get_version(self, path):
        # is this visual studio?
        if hasattr(self, "vs"):
            return self.vs.name, str(self.vs.year), self.vs.name
        # # other compilers
        # print("cmp: found compiler:", name, path)
        out = runsyscmd([path, '--version'], echo_cmd=False, capture_output=True).strip("\n")
        version_full = out.split("\n")[0]
        splits = version_full.split(" ")
        name = splits[0].lower()
        # print("cmp: version:", name, "---", firstline, "---")
        vregex = r'(\d+\.\d+)\.\d+'
        if name.startswith("g++") or name.startswith("gcc"):
            name = "gcc"
            version = runsyscmd([path, '-dumpversion'], echo_cmd=False, capture_output=True).strip("\n")
            version = re.sub(vregex, r'\1', version)
            # print("gcc version:", version, "---")
        elif name.startswith("clang"):
            name = "clang"
            version = re.sub(r'clang version ' + vregex + '.*', r'\1', version_full)
            # print("clang version:", version, "---")
        elif name.startswith("icpc"):
            name = "icc"
            version = re.sub(r'icpc \(ICC\) ' + vregex + '.*', r'\1', version_full)
            # print("icc version:", version, "---")
        else:
            version = runsyscmd([path, '--dumpversion'], echo_cmd=False, capture_output=True).strip("\n")
            version = re.sub(vregex, r'\1', version)
        #
        return name, version, version_full


# -----------------------------------------------------------------------------
class VisualStudioInfo:

    order = ('vs2015','vs2017','vs2013','vs2012','vs2010','vs2008','vs2005',)
    # a reversible dictionary for the VS version numbers
    _versions = {
        'vs2015':14, 14:'vs2015', 'vs2015_64':14, 'vs2015_32':14, 'vs2015_arm':14 ,  # nopep8
        'vs2017':15, 15:'vs2017', 'vs2017_64':15, 'vs2017_32':15, 'vs2017_arm':15 ,  # nopep8
        'vs2013':12, 12:'vs2013', 'vs2013_64':12, 'vs2013_32':12, 'vs2013_arm':12 ,  # nopep8
        'vs2012':11, 11:'vs2012', 'vs2012_64':11, 'vs2012_32':11, 'vs2012_arm':11 ,  # nopep8
        'vs2010':10, 10:'vs2010', 'vs2010_64':10, 'vs2010_32':10, 'vs2010_ia64':10,  # nopep8
        'vs2008':9 , 9 :'vs2008', 'vs2008_64':9 , 'vs2008_32':9 , 'vs2008_ia64':9 ,  # nopep8
        'vs2005':8 , 8 :'vs2005', 'vs2005_64':8 , 'vs2005_32':8 ,   # nopep8
    }
    _sfx = ' Win64' if Architecture.default().is64 else ''
    # a reversible dictionary for the names
    _names = {
        'vs2017'      : 'Visual Studio 15 2017' + _sfx , 'Visual Studio 15 2017' + _sfx : 'vs2017'      ,  # nopep8
        'vs2017_32'   : 'Visual Studio 15 2017'        , 'Visual Studio 15 2017'        : 'vs2017_32'   ,  # nopep8
        'vs2017_64'   : 'Visual Studio 15 2017 Win64'  , 'Visual Studio 15 2017 Win64'  : 'vs2017_64'   ,  # nopep8
        'vs2017_arm'  : 'Visual Studio 15 2017 ARM'    , 'Visual Studio 15 2017 ARM'    : 'vs2017_arm'  ,  # nopep8
        'vs2015'      : 'Visual Studio 14 2015' + _sfx , 'Visual Studio 14 2015' + _sfx : 'vs2015'      ,  # nopep8
        'vs2015_32'   : 'Visual Studio 14 2015'        , 'Visual Studio 14 2015'        : 'vs2015_32'   ,  # nopep8
        'vs2015_64'   : 'Visual Studio 14 2015 Win64'  , 'Visual Studio 14 2015 Win64'  : 'vs2015_64'   ,  # nopep8
        'vs2015_arm'  : 'Visual Studio 14 2015 ARM'    , 'Visual Studio 14 2015 ARM'    : 'vs2015_arm'  ,  # nopep8
        'vs2013'      : 'Visual Studio 12 2013' + _sfx , 'Visual Studio 12 2013' + _sfx : 'vs2013'      ,  # nopep8
        'vs2013_32'   : 'Visual Studio 12 2013'        , 'Visual Studio 12 2013'        : 'vs2013_32'   ,  # nopep8
        'vs2013_64'   : 'Visual Studio 12 2013 Win64'  , 'Visual Studio 12 2013 Win64'  : 'vs2013_64'   ,  # nopep8
        'vs2013_arm'  : 'Visual Studio 12 2013 ARM'    , 'Visual Studio 12 2013 ARM'    : 'vs2013_arm'  ,  # nopep8
        'vs2012'      : 'Visual Studio 11 2012' + _sfx , 'Visual Studio 11 2012' + _sfx : 'vs2012'      ,  # nopep8
        'vs2012_32'   : 'Visual Studio 11 2012'        , 'Visual Studio 11 2012'        : 'vs2012_32'   ,  # nopep8
        'vs2012_64'   : 'Visual Studio 11 2012 Win64'  , 'Visual Studio 11 2012 Win64'  : 'vs2012_64'   ,  # nopep8
        'vs2012_arm'  : 'Visual Studio 11 2012 ARM'    , 'Visual Studio 11 2012 ARM'    : 'vs2012_arm'  ,  # nopep8
        'vs2010'      : 'Visual Studio 10 2010' + _sfx , 'Visual Studio 10 2010' + _sfx : 'vs2010'      ,  # nopep8
        'vs2010_32'   : 'Visual Studio 10 2010'        , 'Visual Studio 10 2010'        : 'vs2010_32'   ,  # nopep8
        'vs2010_64'   : 'Visual Studio 10 2010 Win64'  , 'Visual Studio 10 2010 Win64'  : 'vs2010_64'   ,  # nopep8
        'vs2010_ia64' : 'Visual Studio 10 2010 IA64'   , 'Visual Studio 10 2010 IA64'   : 'vs2010_ia64' ,  # nopep8
        'vs2008'      : 'Visual Studio 8 2008' + _sfx  , 'Visual Studio 8 2008' + _sfx  : 'vs2008'      ,  # nopep8
        'vs2008_32'   : 'Visual Studio 8 2008'         , 'Visual Studio 8 2008'         : 'vs2008_32'   ,  # nopep8
        'vs2008_64'   : 'Visual Studio 8 2008 Win64'   , 'Visual Studio 8 2008 Win64'   : 'vs2008_64'   ,  # nopep8
        'vs2008_ia64' : 'Visual Studio 8 2008 IA64'    , 'Visual Studio 8 2008 IA64'    : 'vs2008_ia64' ,  # nopep8
        'vs2005'      : 'Visual Studio 5 2005' + _sfx  , 'Visual Studio 5 2005' + _sfx  : 'vs2005'      ,  # nopep8
        'vs2005_32'   : 'Visual Studio 5 2005'         , 'Visual Studio 5 2005'         : 'vs2005_32'   ,  # nopep8
        'vs2005_64'   : 'Visual Studio 5 2005 Win64'   , 'Visual Studio 5 2005 Win64'   : 'vs2005_64'   ,  # nopep8
    }

    _toolsets = (
        # vs2017 compiler toolsets
        'v141_clang', 'v141_xp', 'v141',
        # vs2015 compiler toolsets
        'v140_clang', 'v140_xp', 'v140',
        # vs2013 compiler toolsets
        'v120_xp', 'v120',
        # vs2013 compiler toolsets
        'v110_xp', 'v110',
        # vs2013 compiler toolsets
        'v100_xp', 'v100',
        # aliases - implicit compiler toolsets (the same as the chosen VS version)
        'xp', 'clang'
    )

    @staticmethod
    def parse_toolset(name):
        ts = '|'.join(__class__._toolsets)
        rx = 'vs.....*_('+ts+')$'
        if not re.search(rx, name):
            return None
        toolset = re.sub(rx, r'\1', name)
        if toolset not in __class__._toolsets:
            raise Exception("could not parse toolset {} from vs spec {}".format(toolset, name))
        if toolset == 'clang' or toolset == 'xp':
            assert re.match('vs....', name)
            year = int(re.sub(r'^vs(....).*', r'\1', name))
            if year == 2017:
                vs_toolset = 'v141_' + toolset
            elif year == 2015:
                vs_toolset = 'v140_' + toolset
            else:
                assert toolset != "clang"
                if year == 2013:
                    vs_toolset = 'v120_' + toolset
                elif year == 2012:
                    vs_toolset = 'v110_' + toolset
                elif year == 2010:
                    vs_toolset = 'v100_' + toolset
                else:
                    raise Exception("toolset not implemented for " + name + ". toolset="+toolset)
        else:
            vs_toolset = toolset
        if vs_toolset.endswith('clang'):
            vs_toolset += '_c2'
        return vs_toolset

    @staticmethod
    def sep_name_toolset(name):
        toolset = __class__.parse_toolset(name)
        cn = name if toolset is None else name[:(-len(toolset)-1)]
        return cn, toolset

    def __init__(self, name):
        cn, toolset = __class__.sep_name_toolset(name)
        if cn not in __class__._versions.keys():
            raise Exception("unknown alias: " + name)
        ver = __class__._versions[cn]
        self.name = name
        self.name_without_toolset = cn
        self.toolset = toolset
        self.ver = ver
        self.year = int(re.sub(r'^vs(....).*', r'\1', name))
        self.gen = __class__.to_gen(cn)
        self.dir = __class__.vsdir(ver)
        self.msbuild = __class__.msbuild(ver)
        self.vcvarsall = __class__.vcvarsall(ver)
        self.is_installed = __class__.is_installed(ver)
        self.cxx_compiler = __class__.cxx_compiler(ver)
        self.c_compiler = __class__.c_compiler(ver)

    def cmd(self, cmd_args, *runsyscmd_args):
        if isinstance(cmd_args, list):
            cmd_args = " ".join(cmd_args)
        cmd_args = self.vcvarsall + "; " + cmd_args
        return runsyscmd(cmd_args, *runsyscmd_args)

    @staticmethod
    def find_any():
        for k in __class__.order:
            if __class__.is_installed(k):
                return __class__(k)
        return None

    @staticmethod
    def cxx_compiler(name_or_gen_or_ver):
        if not __class__.is_installed(name_or_gen_or_ver):
            return None
        return CMakeSystemInformation.cxx_compiler(__class__.to_gen(name_or_gen_or_ver))

    @staticmethod
    def c_compiler(name_or_gen_or_ver):
        if not __class__.is_installed(name_or_gen_or_ver):
            return None
        return CMakeSystemInformation.c_compiler(__class__.to_gen(name_or_gen_or_ver))

    @staticmethod
    def to_name(ver_or_name_or_gen):
        if isinstance(ver_or_name_or_gen, int):
            return __class__._versions[ver_or_name_or_gen]
        else:
            if ver_or_name_or_gen.startswith('vs'):
                return __class__.sep_name_toolset(ver_or_name_or_gen)[0]
            n = __class__._names.get(ver_or_name_or_gen)
            if n is not None:
                return n
        raise Exception("could not find '{}'".format(ver_or_name_or_gen))

    @staticmethod
    def to_ver(ver_or_name_or_gen):
        if isinstance(ver_or_name_or_gen, int):
            return ver_or_name_or_gen
        else:
            n = __class__.to_name(ver_or_name_or_gen)
            return __class__._versions[n]

    @staticmethod
    def to_gen(ver_or_name_or_gen):
        if isinstance(ver_or_name_or_gen, int):
            ver_or_name_or_gen = __class__._versions[ver_or_name_or_gen]
        if ver_or_name_or_gen.startswith('Visual Studio'):
            return ver_or_name_or_gen
        ver_or_name_or_gen = __class__.sep_name_toolset(ver_or_name_or_gen)[0]
        return __class__._names[ver_or_name_or_gen]

    @staticmethod
    def vsdir(ver_or_name_or_gen):
        """get the directory where VS is installed"""
        ver = __class__.to_ver(ver_or_name_or_gen)
        if ver < 15:
            progfilesx86 = os.environ['ProgramFiles(x86)']
            d = os.path.join(progfilesx86, 'Microsoft Visual Studio ' + str(ver) + '.0')
            if not os.path.exists(d):
                try:
                    v = os.environ['VS{}0COMNTOOLS'.format(str(ver))]
                    d = os.path.abspath(os.path.join(v, '..', '..'))
                except:
                    pass
        elif ver == 15:
            # VS 2017+ is no longer a singleton, and may be installed anywhere,
            # and the environment variable VS***COMNTOOLS no longer exists.
            # So use CMake to do the grunt work for us, and pick up from there.
            # http://stackoverflow.com/questions/40694598/how-do-i-call-visual-studio-2017-rcs-version-of-msbuild-from-a-bat-files
            def fn():
                if not __class__.is_installed(ver):  # but use cmake only if VS2017 is installed
                    return ""
                cxx = CMakeSystemInformation.cxx_compiler(__class__.to_gen('vs2017'))
                # VC dir is located on the root of the VS install dir
                vsdir = re.sub(r'(.*)[\\/]VC[\\/].*', r'\1', str(cxx))
                return vsdir
            d = cmember(__class__, '_vs2017dir', fn)
        else:
            raise Exception('VS Version not implemented: ' + str(ver))
        return d

    @staticmethod
    def vcvarsall(ver_or_name_or_gen):
        """get the path to vcvarsall.bat"""
        ver = __class__.to_ver(ver_or_name_or_gen)
        if ver < 15:
            s = os.path.join(__class__.vsdir(ver), 'VC', 'vcvarsall.bat')
        elif ver == 15:
            s = os.path.join(__class__.vsdir(ver), 'VC', 'Auxiliary', 'Build', 'vcvarsall.bat')
        else:
            raise Exception('VS Version not implemented: ' + str(ver))
        return s

    @staticmethod
    def msbuild(ver_or_name_or_gen):
        """get the MSBuild.exe path"""
        ver = __class__.to_ver(ver_or_name_or_gen)
        if ver < 15:
            progfilesx86 = os.environ['ProgramFiles(x86)']
            msbuild = os.path.join(progfilesx86, 'MSBuild', str(ver)+'.0', 'bin', 'MSBuild.exe')
        else:
            if ver > 15:
                raise Exception('VS Version not implemented: ' + str(ver))
            if Architecture.default().is64:
                msbuild = os.path.join(__class__.vsdir(ver), 'MSBuild', '15.0', 'Bin', 'amd64', 'MSBuild.exe')
            else:
                msbuild = os.path.join(__class__.vsdir(ver), 'MSBuild', '15.0', 'Bin', 'MSBuild.exe')
        return msbuild

    @staticmethod
    def devenv(ver_or_name_or_gen):
        """get path to devenv"""
        pass

    @staticmethod
    def is_installed(ver_or_name_or_gen):
        ver = __class__.to_ver(ver_or_name_or_gen)
        return cmember(__class__, '_is_installed_'+str(ver), lambda: __class__._is_installed_impl(ver))

    @staticmethod
    def _is_installed_impl(ver):
        assert isinstance(ver, int)
        if ver < 15:
            import winreg as wr
            key = "SOFTWARE\Microsoft\VisualStudio\{}.0"
            try:
                wr.OpenKey(wr.HKEY_LOCAL_MACHINE, key.format(ver), 0, wr.KEY_READ)
                # fail if we can't find the dir
                if not os.path.exists(__class__.vsdir(ver)):
                    return False
                # apparently the dir is not enough, so check also vcvarsall
                if not os.path.exists(__class__.vcvarsall(ver)):
                    return False
                return True
            except:
                return False
        else:
            #
            # ~~~~~~~~~~~~~~ this is fragile.... ~~~~~~~~~~~~~~
            #
            # Unlike earlier versions, VS2017 is no longer a singleton installation.
            # Each VS2017 installed instance keeps a store of its data under
            # %ProgramData%\Microsoft\VisualStudio\Packages\_Instances\<hash>\state.json
            #
            # this info was taken from:
            # http://stackoverflow.com/questions/40694598/how-do-i-call-visual-studio-2017-rcs-version-of-msbuild-from-a-bat-files
            progdata = os.environ['ProgramData']
            instances_dir = os.path.join(progdata, 'Microsoft', 'VisualStudio', 'Packages', '_Instances')
            if not os.path.exists(instances_dir):
                return False
            pat = os.path.join(instances_dir, '*', 'state.json')
            instances = glob.glob(pat)
            if not instances:
                return False
            for i in instances:
                with open(i, encoding="utf8") as json_str:
                    d = json.load(json_str)

                    def _get(*entry):
                        j = "/".join(list(entry))
                        try:
                            if isinstance(entry, str):
                                v = d[entry]
                            else:
                                v = None
                                for e in entry:
                                    # print("key:", e, "value:", v if v is not None else "<none yet>")
                                    v = v[e] if v is not None else d[e]
                        except:
                            raise Exception("could not find entry '" + j + "' in the json data at " + i + "\nMaybe the specs have changed?")  # nopep8
                        return v
                    # check that the version matches
                    version_string = _get('catalogInfo', 'buildVersion')
                    version_number = int(re.sub(r'(\d\d).*', r'\1', version_string))
                    if version_number != ver:
                        continue
                    # check that the directory exists
                    install_dir = _get('installationPath')
                    if not os.path.exists(install_dir):
                        continue
                    # maybe further checks are necessary?
                    # For now we stop here, and accept that this installation exists.
                    return True
            return False


# -----------------------------------------------------------------------------
class Variant(BuildItem):
    """for variations in compile options"""

    def __init__(self, name):
        super().__init__(name)
        self.options = CompileOptions()


# -----------------------------------------------------------------------------
class Generator(BuildItem):

    """Visual Studio aliases example:
    vs2013: use the bitness of the default OS
    vs2013_32: use 32bit version
    vs2013_64: use 64bit version
    """

    @staticmethod
    def default():
        return Generator(__class__.default_str(), cpu_count())

    @staticmethod
    def default_str():
        s = CMakeSystemInformation.generator()
        return s

    @staticmethod
    def create_default(system, arch, compiler, num_jobs):
        if not compiler.is_msvc:
            if System.default_str() == "windows":
                return Generator("Unix Makefiles", num_jobs)
            else:
                return Generator(__class__.default_str(), num_jobs)
        else:
            name = compiler.name
            if arch.is_arm:
                raise Exception("not implemented")
            elif arch.is64:
                if not compiler.name.endswith("_64"):
                    compiler.name += "_64"
            elif arch.is32:
                if not compiler.name.endswith("_32"):
                    compiler.name += "_32"
            return Generator(name, num_jobs)

    @staticmethod
    def resolve_alias(gen):
        if gen.startswith('vs') or gen.startswith('Visual Studio'):
            return VisualStudioInfo.to_gen(gen)
        return gen

    def __init__(self, name, num_jobs):
        if name.startswith('vs'):
            name = VisualStudioInfo.to_gen(name)
        self.alias = name
        super().__init__(name)
        self.num_jobs = num_jobs
        self.is_makefile = name.endswith("Makefiles")
        self.is_ninja = name.endswith("Ninja")
        self.is_msvc = name.startswith("Visual Studio")

    def configure_args(self):
        if self.name != "":
            return ['-G', '"{}"'.format(self.name)]
        else:
            return []

    def cmd(self, targets, build):
        if self.is_makefile:
            return ['make', '-j', str(self.num_jobs)] + targets
        elif self.is_msvc:
            if not hasattr(self, "sln"):
                sln_files = glob.glob("*.sln")
                if len(sln_files) != 1:
                    raise Exception("there's more than one solution file in the project folder")
                self.sln = sln_files[0]
            return [build.compiler.vs.msbuild, self.sln,
                    '/maxcpucount:'+str(self.num_jobs),
                    '/property:Configuration='+str(build.buildtype),
                    '/target:'+';'.join(targets)]
        else:
            return ['cmake', '--build', '.', '--config', str(build.buildtype) ] + ['--target '+ t for t in targets ]

    def install(self, build):
        return ['cmake', '--build', '.', '--config', str(build.buildtype), '--target', 'install']

    """
    generators: https://cmake.org/cmake/help/v3.7/manual/cmake-generators.7.html

    Unix Makefiles
    MSYS Makefiles
    MinGW Makefiles
    NMake Makefiles
    Ninja
    Watcom WMake
    CodeBlocks - Ninja
    CodeBlocks - Unix Makefiles
    CodeBlocks - MinGW Makefiles
    CodeBlocks - NMake Makefiles
    CodeLite - Ninja
    CodeLite - Unix Makefiles
    CodeLite - MinGW Makefiles
    CodeLite - NMake Makefiles
    Eclipse CDT4 - Ninja
    Eclipse CDT4 - Unix Makefiles
    Eclipse CDT4 - MinGW Makefiles
    Eclipse CDT4 - NMake Makefiles
    KDevelop3
    KDevelop3 - Unix Makefiles
    Kate - Ninja
    Kate - Unix Makefiles
    Kate - MinGW Makefiles
    Kate - NMake Makefiles
    Sublime Text 2 - Ninja
    Sublime Text 2 - Unix Makefiles
    Sublime Text 2 - MinGW Makefiles
    Sublime Text 2 - NMake Makefiles

    Visual Studio 6
    Visual Studio 7
    Visual Studio 7 .NET 2003
    Visual Studio 8 2005 [Win64|IA64]
    Visual Studio 9 2008 [Win64|IA64]
    Visual Studio 10 2010 [Win64|IA64]
    Visual Studio 11 2012 [Win64|ARM]
    Visual Studio 12 2013 [Win64|ARM]
    Visual Studio 14 2015 [Win64|ARM]
    Visual Studio 15 2017 [Win64|ARM]

    Green Hills MULTI
    Xcode
    """


# -----------------------------------------------------------------------------
class Build:
    """Holds a build's settings"""

    pfile = "pycmake_preload.cmake"

    def __init__(self, proj_root, build_root, install_root,
                 system, arch, buildtype, compiler, variant,
                 num_jobs):
        self.generator = Generator.create_default(sys, arch, compiler, num_jobs)
        self.system = system
        self.architecture = arch
        self.buildtype = buildtype
        self.compiler = compiler
        self.variant = variant
        # self.crosscompile = (system != System.default())
        # self.toolchain = None
        self.projdir = chkf(proj_root)
        self.buildroot = os.path.abspath(build_root)
        self.builddir = os.path.abspath(os.path.join(build_root, self._cat("-", for_build_dir=True)))
        self.preload_file = os.path.join(self.builddir, Build.pfile)
        self.installroot = os.path.abspath(install_root)
        self.installdir = os.path.join(self.installroot, self._cat("-", for_build_dir=False))

    def __repr__(self):
        return self._cat("-", for_build_dir=False)

    def _cat(self, sep, for_build_dir):
        if self.compiler.is_msvc and for_build_dir:
            s = "{1}{0}{2}{0}{3}"
            s = s.format(sep, self.system, self.architecture, self.compiler)
        else:
            s = "{1}{0}{2}{0}{3}{0}{4}"
            s = s.format(sep, self.system, self.architecture, self.compiler, self.buildtype)
        if self.variant:
            s += "{0}{1}".format(sep, self.variant)
        return s

    def create_dir(self):
        if not os.path.exists(self.builddir):
            os.makedirs(self.builddir)

    def _gather_flags(self):
        flags = self.generator.compile_flags()
        return flags

    def create_preload_file(self):
        self.create_dir()
        lines = []
        # http://stackoverflow.com/questions/17597673/cmake-preload-script-for-cache
        def _s(var, value, type):
            lines.append('set({} {} CACHE {} "")'.format(var, '"{}"'.format(value), type))
            lines.append('_pycmakedbg({})'.format(var))
            lines.append('')
        def s(var, value): _s(var, value, "STRING")
        def p(var, value): _s(var, re.sub(r'\\', '/', value), "PATH")
        def f(var, value): _s(var, re.sub(r'\\', '/', value), "FILEPATH")

        p("CMAKE_INSTALL_PREFIX", self.installdir)
        f("CMAKE_CXX_COMPILER", self.compiler.path)
        f("CMAKE_C_COMPILER", self.compiler.c_compiler)
        s("CMAKE_BUILD_TYPE", self.buildtype)
        flags = self._gather_flags()
        if flags:
            s('CMAKE_CXX_FLAGS', " ".join(flags))

        if len(lines) > 0:
            l1 = "# Do not edit. Will be overwritten."
            l2 = "# Generated by pycmake on " + datetime.now().strftime("%Y/%m/%d %H:%m")
            lines.insert(0, l1)
            lines.insert(1, l2)
            lines.insert(2, "")
            lines.insert(3, 'message(STATUS "pycmake:preload----------------------")')
            lines.insert(4, """function(_pycmakedbg var)
message(STATUS "pycmake: ${var}=${${var}}")
endfunction(_pycmakedbg)
""")
            lines.append('message(STATUS "pycmake:preload----------------------")')
            lines.append("")
            lines.append(l1)
            lines.append(l2)
        with open(self.preload_file, "w") as f:
            f.writelines([l+"\n" for l in lines])
        return self.preload_file

    def configure(self):
        self.create_dir()
        if not os.path.exists(self.preload_file):
            self.create_preload_file()
        with cwd_back(self.builddir):
            cmd = (['cmake', '-C', os.path.basename(self.preload_file),]
                   + self.generator.configure_args() +
                   [# '-DCMAKE_TOOLCHAIN_FILE='+toolchain_file,
                   self.projdir])
            runsyscmd(cmd, echo_output=True)
            with open("pycmake_configure.done", "w") as f:
                f.write(" ".join(cmd) + "\n")

    def build(self, targets = []):
        self.create_dir()
        with cwd_back(self.builddir):
            if not os.path.exists("pycmake_configure.done"):
                self.configure()
            if self.compiler.is_msvc and len(targets) == 0:
                targets = ["ALL_BUILD"]
            cmd = self.generator.cmd(targets, self)
            runsyscmd(cmd, echo_output=True)
            with open("pycmake_build.done", "w") as f:
                f.write(" ".join(cmd) + "\n")

    def install(self):
        self.create_dir()
        with cwd_back(self.builddir):
            if not os.path.exists("pycmake_build.done"):
                self.build()
            cmd = self.generator.install(self)
            print(cmd)
            runsyscmd(cmd, echo_output=True)

    def clean(self):
        self.create_dir()
        with cwd_back(self.builddir):
            cmd = self.generator.cmd(['clean'], self)
            runsyscmd(cmd, echo_output=True)
            os.remove("pycmake_build.done")


# -----------------------------------------------------------------------------
class ProjectConfig:

    # @staticmethod
    # def default_systems():
    #     return ctor(System, ["linux", "windows", "android", "ios", "ps4", "xboxone"])
    # @staticmethod
    # def default_architectures():
    #     return ctor(Architecture, ["x86", "x86_64", "arm"])
    # @staticmethod
    # def default_buildtypes():
    #     return ctor(BuildType, ["Debug", "Release"])
    # @staticmethod
    # def default_compilers():
    #     return ctor(Compiler, ["clang++", "g++", "icpc"])
    # # no default variants

    def __init__(self, **kwargs):
        projdir = kwargs.get('proj_dir', os.getcwd())
        if projdir == ".":
            projdir = os.getcwd()
        self.rootdir = projdir
        self.cmakelists = chkf(self.rootdir, "CMakeLists.txt")
        self.builddir = kwargs.get('build_dir', os.path.join(os.getcwd(), "build"))
        self.installdir = kwargs.get('install_dir', os.path.join(os.getcwd(), "install"))

        def _get(name, class_):
            g = kwargs.get(name)
            if g is None or not g:
                g = [class_.default()] if class_ is not None else [None]
                return g
            l = []
            for i in g:
                l.append(class_(i))
            return l
        self.systems = _get('systems', System)
        self.architectures = _get('architectures', Architecture)
        self.buildtypes = _get('build_types', BuildType)
        self.compilers = _get('compilers', Compiler)
        self.variants = _get('variants', None)

        #self.generator = Generator(kwargs.get('generator'))
        self.num_jobs = kwargs.get('jobs')

        configfile = os.path.join(projdir, "pycmake.json")
        self.configfile = None
        if os.path.exists(configfile):
            self.parse_file(configfile)
            self.configfile = configfile

        self.builds = []
        def _cbm(li):
            d = odict()
            for i in li:
                d[i] = []
            return d
        self.system_builds = _cbm(self.systems)
        self.architecture_builds = _cbm(self.architectures)
        self.buildtype_builds = _cbm(self.buildtypes)
        self.compiler_builds = _cbm(self.compilers)
        self.variant_builds = _cbm(self.variants)
        for s in self.systems:
            for a in self.architectures:
                for c in self.compilers:
                    for m in self.buildtypes:
                        for v in self.variants:
                            self.add_build_if_valid(s, a, m, c, v)

    def parse_file(self, configfile):
        raise Exception("not implemented")

    def add_build_if_valid(self, system, arch, buildtype, compiler, variant):
        if not self.is_valid(system, arch, buildtype, compiler, variant):
            return False
        b = Build(self.rootdir, self.builddir, self.installdir,
                  system, arch, buildtype, compiler, variant,
                  self.num_jobs)
        self.builds.append(b)
        #print(self.system_builds)
        self.system_builds[system].append(b)
        self.architecture_builds[arch].append(b)
        self.buildtype_builds[buildtype].append(b)
        self.compiler_builds[compiler].append(b)
        self.variant_builds[variant].append(b)
        return True

    def is_valid(self, sys, arch, mode, compiler, variant):
        # TODO
        return True

    def select(self, **kwargs):
        out = [b for b in self.builds]

        def _h(li, kw, attr):
            g = kwargs.get(kw)
            if g is None:
                return li
            else:
                lo = []
                for b in li:
                    if str(getattr(b, attr)) == g:
                        lo.append(b)
            return lo
        out = _h(out, "sys", "system")
        out = _h(out, "arch", "architecture")
        out = _h(out, "buildtype", "buildtype")
        out = _h(out, "compiler", "compiler")
        out = _h(out, "variant", "variant")
        return out

    def select_and_show(self, **kwargs):
        builds = self.select(**kwargs)
        if len(builds) > 0:
            print("selected builds:")
            for b in builds:
                print(b)
        else:
            print("no builds selected")
        return builds

    def show_builds(self, **kwargs):
        self.select_and_show(**kwargs)

    def create_tree(self, **restrict_to):
        builds = self.select_and_show(**restrict_to)
        for b in builds:
            b.create_dir()
            b.create_preload_file()
            # print(b, ":", d)

    def configure(self, **restrict_to):
        if not os.path.exists(self.builddir):
            os.makedirs(self.builddir)
        self._execute(Build.configure, "Configuring", **restrict_to)

    def build(self, **restrict_to):
        self._execute(Build.build, "Building", **restrict_to)

    def clean(self, **restrict_to):
        self._execute(Build.clean, "Cleaning", **restrict_to)

    def install(self, **restrict_to):
        self._execute(Build.install, "Installing", **restrict_to)

    def _execute(self, fn, msg, **restrict_to):
        builds = self.select_and_show(**restrict_to)
        num = len(builds)
        if num == 0:
            return
        print("")
        print("===============================================")
        if num > 1:
            print(msg + ": start", num, "builds")
        else:
            print(msg + ": start", builds[0])
        for i, b in enumerate(builds):
            print("-----------------------------------------------")
            if num > 1:
                print(msg + ": build #{} of {}:".format(i, num), b)
            else:
                print(msg, b)
            print("-----------------------------------------------")
            fn(b)
        print("-----------------------------------------------")
        if num > 1:
            print(msg + ": finished", num, "builds")
        else:
            print(msg + ": finished", builds[0])
        print("===============================================")

# -----------------------------------------------------------------------------
# -----------------------------------------------------------------------------
# -----------------------------------------------------------------------------
# Command line tool
# -----------------------------------------------------------------------------
# -----------------------------------------------------------------------------
# -----------------------------------------------------------------------------

cmds = odict([
    ('help', ['h']),
    ('configure', ['conf', 'c']),
    ('build', ['b']),
    ('install', ['i']),
])


def pycmake_main(in_args=None):
    '''Easily process several build trees of a CMake project'''
    if in_args is None: in_args = sys.argv[1:]
    p = argparse.ArgumentParser(prog='pycmake',
                                description=__doc__,
                                usage='%(prog)s [-h]',
                                formatter_class=argparse.RawDescriptionHelpFormatter,
                                epilog=examples)
    sp = p.add_subparsers(help='')
    for cmd,aliases in cmds.items():
        cl = cmdclass(cmd)
        h = sp.add_parser(name=cmd, aliases=aliases,
                          help=cl.__doc__, description=cl.__doc__)
        cl().add_args(h)
        def exec_cmd(args, cmd_class = cl):
            obj = cmd_class()
            proj = obj.proj(args)
            obj._exec(proj, args)
        h.set_defaults(func=exec_cmd)
    args = p.parse_args(in_args)
    if not hasattr(args, 'func'):
        argerror('missing subcommand')
    args.func(args)


def cmdclass(cmd_name):
    return getattr(sys.modules[__name__], cmd_name)


def argerror(*msg_args):
    print(*msg_args, '\n')
    pycmake_main(['-h'])
    exit(1)


def cslist(arg):
    '''transform comma-separated arguments into a list of strings'''
    return arg.split(',')


# -----------------------------------------------------------------------------
class cmdbase:
    '''base class for commands'''
    def add_args(self, parser):
        '''add arguments to a command parser'''
        pass
    def proj(self, args):
        print(args)
        '''create a project given the configuration.'''
        return ProjectConfig(**vars(args))
    def _exec(self, proj, args):
        raise Exception('never call the base class method. Implement this in derived classes')


class projcmd(cmdbase):
    '''a command which refers to a project'''
    def add_args(self, parser):
        parser.add_argument("proj-dir", nargs="?", default=".",
                            help="""the directory where CMakeLists.txt is located. An empty argument
                            will default to the current directory ie, \".\". Passing a directory
                            which does not contain a CMakeLists.txt will cause an error.""")
        parser.add_argument("--build-dir", default="./build",
                            help="set the build root (defaults to ./build)")
        parser.add_argument("--install-dir", default="./install",
                            help="set the install root (defaults to ./install)")
        parser.add_argument("-j", "--jobs", default=cpu_count(),
                            help="""build with the given number of parallel jobs
                            (defaults to %(default)s on this machine).""")
        parser.add_argument("-G", "--generator", default=Generator.default_str(),
                            help="set the cmake generator (on this machine, defaults to \"%(default)s\")")
        parser.add_argument("-C", "--compiler-flags", default=Generator.default_str(),
                            help="set the cmake generator (on this machine, defaults to \"%(default)s\")")


class selectcmd(projcmd):
    '''a command which selects several builds'''
    def add_args(self, parser):
        super().add_args(parser)
        g = parser.add_argument_group(title="Selecting the builds")
        g.add_argument("-t", "--build-types", metavar="type1,type2,...", default=["Release"], type=cslist,
                       help="""restrict actions to the given build types.
                       Defaults to \"%(default)s\".""")
        g.add_argument("-c", "--compilers", metavar="compiler1,compiler2,...",
                       default=[Compiler.default_str()], type=cslist,
                       help="""restrict actions to the given compilers.
                       Defaults to CMake's default compiler, \"%(default)s\" on this system.""")
        g.add_argument("-s", "--systems", metavar="os1,os2,...", default=[System.default_str()], type=cslist,
                       help="""(WIP) restrict actions to the given operating systems.
                       Defaults to the current system, \"%(default)s\".
                       This feature requires os-specific toolchains and is currently a
                       work-in-progress.""")
        g.add_argument("-a", "--architectures", metavar="arch1,arch2,...",
                       default=[Architecture.default_str()], type=cslist,
                       help="""(WIP) restrict actions to the given processor architectures.
                       Defaults to CMake's default architecture, \"%(default)s\" on this system.
                       This feature requires os-specific toolchains and is currently a
                       work-in-progress.""")
        g.add_argument("-v", "--variants", metavar="variant1,variant2,...", default=[], type=cslist,
                       help="""(WIP) restrict actions to the given variants.
                       This feature is currently a work-in-progress.""")


class help(projcmd):
    '''get help on a particular subcommand or topic'''
    def add_args(self, parser):
        parser.add_argument('subcommand', default="")
    def _exec(self, proj, args):
        if not hasattr(args, 'subcommand'):
            pycmake_main(['-h'])
        else:
            sc = cmds.get(args.subcommand)
            if sc is not None:
                pycmake_main([sc, '-h'])
            else:
                subtopic = help_topics.get(args.subcommand)
                if subtopic is None:
                    argerror("unknown subcommand:", args.subcommand)
                if isinstance(subtopic, str):
                    print(subtopic)
                else:
                    h = subtopic()
                    print(h)


class configure(selectcmd):
    '''configure the selected builds'''
    def _exec(self, proj, args):
        proj.configure()


class build(selectcmd):
    '''build the selected builds, configuring before if necessary'''
    def _exec(self, proj, args):
        proj.build()


class install(selectcmd):
    '''install the selected builds, building before if necessary'''
    def _exec(self, proj, args):
        proj.install()


# ------------------------------------------------------------------------------


# to update the examples in a Markdown file, pipe the help through sed:
# sed 's:^#\ ::g' | sed 's:^\$\(\ .*\):\n```\n$ \1\n```:g'
examples = '''
-----------------------------
Some examples:

# Configure and build a CMakeLists.txt project located on the folder above
# the current one. The build trees will be placed in separate folders under
# a folder named "build" located on the current folder. Likewise, the installation
# prefix will be set to a sister folder named "install". A c++ compiler will
# be selected from the system, and the CMAKE_BUILD_TYPE will be set to Release.
$ %(prog)s build ..

# Same as above, but now look for CMakeLists.txt on the current dir.
$ %(prog)s build .

# Same as above: like with cmake, omitting the project dir defaults will cause
# searching for CMakeLists.txt on the current dir.
$ %(prog)s build

# Same as above: 'b' can be used as an alias to 'install'.
$ %(prog)s b

# Same as above, and additionally install. 'i' can be used as an alias to 'install'.
$ %(prog)s i

# Only configure; do not build, do not install. 'conf' and 'c' are aliases to 'configure'.
$ %(prog)s c

# Build only the Debug build type.
$ %(prog)s b -t Debug

# Build both Debug and Release build types (resulting in 2 build trees).
$ %(prog)s b -t Debug,Release

# Build using both clang++ and g++ (2 build trees).
$ %(prog)s b -c clang++,g++

# Build using both clang++,g++ and in Debug,Release modes (4 build trees).
$ %(prog)s b -c clang++,g++ -t Debug,Release

# Build using clang++,g++,icpc in Debug,Release,MinSizeRel modes (9 build trees).
$ %(prog)s b -c clang++,g++,icpc -t Debug,Release,MinSizeRel
'''

help_topics = {
    'variants':'''
help on variants
''',

    'compiler_flags':'''
help on preset compiler flags
''',

    'visual_studio':'''
visual studio topic help
''',
}


# -----------------------------------------------------------------------------

if __name__ == "__main__":

    pycmake_main(sys.argv[1:])
