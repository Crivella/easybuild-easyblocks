##
# Copyright 2013-2024 Dmitri Gribenko
# Copyright 2013-2024 Ghent University
#
# This file is triple-licensed under GPLv2 (see below), MIT, and
# BSD three-clause licenses.
#
# This file is part of EasyBuild,
# originally created by the HPC team of Ghent University (http://ugent.be/hpc/en),
# with support of Ghent University (http://ugent.be/hpc),
# the Flemish Supercomputer Centre (VSC) (https://www.vscentrum.be),
# Flemish Research Foundation (FWO) (http://www.fwo.be/en)
# and the Department of Economy, Science and Innovation (EWI) (http://www.ewi-vlaanderen.be/en).
#
# https://github.com/easybuilders/easybuild
#
# EasyBuild is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation v2.
#
# EasyBuild is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with EasyBuild.  If not, see <http://www.gnu.org/licenses/>.
##
"""
Support for building and installing Clang, implemented as an easyblock.

@author: Dmitri Gribenko (National Technical University of Ukraine "KPI")
@author: Ward Poelmans (Ghent University)
@author: Alan O'Cais (Juelich Supercomputing Centre)
@author: Maxime Boissonneault (Digital Research Alliance of Canada, Universite Laval)
@author: Davide Grassano (CECAM HQ - Lausanne)
"""

import glob
import os
import re
import shutil

from easybuild.framework.easyconfig import CUSTOM
from easybuild.toolchains.compiler.clang import Clang
from easybuild.tools import LooseVersion, run
from easybuild.tools.build_log import EasyBuildError, print_warning
from easybuild.tools.config import build_option
from easybuild.tools.environment import setvar
from easybuild.tools.filetools import (apply_regex_substitutions, change_dir,
                                       mkdir, symlink, which)
from easybuild.tools.modules import get_software_root
from easybuild.tools.run import run_cmd
from easybuild.tools.systemtools import (AARCH32, AARCH64, POWER, RISCV64,
                                         X86_64, get_cpu_architecture,
                                         get_os_name, get_os_version,
                                         get_shared_lib_ext)

from easybuild.easyblocks.generic.cmakemake import CMakeMake

# List of all possible build targets for Clang
CLANG_TARGETS = ["all", "AArch64", "AMDGPU", "ARM", "CppBackend", "Hexagon", "Mips",
                 "MBlaze", "MSP430", "NVPTX", "PowerPC", "R600", "RISCV", "Sparc",
                 "SystemZ", "X86", "XCore"]

# Mapping of EasyBuild CPU architecture names to list of default LLVM target names
DEFAULT_TARGETS_MAP = {
    AARCH32: ['ARM'],
    AARCH64: ['AArch64'],
    POWER: ['PowerPC'],
    RISCV64: ['RISCV'],
    X86_64: ['X86'],
}

# List of all possible AMDGPU gfx targets supported by LLVM
AMDGPU_GFX_SUPPORT = ['gfx700', 'gfx701', 'gfx801', 'gfx803', 'gfx900',
                      'gfx902', 'gfx906', 'gfx908', 'gfx90a', 'gfx90c',
                      'gfx1010', 'gfx1030', 'gfx1031']

# List of all supported CUDA toolkit versions supported by LLVM
CUDA_TOOLKIT_SUPPORT = ['80', '90', '91', '92', '100', '101', '102', '110', '111', '112']


# When extending the lists below, make sure to add additional sanity checks!
# List of the known LLVM projects
KNOWN_LLVM_PROJECTS = ['llvm', 'clang', 'polly', 'lld', 'lldb', 'clang-tools-extra', 'flang', 'mlir']
# List of the known LLVM runtimes
KNOWN_LLVM_RUNTIMES = ['compiler-rt', 'libunwind', 'libcxx', 'libcxxabi', 'openmp']

