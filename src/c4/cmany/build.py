import os
import copy
import glob
from datetime import datetime

from .generator import Generator
from . import util
from .cmake import CMakeCache, CMakeSysInfo

# experimental, don't think it will stay unless conan starts accepting args
from .conan import Conan


# -----------------------------------------------------------------------------
class Build:
    """Holds a build's settings"""

    pfile = "cmany_preload.cmake"

    def __init__(self, proj_root, build_root, install_root,
                 system, arch, buildtype, compiler, variant, flags,
                 num_jobs, kwargs):
        #
        self.kwargs = kwargs
        #
        self.projdir = util.chkf(proj_root)
        self.buildroot = os.path.abspath(build_root)
        self.installroot = os.path.abspath(install_root)
        #
        self.flags = flags
        self.system = system
        self.architecture = arch
        self.buildtype = buildtype
        self.compiler = compiler
        self.variant = variant

        self.adjusted = False

        self._set_paths()
        # WATCHOUT: this may trigger a readjustment of this build's parameters
        self.generator = Generator.create(self, num_jobs)

        self.flags.resolve_flag_aliases(self.compiler)
        self.system.flags.resolve_flag_aliases(self.compiler)
        self.architecture.flags.resolve_flag_aliases(self.compiler)
        self.buildtype.flags.resolve_flag_aliases(self.compiler)
        self.compiler.flags.resolve_flag_aliases(self.compiler)
        self.variant.flags.resolve_flag_aliases(self.compiler)

        # This will load the vars from the builddir cache, if it exists.
        # It should be done only after creating the generator.
        self.varcache = CMakeCache(self.builddir)
        # ... and this will overwrite (in memory) the vars with the input
        # arguments. This will make the cache dirty and so we know when it
        # needs to be committed back to CMakeCache.txt
        self.gather_input_cache_vars()

        self.deps = kwargs.get('deps', '')
        if self.deps and not os.path.isabs(self.deps):
            self.deps = os.path.abspath(self.deps)
        self.deps_prefix = kwargs.get('deps_prefix')
        if self.deps_prefix and not os.path.isabs(self.deps_prefix):
            self.deps_prefix = os.path.abspath(self.deps_prefix)
        if not self.deps_prefix:
            self.deps_prefix = self.builddir

    def _set_paths(self):
        self.tag = self._cat('-')
        self.buildtag = self.tag
        self.installtag = self.tag  # this was different in the past and may become so in the future
        self.builddir = os.path.abspath(os.path.join(self.buildroot, self.buildtag))
        self.installdir = os.path.join(self.installroot, self.installtag)
        self.preload_file = os.path.join(self.builddir, Build.pfile)
        self.cachefile = os.path.join(self.builddir, 'CMakeCache.txt')

    def adjust(self, **kwargs):
        a = kwargs.get('architecture')
        if a and a != self.architecture:
            self.adjusted = True
            self.architecture = a
        c = kwargs.get('compiler')
        if c and c != self.compiler:
            self.adjusted = True
            self.compiler = c
        self._set_paths()

    def __repr__(self):
        return self.tag

    def _cat(self, sep):
        s = "{1}{0}{2}{0}{3}{0}{4}"
        s = s.format(sep, self.system, self.architecture, self.compiler, self.buildtype)
        if self.variant and self.variant.name and self.variant.name != "none":
            s += "{0}{1}".format(sep, self.variant)
        return s

    def create_dir(self):
        if not os.path.exists(self.builddir):
            os.makedirs(self.builddir)

    def configure_cmd(self, for_json=False):
        if for_json:
            return ('-C ' + self.preload_file
                    + ' ' + self.generator.configure_args(for_json))
        cmd = (['cmake', '-C', self.preload_file]
               + self.generator.configure_args())
        if self.kwargs.get('export_compile', False):
            cmd.append('-DCMAKE_EXPORT_COMPILE_COMMANDS=1')
        cmd += [  # '-DCMAKE_TOOLCHAIN_FILE='+toolchain_file,
            self.projdir]
        return cmd

    def configure(self):
        self.create_dir()
        self.create_preload_file()
        self.handle_deps()
        if self.needs_cache_regeneration():
            self.varcache.commit(self.builddir)
        with util.setcwd(self.builddir, silent=False):
            cmd = self.configure_cmd()
            util.runsyscmd(cmd)
            self.mark_configure_done(cmd)

    def mark_configure_done(self, cmd):
        with util.setcwd(self.builddir):
            with open("cmany_configure.done", "w") as f:
                f.write(" ".join(cmd) + "\n")

    def needs_configure(self):
        if not os.path.exists(self.builddir):
            return True
        with util.setcwd(self.builddir):
            if not os.path.exists("cmany_configure.done"):
                return True
            if self.needs_cache_regeneration():
                return True
        return False

    def needs_cache_regeneration(self):
        if os.path.exists(self.cachefile) and self.varcache.dirty:
            return True
        return False

    def build(self, targets=[]):
        self.create_dir()
        with util.setcwd(self.builddir, silent=False):
            if self.needs_configure():
                self.configure()
            self.handle_deps()
            if len(targets) == 0:
                if self.compiler.is_msvc:
                    targets = ["ALL_BUILD"]
                else:
                    targets = ["all"]
            # cmake --build and visual studio won't handle
            # multiple targets at once, so loop over them.
            for t in targets:
                cmd = self.generator.cmd([t])
                util.runsyscmd(cmd)
            # this was written before using the loop above.
            # it can come to fail in some corner cases.
            self.mark_build_done(cmd)

    def mark_build_done(self, cmd):
        with util.setcwd(self.builddir):
            with open("cmany_build.done", "w") as f:
                f.write(" ".join(cmd) + "\n")

    def needs_build(self):
        if not os.path.exists(self.builddir):
            return True
        with util.setcwd(self.builddir):
            if not os.path.exists("cmany_build.done"):
                return True
            if self.needs_cache_regeneration():
                return True
        return False

    def install(self):
        self.create_dir()
        with util.setcwd(self.builddir, silent=False):
            if self.needs_build():
                self.build()
            cmd = self.generator.install()
            print(cmd)
            util.runsyscmd(cmd)

    def clean(self):
        self.create_dir()
        with util.setcwd(self.builddir):
            cmd = self.generator.cmd(['clean'])
            util.runsyscmd(cmd)
            os.remove("cmany_build.done")

    def _get_flagseq(self):
        return (
            self.flags,
            self.system.flags,
            self.architecture.flags,
            self.compiler.flags,
            self.buildtype.flags,
            self.variant.flags
        )

    def _gather_flags(self, which, append_to_sysinfo_var=None, with_defines=False):
        flags = []
        if append_to_sysinfo_var:
            try:
                flags = [CMakeSysInfo.var(append_to_sysinfo_var, self.generator)]
            except:
                pass
        # append overall build flags
        # append variant flags
        flagseq = self._get_flagseq()
        for fs in flagseq:
            wf = getattr(fs, which)
            for f in wf:
                r = f.get(self.compiler)
                flags.append(r)
            if with_defines:
                flags += fs.defines
        # we're done
        return flags

    def _gather_cmake_vars(self):
        flagseq = self._get_flagseq()
        for fs in flagseq:
            for v in fs.cmake_vars:
                spl = v.split('=')
                vval = ''.join(spl[1:]) if len(spl) > 1 else ''
                nspl = spl[0].split(':')
                if len(nspl) == 1:
                    self.varcache.setvar(nspl[0], vval, from_input=True)
                elif len(nspl) == 2:
                    self.varcache.setvar(nspl[0], vval, nspl[1], from_input=True)
                else:
                    raise Exception('could not parse variable value: ' + v)

    def gather_input_cache_vars(self):
        self._gather_cmake_vars()
        vc = self.varcache
        #
        def _set(pfn, pname, pval): pfn(pname, pval, from_input=True)
        if not self.generator.is_msvc:
            _set(vc.f, 'CMAKE_C_COMPILER', self.compiler.c_compiler)
            _set(vc.f, 'CMAKE_CXX_COMPILER', self.compiler.path)
        _set(vc.s, 'CMAKE_BUILD_TYPE', str(self.buildtype))
        _set(vc.p, 'CMAKE_INSTALL_PREFIX', self.installdir)
        #
        cflags = self._gather_flags('cflags', 'CMAKE_C_FLAGS_INIT', with_defines=True)
        if cflags:
            _set(vc.s, 'CMAKE_C_FLAGS', ' '.join(cflags))
        #
        cxxflags = self._gather_flags('cxxflags', 'CMAKE_CXX_FLAGS_INIT', with_defines=True)
        if cxxflags:
            _set(vc.s, 'CMAKE_CXX_FLAGS', ' '.join(cxxflags))
        #
        # if self.flags.include_dirs:
        #     _set(vc.s, 'CMANY_INCLUDE_DIRECTORIES', ';'.join(self.flags.include_dirs))
        #
        # if self.flags.link_dirs:
        #     _set(vc.s, 'CMAKE_LINK_DIRECTORIES', ';'.join(self.flags.link_dirs))
        #

    def create_preload_file(self):
        # http://stackoverflow.com/questions/17597673/cmake-preload-script-for-cache
        self.create_dir()
        lines = []
        s = '_cmany_set({} "{}" {})'
        for _, v in self.varcache.items():
            if v.from_input:
                lines.append(s.format(v.name, v.val, v.vartype))
        if lines:
            tpl = __class__.preload_file_tpl
        else:
            tpl = __class__.preload_file_tpl_empty
        now = datetime.now().strftime("%Y/%m/%d %H:%m")
        txt = tpl.format(date=now, vars="\n".join(lines))
        with open(self.preload_file, "w") as f:
            f.write(txt)
        return self.preload_file

    preload_file_tpl = ("""# Do not edit. Will be overwritten.
# Generated by cmany on {date}

if(NOT _cmany_set_def)
    set(_cmany_set_def ON)
    function(_cmany_set var value type)
        set(${{var}} "${{value}}" CACHE ${{type}} "")
        message(STATUS "cmany: ${{var}}=${{value}}")
    endfunction(_cmany_set)
endif(NOT _cmany_set_def)

message(STATUS "cmany:preload----------------------")
{vars}
message(STATUS "cmany:preload----------------------")
""" +
# """
# if(CMANY_INCLUDE_DIRECTORIES)
#     include_directories(${{CMANY_INCLUDE_DIRECTORIES}})
# endif()
#
# if(CMANY_LINK_DIRECTORIES)
#     link_directories(${{CMANY_LINK_DIRECTORIES}})
# endif()
# """ +
"""
# Do not edit. Will be overwritten.
# Generated by cmany on {date}
""")
    preload_file_tpl_empty = """# Do not edit. Will be overwritten.
# Generated by cmany on {date}

message(STATUS "cmany: nothing to preload...")
"""

    @property
    def deps_done(self):
        dmark = os.path.join(self.builddir, "cmany_deps.done")
        exists = os.path.exists(dmark)
        return exists

    def mark_deps_done(self):
        with util.setcwd(self.builddir):
            with open("cmany_deps.done", "w") as f:
                s = ''
                if self.deps: s += self.deps + '\n'
                if self.deps_prefix: s += self.deps_prefix + '\n'
                f.write(s)

    def handle_deps(self):
        if self.deps_done:
            return
        if not self.deps:
            self.handle_conan()
            self.mark_deps_done()
            return
        util.lognotice(self.tag + ': building dependencies', self.deps)
        dup = copy.copy(self)
        dup.builddir = os.path.join(self.builddir, 'cmany_deps-build')
        dup.installdir = self.deps_prefix
        util.logwarn('installdir:', dup.installdir)
        dup.projdir = self.deps
        dup.preload_file = os.path.join(self.builddir, self.preload_file)
        dup.deps = None
        dup.generator.build = dup
        dup.configure()
        dup.build()
        try:
            # if the dependencies cmake project is purely consisted of
            # external projects, there won't be an install target.
            dup.install()
        except:
            pass
        util.logdone(self, ': building dependencies: done')
        util.logwarn('installdir:', dup.installdir)
        self.varcache.p('CMAKE_PREFIX_PATH', self.installdir)
        self.mark_deps_done()

    def handle_conan(self):
        if not self.kwargs.get('with_conan'):
            return
        doit = False
        f = None
        for fn in ('conanfile.py', 'conanfile.txt'):
            f = os.path.join(self.projdir, fn)
            cf = os.path.join(self.builddir, 'conanbuildinfo.cmake')
            if os.path.exists(f) and not os.path.exists(cf):
                doit = True
                break
        if not doit:
            return
        util.logdone('found conan file')
        c = Conan()
        c.install(self)

    def json_data(self):
        """
        https://blogs.msdn.microsoft.com/vcblog/2016/11/16/cmake-support-in-visual-studio-the-visual-studio-2017-rc-update/
        https://blogs.msdn.microsoft.com/vcblog/2016/12/20/cmake-support-in-visual-studio-2017-whats-new-in-the-rc-update/
        """
        builddir = self.builddir.replace(self.projdir, '${projectDir}')
        builddir = re.sub(r'\\', r'/', builddir)
        return odict([
            ('name', self.tag),
            ('generator', self.generator.name),
            ('configurationType', self.buildtype.name),
            ('buildRoot', builddir),
            ('cmakeCommandArgs', self.configure_cmd(for_json=True)),
            # ('variables', []),  # this is not needed since the vars are set in the preload file
        ])

    def get_targets(self):
        with util.setcwd(self.builddir):
            if self.generator.is_msvc:
                # each target in MSVC has a corresponding vcxproj file
                files = glob.glob(".", "*.vcxproj")
                files = [os.path.basename(f) for f in files]
                files = [os.path.splitext(f)[0] for f in files]
                return files
            elif self.generator.is_makefile:
                output = util.runsyscmd(["make", "help"], echo_cmd=False,
                                        echo_output=False, capture_output=True)
                output = output.split("\n")
                output = output[1:]  # The following are some of the valid targets....
                output = [o[4:] for o in output]  # take off the initial "... "
                output = [re.sub(r'(.*)\ \(the default if no target.*\)', r'\1', o) for o in output]
                output = sorted(output)
                result = []
                for o in output:
                    if o:
                        result.append(o)
                return result
            else:
                util.logerr("sorry, feature not implemented for this generator: " +
                            str(self.generator))