class EB_LLVMcomponent(CMakeMake):
    """Support for building LLVM standalone projects."""

    @staticmethod
    def extra_options():
        extra_vars = CMakeMake.extra_options()
        extra_vars.update({
            'amd_gfx_list': [None, "List of AMDGPU targets to build for. Possible values: " +
                             ', '.join(AMDGPU_GFX_SUPPORT), CUSTOM],
            'assertions': [True, "Enable assertions.  Helps to catch bugs in Clang.", CUSTOM],
            'bootstrap': [False, "Bootstrap Clang using GCC", CUSTOM],
            # 'build_extra_clang_tools': [False, "Build extra Clang tools", CUSTOM],
            'build_targets': [None, "Build targets for LLVM (host architecture if None). Possible values: " +
                                    ', '.join(CLANG_TARGETS), CUSTOM],
            'default_cuda_capability': [None, "Default CUDA capability specified for clang, e.g. '7.5'", CUSTOM],
            'default_openmp_runtime': [None, "Default OpenMP runtime for clang (for example, 'libomp')", CUSTOM],
            'enable_rtti': [False, "Enable Clang RTTI", CUSTOM],
            'python_bindings': [False, "Install python bindings", CUSTOM],
            'skip_all_tests': [False, "Skip running of tests", CUSTOM],
            # 'static_analyzer': [True, "Install the static analyser of Clang", CUSTOM],
            # The sanitizer tests often fail on HPC systems due to the 'weird' environment.
            # 'skip_sanitizer_tests': [True, "Do not run the sanitizer tests", CUSTOM],
            'test_suite_max_failed': [0, "Maximum number of failing tests (does not count allowed failures)", CUSTOM],
        })
        # disable regular out-of-source build, too simplistic for Clang to work
        extra_vars['separate_build_dir'][0] = False
        return extra_vars

    def __init__(self, *args, **kwargs):
        """Initialize custom class variables for Clang."""
        super(EB_LLVMcomponent, self).__init__(*args, **kwargs)

        if LooseVersion(self.version) < LooseVersion('18.1.6'):
            raise EasyBuildError("LLVM version %s is not supported, please use version 18.1.6 or newer", self.version)

        self.llvm_src_dir = None
        self.llvm_obj_dir_stage1 = None
        self.llvm_obj_dir_stage2 = None
        self.llvm_obj_dir_stage3 = None
        self.test_targets = []
        self.check_files = []
        self.check_dirs = []
        self.check_commands = []
        self.make_parallel_opts = ""
        self.runtime_lib_path = "lib"

        if self.cfg['bootstrap'] and self.name.lower() != 'clang':
            raise EasyBuildError("Bootstrapping only supported for Clang")

        # Ensure version of Python3 found is from EB and not a virtualenv
        # https://cmake.org/cmake/help/latest/module/FindPython3.html
        self.cfg.update('configopts', "-DPython3_FIND_VIRTUALENV=STANDARD")

        build_targets = self.cfg['build_targets']
        # define build_targets if not set
        if build_targets is None:
            deps = [dep['name'].lower() for dep in self.cfg.dependencies()]
            arch = get_cpu_architecture()
            try:
                default_targets = DEFAULT_TARGETS_MAP[arch][:]
                # If CUDA is included as a dep, add NVPTX as a target
                # There are (old) toolchains with CUDA as part of the toolchain
                cuda_toolchain = hasattr(self.toolchain, 'COMPILER_CUDA_FAMILY')
                if 'cuda' in deps or cuda_toolchain:
                    default_targets += ['NVPTX']
                # For AMDGPU support we need ROCR-Runtime and
                # ROCT-Thunk-Interface, however, since ROCT is a dependency of
                # ROCR we only check for the ROCR-Runtime here
                # https://openmp.llvm.org/SupportAndFAQ.html#q-how-to-build-an-openmp-amdgpu-offload-capable-compiler
                if 'rocr-runtime' in deps:
                    default_targets += ['AMDGPU']
                self.cfg['build_targets'] = build_targets = default_targets
                self.log.debug("Using %s as default build targets for CPU/GPU architecture %s.", default_targets, arch)
            except KeyError:
                raise EasyBuildError("No default build targets defined for CPU architecture %s.", arch)

        # carry on with empty list from this point forward if no build targets are specified
        if build_targets is None:
            self.cfg['build_targets'] = build_targets = []

        unknown_targets = [target for target in build_targets if target not in CLANG_TARGETS]

        if unknown_targets:
            raise EasyBuildError("Some of the chosen build targets (%s) are not in %s.",
                                 ', '.join(unknown_targets), ', '.join(CLANG_TARGETS))

        if "MBlaze" in build_targets:
            raise EasyBuildError("Build target MBlaze is not supported anymore in > Clang-3.3")

        name = self.name.lower()
        if name.startswith('llvm'):
            name = name[4:]
        self.project_name = name
        self.srcdir = os.path.join(self.builddir, name)

    def extract_step(self):
        """Rename extracted directories to match expected directory name."""
        super(EB_LLVMcomponent, self).extract_step()
        rgx = re.compile(r'(.*)-%s\.src' % self.version)
        for dname in glob.glob('*-%s.src' % self.version):
            m = rgx.match(dname)
            if m:
                shutil.move(dname, m.group(1))

    def patch_step(self, beginpath=None, patches=None):
        """Patch step."""
        super(EB_LLVMcomponent, self).patch_step(beginpath=self.builddir, patches=patches)

    def prepare_step(self, start_dir=True, load_tc_deps_modules=True):
        """Prepare step."""
        super(EB_LLVMcomponent, self).prepare_step(start_dir=False, load_tc_deps_modules=load_tc_deps_modules)

    def check_readiness_step(self):
        """Fail early on RHEL 5.x and derivatives because of known bug in libc."""
        super(EB_LLVMcomponent, self).check_readiness_step()
        # RHEL 5.x have a buggy libc.  Building stage 2 will fail.
        if get_os_name() in ['redhat', 'RHEL', 'centos', 'SL'] and get_os_version().startswith('5.'):
            raise EasyBuildError("Can not build Clang on %s v5.x: libc is buggy, building stage 2 will fail. "
                                 "See http://stackoverflow.com/questions/7276828/", get_os_name())

    def _clang_config(self):
        """Configurations specific to Clang."""
        runtimes_root = get_software_root('LLVMruntimes')
        if runtimes_root is None:
            raise EasyBuildError("LLVMruntimes is required to build Clang")
        lld_root = get_software_root('lld')
        if lld_root is None:
            raise EasyBuildError("lld is required to build Clang")
        compiler_rt_root = get_software_root('compiler-rt')
        if compiler_rt_root is None:
            raise EasyBuildError("compiler-rt is required to build Clang")

        if self.cfg["enable_rtti"] and self.name.lower() == 'clang':
            self.cfg.update('configopts', '-DLLVM_REQUIRES_RTTI=ON')
            self.cfg.update('configopts', '-DLLVM_ENABLE_RTTI=ON')
            self.cfg.update('configopts', '-DLLVM_ENABLE_EH=ON')
        if self.cfg["default_openmp_runtime"]:
            self.cfg.update(
                'configopts',
                '-DCLANG_DEFAULT_OPENMP_RUNTIME=%s' % self.cfg["default_openmp_runtime"]
            )

        ld_driver = 'ld.lld'
        # self.cfg.update('configopts', "-DCLANG_DEFAULT_LINKER=%s" % os.path.join(lld_root, 'bin', ld_driver))
        self.cfg.update('configopts', "-DCLANG_DEFAULT_LINKER=lld")
        # self.cfg.update('configopts', "-DCLANG_DEFAULT_CXX_STDLIB=%s" % os.path.join(runtimes_root, 'libcxx'))
        self.cfg.update('configopts', "-DCLANG_DEFAULT_CXX_STDLIB=libc++")
        self.cfg.update('configopts', "-DCLANG_DEFAULT_RTLIB=compiler-rt")
        self.cfg.update('configopts', "-DLLVM_BUILD_UTILS=ON") # Required for clang-tblgen

        if not which('llvm-gtest'):
            self.log.warn("llvm-gtest not found, disabling tests")
            self.cfg.update('configopts', "-DLLVM_INCLUDE_TESTS=OFF")
        else:
            self.cfg.update('configopts', "-DLLVM_INCLUDE_TESTS=ON")
            self.test_targets = ['check-clang']

        # GCC and Clang are installed in different prefixes and Clang will not
        # find the GCC installation on its own.
        # First try with GCCcore, as GCC built on top of GCCcore is just a wrapper for GCCcore and binutils,
        # instead of a full-fledge compiler
        gcc_prefix = get_software_root('GCCcore')
        # If that doesn't work, try with GCC
        if gcc_prefix is None:
            gcc_prefix = get_software_root('GCC')
        # If that doesn't work either, print error and exit
        if gcc_prefix is None:
            raise EasyBuildError("Can't find GCC or GCCcore to use")
        self.cfg.update('configopts', "-DGCC_INSTALL_PREFIX='%s'" % gcc_prefix)
        self.log.debug("Using %s as GCC_INSTALL_PREFIX", gcc_prefix)

        self.check_files += [
            'bin/%s' % f for f in [
                'clang', 'clang++', 'clang-cpp', 'clang-cl', 'clang-repl', 'hmaptool', 'amdgpu-arch', 'nvptx-arch',
                'intercept-build', 'scan-build', 'scan-build-py', 'scan-view', 'analyze-build', 'c-index-test',
                'clang-tblgen',
            ]] + [
                'lib/%s' % f for f in [
                    'libclang.so', 'libclang-cpp.so', 'libclangAST.a', 'libclangCrossTU.a', 'libclangFrontend.a',
                    'libclangInterpreter.a', 'libclangParse.a', 'libclangTooling.a'
                ]
            ]
        self.check_dirs += [
            'lib/cmake/clang', 'lib/libear', 'lib/libscanbuild', 'include/clang'
        ]
        self.check_commands += [
            ' | '.join([
                'echo \'int main(int argc, char **argv) { return 1; }\'',
                'clang -xc -fplugin="$EBROOTPOLLY/lib/LLVMPolly.so" -O3 -mllvm -polly -'
            ])
        ]

        cte_dir = os.path.join(self.builddir, 'clang-tools-extra')
        if os.path.exists(cte_dir):
            self.log.info("Building Clang with extra tools")
            self.cfg.update('configopts', "-DLLVM_TOOL_CLANG_TOOLS_EXTRA_BUILD=ON")
            self.cfg.update('configopts', "-DLLVM_EXTERNAL_CLANG_TOOLS_EXTRA_SOURCE_DIR=%s" % cte_dir)
            self.check_files += [
                'bin/%s' % f for f in [
                    'clangd', 'clang-tidy', 'clang-pseudo', 'clang-include-fixer', 'clang-query', 'clang-move',
                    'clang-reorder-fields', 'clang-include-cleaner', 'clang-apply-replacements',
                    'clang-change-namespace', 'pp-trace', 'modularize'
                ]
            ] + [
                'lib/%s' % f for f in [
                    'libclangTidy.a', 'libclangQuery.a', 'libclangIncludeFixer.a', 'libclangIncludeCleaner.a',
                ]
            ]

    def _flang_config(self):
        """Configurations specific to Flang."""
        lld_root = get_software_root('lld')
        if lld_root is None:
            raise EasyBuildError("lld is required to build Clang")
        compiler_rt_root = get_software_root('compiler-rt')
        if compiler_rt_root is None:
            raise EasyBuildError("compiler-rt is required to build Clang")

        self.cfg.update('configopts', "-DFLANG_DEFAULT_LINKER=lld")
        self.cfg.update('configopts', "-DFLANG_DEFAULT_RTLIB=compiler-rt")

        self.check_files += [
            'bin/%s' % f for f in [
                'bbc', 'flang-new', 'flang-to-external-fc', 'f18-parse-demo', 'fir-opt', 'tco'
            ]
        ] + [
            'lib/%s' % f for f in [
                'libFortranRuntime.a', 'libFortranSNemantics.a', 'libFortranLower.a', 'libFortranParser.a',
                'libFIRCodeGen.a', 'libflangFrontend.a', 'libFortranCommon.a', 'libFortranDecimal.a',
                'libHLFIRDialect.a'
            ]
        ]

    def _polly_config(self):
        """Configurations specific to Polly."""
        # self.cfg.update('configopts', "-DLLVM_POLLY_LINK_INTO_TOOLS=ON")
        self.test_targets = ['check-polly']

        self.check_files += ['lib/LLVMPolly.so', 'lib/libPolly.a', 'lib/libPollyISL.a']
        self.check_dirs += ['lib/cmake/polly', 'include/polly']

        build_targets = self.cfg['build_targets']
        if "NVPTX" in build_targets:
            self.cfg.update('configopts', "-DPOLLY_ENABLE_GPGPU_CODEGEN=ON")

    def _mlir_config(self):
        """Configurations specific to MLIR."""
        self.cfg.update('configopts', "-DLLVM_BUILD_UTILS=ON")
        self.cfg.update('configopts', "-DMLIR_INCLUDE_TESTS=On")
        # self.cfg.update('configopts', "-DMLIR_INCLUDE_INTEGRATION_TESTS=On")

        self.test_targets = ['check-mlir']

        self.check_files += [
            'bin/mlir-tblgen', 'bin/tblgen-to-irdl', 'bin/mlir-pdll',
            'lib/libMLIRIR.a', 'lib/libmlir_async_runtime.so', 'lib/libmlir_arm_runner_utils.so',
            'lib/libmlir_c_runner_utils.so', 'lib/libmlir_float16_utils.so'
        ]
        self.check_dirs += ['lib/cmake/mlir', 'include/mlir', 'include/mlir-c']

        if self.cfg['python_bindings']:
            self.cfg.update('configopts', "-DMLIR_ENABLE_BINDINGS_PYTHON=ON")


    def _runtimes_config(self):
        """Configurations specific to LLVM runtimes."""
        self.test_targets = [
            'check-runtimes', 'check-unwind', 'check-cxx', 'check-cxxabi',
            # 'check-cxx-abilist'
        ]

        self.cfg.update('configopts', "-DLLVM_ENABLE_RUNTIMES='libunwind;libcxxabi;libcxx'")
        # Requires connection to work
        self.cfg.update('configopts', "-DLIBCXX_INCLUDE_BENCHMARKS=OFF")
        # Required to avoid -nostdlib++ flag with GCC
        self.cfg.update('configopts', "-DCXX_SUPPORTS_NOSTDLIBXX_FLAG=OFF")

        compiler_rt_root = get_software_root('compiler-rt')
        if compiler_rt_root is None:
            raise EasyBuildError("compiler-rt is required to build LLVMruntimes")

        self.cfg.update('configopts', "-DLIBCXX_CXX_ABI=libcxxabi")
        self.cfg.update('configopts', "-DLIBCXX_USE_COMPILER_RT=On")
        self.cfg.update('configopts', "-DLIBCXXABI_USE_LLVM_UNWINDER=On")
        self.cfg.update('configopts', "-DLIBCXXABI_USE_COMPILER_RT=On")
        # self.cfg.update('configopts', "-DLIBUNWIND_USE_COMPILER_RT=On")
        # self.cfg.update('configopts', "-DCOMPILER_RT_USE_LLVM_UNWINDER=On")

        self.check_files += [
            'lib/%s' % f for f in [
                'libunwind.a', 'libc++.a', 'libc++abi.a', 'libunwind.so', 'libc++.so', 'libc++abi.so'
            ]
        ] + [
            'include/%s' % f for f in [
                'unwind.h', 'libunwind.h', 'mach-o/compact_unwind_encoding.h'
            ]
        ]
        self.check_dirs += ['include/c++', 'include/mach-o']

    def _lld_config(self):
        """Configurations specific to LLD."""
        self.cfg.update('configopts', "-DLLVM_INCLUDE_TESTS=On")
        self.test_targets = ['check-lld']

        self.check_files += [
            'bin/lld', 'bin/ld.lld', 'bin/ld64.lld', 'bin/wasm-ld', 'bin/lld-link',
            'lib/liblldCOFF.a', 'lib/liblldCommon.a', 'lib/liblldELF.a', 'lib/liblldMachO.a', 'lib/liblldMinGW.a',
            'lib/liblldWasm.a'
            ]
        self.check_dirs += ['lib/cmake/lld', 'include/lld']

    def _lldb_config(self):
        """Configurations specific to LLDB."""
        self.check_files += [
            'bin/lldb',
            ]
        # self.check_dirs += ['lib/cmake/lld', 'include/lld']

    def _compiler_rt_config(self):
        """Configurations specific to Compiler-RT."""
        # self.cfg.update('configopts', "-DCOMPILER_RT_INCLUDE_TESTS=On")
        # self.cfg.update('configopts', "-DLLVM_EXTERNAL_LIT=%s" % which('llvm-lit'))
        # cflags = ' '.join([
        #     '-I%s' % os.path.join(self.srcdir, 'include'),
        # ])
        # self.cfg.update('configopts', "-DCOMPILER_RT_TEST_COMPILER_CFLAGS=\"%s\"" % cflags)
        # self.test_targets = ['check-all']

        # Remove the lit.common.cfg.py to avoid configuration issues
        # cfg_path = os.path.join(self.srcdir, 'test', 'lit.common.cfg.py')
        # if os.path.exists(cfg_path):
        #     os.remove(cfg_path)

        # Disable all sanitizer tests
        # Could be reenabled if runing Clang toolchain instead of GCC
        # cmakelists_tests = os.path.join(self.srcdir, 'test', 'CMakeLists.txt')
        # apply_regex_substitutions(cmakelists_tests, [(r'compiler_rt_test_runtime.*san.*', '')])

        # CMake does not check that compiler is Clang before adding this flags
        # for test_dir in ['fuzzer', 'asan', 'gwp_asan', 'interception', 'sanitizer_common', 'ubsan']:
        #     test_file = os.path.join(self.srcdir, 'lib', test_dir, 'tests', 'CMakeLists.txt')
        #     if os.path.exists(test_file):
        #         apply_regex_substitutions(test_file, [
        #             (r'.*--driver-mode=g\+\+.*', ''),
        #             (r'.*-fsanitize-ignorelist=.*', ''),
        #             ])

        arch = get_cpu_architecture()
        self.check_files += [
            'lib/linux/%s-%s.a' % (f, arch) for f in [
                'libclang_rt.asan', 'libclang_rt.hwasan', 'libclang_rt.lsan', 'libclang_rt.memprof', 'libclang_rt.xray',
                'libclang_rt.scudo_standalone', 'libclang_rt.stats', 'libclang_rt.tsan', 'libclang_rt.ubsan_minimal',
            ]
        ] + ['bin/hwasan_symbolize']
        self.check_dirs += ['include/sanitizer', 'include/fuzzer', 'include/orc', 'include/xray']

    def configure_step(self):
        """Run CMake for stage 1 Clang."""

        # if all(dep['name'] != 'ncurses' for dep in self.cfg['dependencies']):
        #     print_warning('Clang requires ncurses to run, did you forgot to add it to dependencies?')

        llvm_root = get_software_root('LLVM')
        if not llvm_root:
            raise EasyBuildError("Can't find LLVM to use")

        for prevs in ['LLVM', 'lld', 'lldb', 'clang-tools-extra', 'clang', 'flang', 'mlir']:
            sft_root = get_software_root(prevs)
            if sft_root:
                cmake_dir = os.path.join(sft_root, 'lib', 'cmake', prevs.lower())
                self.cfg.update('configopts', "-D%s_DIR=%s" % (prevs.upper(), cmake_dir))

        llvm_path = os.path.join(self.builddir, 'llvm')
        if os.path.exists(llvm_path):
            llvm_path = os.path.join(llvm_path, 'cmake', 'modules')
            cmake_mpath = os.getenv('CMAKE_MODULE_PATH')
            if cmake_mpath is None:
                cmake_mpath = llvm_path
            else:
                cmake_mpath = "%s;%s" % (llvm_path, cmake_mpath)
            self.cfg.update('configopts', "-DCMAKE_MODULE_PATH=%s" % cmake_mpath)

        self.cfg.update('configopts', "-DLLVM_LINK_LLVM_DYLIB=ON")

        self.llvm_obj_dir_stage1 = os.path.join(self.builddir, 'llvm.obj.1')
        if self.cfg['bootstrap']:
            self.llvm_obj_dir_stage2 = os.path.join(self.builddir, 'llvm.obj.2')
            self.llvm_obj_dir_stage3 = os.path.join(self.builddir, 'llvm.obj.3')

        # if LooseVersion(self.version) >= LooseVersion('3.3'):
        #     disable_san_tests = False
        #     # all sanitizer tests will fail when there's a limit on the vmem
        #     # this is ugly but I haven't found a cleaner way so far
        #     (vmemlim, ec) = run_cmd("ulimit -v", regexp=False)
        #     if not vmemlim.startswith("unlimited"):
        #         disable_san_tests = True
        #         self.log.warn("There is a virtual memory limit set of %s KB. The tests of the "
        #                       "sanitizers will be disabled as they need unlimited virtual "
        #                       "memory unless --strict=error is used." % vmemlim.strip())

        #     # the same goes for unlimited stacksize
        #     (stacklim, ec) = run_cmd("ulimit -s", regexp=False)
        #     if stacklim.startswith("unlimited"):
        #         disable_san_tests = True
        #         self.log.warn("The stacksize limit is set to unlimited. This causes the ThreadSanitizer "
        #                       "to fail. The sanitizers tests will be disabled unless --strict=error is used.")

        #     if (disable_san_tests or self.cfg['skip_sanitizer_tests']) and build_option('strict') != run.ERROR:
        #         self.log.debug("Disabling the sanitizer tests")
        #         self.disable_sanitizer_tests()

        # Create and enter build directory.
        mkdir(self.llvm_obj_dir_stage1)
        change_dir(self.llvm_obj_dir_stage1)


        # Configure default options for the project
        configure_func = getattr(self, '_%s_config' % self.project_name.replace('-', '_'), None)
        if configure_func:
            configure_func()

        if self.cfg['assertions']:
            self.cfg.update('configopts', "-DLLVM_ENABLE_ASSERTIONS=ON")
        else:
            self.cfg.update('configopts', "-DLLVM_ENABLE_ASSERTIONS=OFF")

        lld_root = get_software_root('lld')
        if lld_root:
            # Should be different depending on OS
            lld_driver = 'lld'
            self.cfg.update('configopts', "-DCMAKE_EXE_LINKER_FLAGS=-fuse-ld=%s" % lld_driver)

        self.cfg.update('configopts', "-DCMAKE_AR=%s" % os.path.join(llvm_root, 'bin', 'llvm-ar'))
        self.cfg.update('configopts', "-DCMAKE_NM=%s" % os.path.join(llvm_root, 'bin', 'llvm-nm'))
        self.cfg.update('configopts', "-DCMAKE_RANLIB=%s" % os.path.join(llvm_root, 'bin', 'llvm-ranlib'))

        # if "NVPTX" in build_targets:
        #     self.cfg.update('configopts', "-DPOLLY_ENABLE_GPGPU_CODEGEN=ON")

        # If Z3 is included as a dep, enable support in static analyzer (if enabled)
        # if self.cfg["static_analyzer"] and LooseVersion(self.version) >= LooseVersion('9.0.0'):
        #     z3_root = get_software_root("Z3")
        #     if z3_root:
        #         self.cfg.update('configopts', "-DLLVM_ENABLE_Z3_SOLVER=ON")
        #         self.cfg.update('configopts', "-DLLVM_Z3_INSTALL_DIR=%s" % z3_root)

        build_targets = self.cfg['build_targets']

        self.cfg.update('configopts', '-DLLVM_TARGETS_TO_BUILD="%s"' % ';'.join(build_targets))

        if self.cfg['parallel']:
            self.make_parallel_opts = "-j %s" % self.cfg['parallel']

        # If hwloc is included as a dep, use it in OpenMP runtime for affinity
        hwloc_root = get_software_root('hwloc')
        if hwloc_root:
            self.cfg.update('configopts', '-DLIBOMP_USE_HWLOC=ON')
            self.cfg.update('configopts', '-DLIBOMP_HWLOC_INSTALL_DIR=%s' % hwloc_root)

        # If 'NVPTX' is in the build targets we assume the user would like OpenMP offload support as well
        if 'NVPTX' in build_targets:
            # list of CUDA compute capabilities to use can be specifed in two ways (where (2) overrules (1)):
            # (1) in the easyconfig file, via the custom cuda_compute_capabilities;
            # (2) in the EasyBuild configuration, via --cuda-compute-capabilities configuration option;
            ec_cuda_cc = self.cfg['cuda_compute_capabilities']
            cfg_cuda_cc = build_option('cuda_compute_capabilities')
            cuda_cc = cfg_cuda_cc or ec_cuda_cc or []
            if not cuda_cc:
                raise EasyBuildError("Can't build Clang with CUDA support "
                                     "without specifying 'cuda-compute-capabilities'")
            default_cc = self.cfg['default_cuda_capability'] or min(cuda_cc)
            if not self.cfg['default_cuda_capability']:
                print_warning("No default CUDA capability defined! "
                              "Using '%s' taken as minimum from 'cuda_compute_capabilities'" % default_cc)
            cuda_cc = [cc.replace('.', '') for cc in cuda_cc]
            default_cc = default_cc.replace('.', '')
            self.cfg.update('configopts', '-DCLANG_OPENMP_NVPTX_DEFAULT_ARCH=sm_%s' % default_cc)
            self.cfg.update('configopts', '-DLIBOMPTARGET_NVPTX_COMPUTE_CAPABILITIES=%s' % ','.join(cuda_cc))
        # If we don't want to build with CUDA (not in dependencies) trick CMakes FindCUDA module into not finding it by
        # using the environment variable which is used as-is and later checked for a falsy value when determining
        # whether CUDA was found
        if not get_software_root('CUDA'):
            setvar('CUDA_NVCC_EXECUTABLE', 'IGNORE')
        # If 'AMDGPU' is in the build targets we assume the user would like OpenMP offload support for AMD
        if 'AMDGPU' in build_targets:
            if not get_software_root('ROCR-Runtime'):
                raise EasyBuildError("Can't build Clang with AMDGPU support "
                                     "without dependency 'ROCR-Runtime'")
            ec_amdgfx = self.cfg['amd_gfx_list']
            if not ec_amdgfx:
                raise EasyBuildError("Can't build Clang with AMDGPU support "
                                     "without specifying 'amd_gfx_list'")
            self.cfg.update('configopts', '-DLIBOMPTARGET_AMDGCN_GFXLIST=%s' % ' '.join(ec_amdgfx))

        self.log.info("Configuring")

        super(EB_LLVMcomponent, self).configure_step(srcdir=self.srcdir)

    # def disable_sanitizer_tests(self):
    #     """Disable the tests of all the sanitizers by removing the test directories from the build system"""
    #     if LooseVersion(self.version) < LooseVersion('3.6'):
    #         # for Clang 3.5 and lower, the tests are scattered over several CMakeLists.
    #         # We loop over them, and patch out the rule that adds the sanitizers tests to the testsuite
    #         patchfiles = ['lib/asan', 'lib/dfsan', 'lib/lsan', 'lib/msan', 'lib/tsan', 'lib/ubsan']

    #         for patchfile in patchfiles:
    #             cmakelists = os.path.join(self.llvm_src_dir, 'projects/compiler-rt', patchfile, 'CMakeLists.txt')
    #             if os.path.exists(cmakelists):
    #                 regex_subs = [(r'.*add_subdirectory\(lit_tests\).*', '')]
    #                 apply_regex_substitutions(cmakelists, regex_subs)

    #         # There is a common part seperate for the specific sanitizers, we disable all the common tests
    #         cmakelists = os.path.join('projects', 'compiler-rt', 'lib', 'sanitizer_common', 'CMakeLists.txt')
    #         regex_subs = [(r'.*add_subdirectory\(tests\).*', '')]
    #         apply_regex_substitutions(cmakelists, regex_subs)

    #     else:
    #         # In Clang 3.6, the sanitizer tests are grouped together in one CMakeLists
    #         # We patch out adding the subdirectories with the sanitizer tests
    #         if LooseVersion(self.version) >= LooseVersion('14'):
    #             cmakelists_tests = os.path.join(self.llvm_src_dir, 'compiler-rt', 'test', 'CMakeLists.txt')
    #         else:
    #             cmakelists_tests = os.path.join(self.llvm_src_dir, 'projects', 'compiler-rt', 'test', 'CMakeLists.txt')
    #         regex_subs = []
    #         if LooseVersion(self.version) >= LooseVersion('5.0'):
    #             regex_subs.append((r'compiler_rt_test_runtime.*san.*', ''))
    #         else:
    #             regex_subs.append((r'add_subdirectory\((.*san|sanitizer_common)\)', ''))

    #         apply_regex_substitutions(cmakelists_tests, regex_subs)

    def build_with_prev_stage(self, prev_obj, next_obj):
        """Build Clang stage N using Clang stage N-1"""

        # Create and enter build directory.
        mkdir(next_obj)
        change_dir(next_obj)

        # Make sure clang and clang++ compilers from the previous stage are (temporarily) in PATH
        # The call to prepare_rpath_wrappers() requires the compilers-to-be-wrapped on the PATH
        # Also, the call to 'which' in later in this current function also requires that
        orig_path = os.getenv('PATH')
        prev_obj_path = os.path.join(prev_obj, 'bin')
        setvar('PATH', prev_obj_path + ":" + orig_path)

        # If building with rpath, create RPATH wrappers for the Clang compilers for stage 2 and 3
        if build_option('rpath'):
            my_clang_toolchain = Clang(name='Clang', version='1')
            my_clang_toolchain.prepare_rpath_wrappers()
            self.log.info("Prepared clang rpath wrappers")

            # add symlink for 'opt' to wrapper dir, since Clang expects it in the same directory
            # see https://github.com/easybuilders/easybuild-easyblocks/issues/3075
            clang_wrapper_dir = os.path.dirname(which('clang'))
            symlink(os.path.join(prev_obj_path, 'opt'), os.path.join(clang_wrapper_dir, 'opt'))

            # RPATH wrappers add -Wl,rpath arguments to all command lines, including when it is just compiling
            # Clang by default warns about that, and then some configure tests use -Werror which turns those warnings
            # into errors. As a result, those configure tests fail, even though the compiler supports the requested
            # functionality (e.g. the test that checks if -fPIC is supported would fail, and it compiles without
            # resulting in relocation errors).
            # See https://github.com/easybuilders/easybuild-easyblocks/pull/2799#issuecomment-1270621100
            # Here, we add -Wno-unused-command-line-argument to CXXFLAGS to avoid these warnings alltogether
            cflags = os.getenv('CFLAGS')
            cxxflags = os.getenv('CXXFLAGS')
            setvar('CFLAGS', "%s %s" % (cflags, '-Wno-unused-command-line-argument'))
            setvar('CXXFLAGS', "%s %s" % (cxxflags, '-Wno-unused-command-line-argument'))

        # determine full path to clang/clang++ (which may be wrapper scripts in case of RPATH linking)
        clang = which('clang')
        clangxx = which('clang++')

        # Configure.
        options = [
            "-DCMAKE_INSTALL_PREFIX=%s " % self.installdir,
            "-DCMAKE_C_COMPILER='%s' " % clang,
            "-DCMAKE_CXX_COMPILER='%s' " % clangxx,
            self.cfg['configopts'],
            "-DCMAKE_BUILD_TYPE=%s " % self.build_type,
        ]

        # Cmake looks for llvm-link by default in the same directory as the compiler
        # However, when compiling with rpath, the clang 'compiler' is not actually the compiler, but the wrapper
        # Clearly, the wrapper directory won't llvm-link. Thus, we pass the linker to be used by full path.
        # See https://github.com/easybuilders/easybuild-easyblocks/pull/2799#issuecomment-1275916186
        if build_option('rpath'):
            llvm_link = which('llvm-link')
            options.append("-DLIBOMPTARGET_NVPTX_BC_LINKER=%s" % llvm_link)

        self.log.info("Configuring")
        if LooseVersion(self.version) >= LooseVersion('14'):
            run_cmd("cmake %s %s" % (' '.join(options), os.path.join(self.llvm_src_dir, "llvm")), log_all=True)
        else:
            run_cmd("cmake %s %s" % (' '.join(options), self.llvm_src_dir), log_all=True)

        self.log.info("Building")
        run_cmd("make %s VERBOSE=1" % self.make_parallel_opts, log_all=True)

        # restore $PATH
        setvar('PATH', orig_path)

    def build_step(self):
        """Build Clang stage 1, 2, 3"""
        # Stage 1: build using system compiler.
        self.log.info("Building stage 1")
        change_dir(self.llvm_obj_dir_stage1)
        super(EB_LLVMcomponent, self).build_step()

        if self.cfg['bootstrap']:
            self.log.info("Building stage 2")
            self.build_with_prev_stage(self.llvm_obj_dir_stage1, self.llvm_obj_dir_stage2)

            self.log.info("Building stage 3")
            self.build_with_prev_stage(self.llvm_obj_dir_stage2, self.llvm_obj_dir_stage3)

    def test_step(self):
        """Run Clang tests on final stage (unless disabled)."""
        if not self.cfg['skip_all_tests']:
            if self.cfg['bootstrap']:
                change_dir(self.llvm_obj_dir_stage3)
            else:
                change_dir(self.llvm_obj_dir_stage1)
            for target in self.test_targets:
                self.log.info("Running tests for %s" % target)
                (out, _) = run_cmd("make %s" % target, log_all=False, log_ok=False, simple=False, regexp=False)
                self.log.debug(out)

                rgx_failed = re.compile(r'^ +Failed +: +([0-9]+)', flags=re.MULTILINE)
                mch = rgx_failed.search(out)
                if mch is None:
                    rgx_passed = re.compile(r'^ +Passed +: +([0-9]+)', flags=re.MULTILINE)
                    mch = rgx_passed.search(out)
                    if mch is None:
                        raise EasyBuildError("Failed to extract test results from output")
                    num_failed = 0
                else:
                    num_failed = int(mch.group(1))
                if num_failed > self.cfg['test_suite_max_failed']:
                    raise EasyBuildError("Too many failed tests: %s", num_failed)

    def install_step(self):
        """Install stage 3 binaries."""

        if self.cfg['bootstrap']:
            change_dir(self.llvm_obj_dir_stage3)
        else:
            change_dir(self.llvm_obj_dir_stage1)
        super(EB_LLVMcomponent, self).install_step()

        if self.project_name == 'clang':
            compiler_rt_root = get_software_root('compiler-rt')
            major_version = self.version.split('.')[0]

            lib_path_src = os.path.join(compiler_rt_root, 'lib')
            lib_path_dst = os.path.join(self.installdir, 'lib', 'clang', major_version, 'lib')
            symlink(lib_path_src, lib_path_dst)

    def post_install_step(self):
        """Install python bindings."""
        super(EB_LLVMcomponent, self).post_install_step()

        # copy Python bindings here in post-install step so that it is not done more than once in multi_deps context
        if self.cfg['python_bindings'] and self.project_name in ['clang', 'mlir']:
            python_bindings_source_dir = os.path.join(self.srcdir, "bindings", "python")
            python_bindins_target_dir = os.path.join(self.installdir, 'lib', 'python')

            shutil.copytree(python_bindings_source_dir, python_bindins_target_dir)

    def sanity_check_step(self):
        """Custom sanity check for Clang."""
        # custom_commands = ['clang --help', 'clang++ --help', 'llvm-config --cxxflags']
        custom_commands = []
        shlib_ext = get_shared_lib_ext()

        version = LooseVersion(self.version)

        # Clang v16+ only use the major version number for the resource dir
        resdir_version = self.version
        if version >= '16':
            resdir_version = self.version.split('.')[0]

        # Detect OpenMP support for CPU architecture
        arch = get_cpu_architecture()
        # Check architecture explicitly since Clang uses potentially
        # different names
        if arch == X86_64:
            arch = 'x86_64'
        elif arch == POWER:
            arch = 'ppc64'
        elif arch == AARCH64:
            arch = 'aarch64'
        else:
            print_warning("Unknown CPU architecture (%s) for OpenMP and runtime libraries check!" % arch)

        custom_paths = self.cfg['sanity_check_paths']
        custom_paths.setdefault('files', []).extend(self.check_files)
        custom_paths.setdefault('dirs', []).extend(self.check_dirs)
        custom_commands.extend(self.check_commands)
        if shlib_ext != 'so':
            files = custom_paths.get('files', [])
            files = [f.replace('.so', '.%s' % shlib_ext) for f in files if f.endswith('.so')]
            custom_paths['files'] = files

        # if version >= '14':
        #     glob_pattern = os.path.join(self.installdir, 'lib', '%s-*' % arch)
        #     matches = glob.glob(glob_pattern)
        #     if matches:
        #         directory = os.path.basename(matches[0])
        #         self.runtime_lib_path = os.path.join("lib", directory)
        #     else:
        #         print_warning("Could not find runtime library directory")
        #         self.runtime_lib_path = "lib"
        # else:
        #     self.runtime_lib_path = "lib"

        # custom_paths = {
        #     'files': [
        #         "bin/clang", "bin/clang++", "bin/llvm-ar", "bin/llvm-nm", "bin/llvm-as", "bin/opt", "bin/llvm-link",
        #         "bin/llvm-config", "bin/llvm-symbolizer", "include/llvm-c/Core.h", "include/clang-c/Index.h",
        #         "lib/libclang.%s" % shlib_ext, "lib/clang/%s/include/stddef.h" % resdir_version,
        #     ],
        #     'dirs': ["include/clang", "include/llvm", "lib/clang/%s/lib" % resdir_version],
        # }
        # if self.cfg['static_analyzer']:
        #     custom_paths['files'].extend(["bin/scan-build", "bin/scan-view"])

        # if 'clang-tools-extra' in self.cfg['llvm_projects'] and version >= '3.4':
        #     custom_paths['files'].extend(["bin/clang-tidy"])

        # if 'polly' in self.cfg['llvm_projects']:
        #     custom_paths['files'].extend(["lib/LLVMPolly.%s" % shlib_ext])
        #     custom_paths['dirs'].extend(["include/polly"])

        # if 'lld' in self.cfg['llvm_projects']:
        #     custom_paths['files'].extend(["bin/lld"])

        # if 'lldb' in self.cfg['llvm_projects']:
        #     custom_paths['files'].extend(["bin/lldb"])

        # if 'libunwind' in self.cfg['llvm_runtimes']:
        #     custom_paths['files'].extend([os.path.join(self.runtime_lib_path, "libunwind.%s" % shlib_ext)])

        # if 'libcxx' in self.cfg['llvm_runtimes']:
        #     custom_paths['files'].extend([os.path.join(self.runtime_lib_path, "libc++.%s" % shlib_ext)])

        # if 'libcxxabi' in self.cfg['llvm_runtimes']:
        #     custom_paths['files'].extend([os.path.join(self.runtime_lib_path, "libc++abi.%s" % shlib_ext)])

        # if 'flang' in self.cfg['llvm_projects'] and version >= '15':
        #     flang_compiler = 'flang-new'
        #     custom_paths['files'].extend(["bin/%s" % flang_compiler])
        #     custom_commands.extend(["%s --help" % flang_compiler])

        # if version >= '3.8':
        #     custom_paths['files'].extend(["lib/libomp.%s" % shlib_ext, "lib/clang/%s/include/omp.h" % resdir_version])

        # if version >= '12':
        #     omp_target_libs = ["lib/libomptarget.%s" % shlib_ext, "lib/libomptarget.rtl.%s.%s" % (arch, shlib_ext)]
        # else:
        #     omp_target_libs = ["lib/libomptarget.%s" % shlib_ext]
        # custom_paths['files'].extend(omp_target_libs)

        # # If building for CUDA check that OpenMP target library was created
        # if 'NVPTX' in self.cfg['build_targets']:
        #     custom_paths['files'].append("lib/libomptarget.rtl.cuda.%s" % shlib_ext)
        #     # The static 'nvptx.a' library is not built from version 12 onwards
        #     if version < '12.0':
        #         custom_paths['files'].append("lib/libomptarget-nvptx.a")
        #     ec_cuda_cc = self.cfg['cuda_compute_capabilities']
        #     cfg_cuda_cc = build_option('cuda_compute_capabilities')
        #     cuda_cc = cfg_cuda_cc or ec_cuda_cc or []
        #     # We need the CUDA capability in the form of '75' and not '7.5'
        #     cuda_cc = [cc.replace('.', '') for cc in cuda_cc]
        #     if '12.0' < version < '13.0':
        #         custom_paths['files'].extend(["lib/libomptarget-nvptx-cuda_%s-sm_%s.bc" % (x, y)
        #                                      for x in CUDA_TOOLKIT_SUPPORT for y in cuda_cc])
        #     # libomptarget-nvptx-sm*.bc is not there for Clang 14.x;
        #     elif version < '14.0' or version >= '15.0':
        #         custom_paths['files'].extend(["lib/libomptarget-nvptx-sm_%s.bc" % cc
        #                                      for cc in cuda_cc])
        #     # From version 13, and hopefully onwards, the naming of the CUDA
        #     # '.bc' files became a bit simpler and now we don't need to take
        #     # into account the CUDA version Clang was compiled with, making it
        #     # easier to check for the bitcode files we expect;
        #     # libomptarget-new-nvptx-sm*.bc is only there in Clang 13.x and 14.x;
        #     if version >= '13.0' and version < '15.0':
        #         custom_paths['files'].extend(["lib/libomptarget-new-nvptx-sm_%s.bc" % cc
        #                                       for cc in cuda_cc])
        # # If building for AMDGPU check that OpenMP target library was created
        # if 'AMDGPU' in self.cfg['build_targets']:
        #     custom_paths['files'].append("lib/libLLVMAMDGPUCodeGen.a")
        #     # OpenMP offloading support to AMDGPU was not added until version
        #     # 13, however, building for the AMDGPU target predates this and so
        #     # doesn't necessarily mean that the AMDGPU target failed
        #     if version >= '13.0':
        #         custom_paths['files'].append("lib/libomptarget.rtl.amdgpu.%s" % shlib_ext)
        #         custom_paths['files'].extend(["lib/libomptarget-amdgcn-%s.bc" % gfx
        #                                       for gfx in self.cfg['amd_gfx_list']])
        #         custom_paths['files'].append("bin/amdgpu-arch")
        #     if version >= '14.0':
        #         custom_paths['files'].extend(["lib/libomptarget-new-amdgpu-%s.bc" % gfx
        #                                       for gfx in self.cfg['amd_gfx_list']])

        # if self.cfg['python_bindings']:
        #     custom_paths['files'].extend([os.path.join("lib", "python", "clang", "cindex.py")])
        #     custom_commands.extend(["python -c 'import clang'"])

        super(EB_LLVMcomponent, self).sanity_check_step(custom_paths=custom_paths, custom_commands=custom_commands)

    def make_module_extra(self):
        """Custom variables for module."""
        txt = super(EB_LLVMcomponent, self).make_module_extra()
        if self.project_name == 'clang':
            # we set the symbolizer path so that asan/tsan give meanfull output by default
            asan_symbolizer_path = which('llvm-symbolizer')
            txt += self.module_generator.set_environment('ASAN_SYMBOLIZER_PATH', asan_symbolizer_path)
            if self.cfg['python_bindings']:
                txt += self.module_generator.prepend_paths('PYTHONPATH', os.path.join("lib", "python"))
        # cmake_mod_path = os.path.join('lib', 'cmake', self.project_name)
        # if os.path.exists(os.path.join(self.installdir, cmake_mod_path)):
        #     mod_str = self.module_generator.prepend_paths('CMAKE_MODULE_PATH', cmake_mod_path)
        #     mod_str = re.sub(r'\) *$', ', ";")', mod_str)
        #     # print_warning(mod_str)
        #     # mod_str = re.sub(r'^prepend_path\(', 'prepend-path{', mod_str)
        #     # print_warning(mod_str)
        #     txt += mod_str
        return txt

    def make_module_req_guess(self):
        """
        Clang can find its own headers and libraries but the .so's need to be in LD_LIBRARY_PATH
        """
        guesses = super(EB_LLVMcomponent, self).make_module_req_guess()
        guesses.update({
            'CPATH': ['include'],
            'LIBRARY_PATH': ['lib', 'lib64', 'lib/linux', self.runtime_lib_path],
            'LD_LIBRARY_PATH': ['lib', 'lib64', 'lib/linux', self.runtime_lib_path],
        })
        return guesses
