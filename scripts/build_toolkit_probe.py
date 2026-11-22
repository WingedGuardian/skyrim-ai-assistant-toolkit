#!/usr/bin/env python3
"""Build an already configured ToolkitProbe tree and bind its receipt before compilation."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys

COMMONLIB_COMMIT = 'f82241e5daabfaadace7706bea14a202a7916e9c'
INPUTS = ['tools/toolkit-probe', 'scripts/build_toolkit_probe.py', 'scripts/package_toolkit_probe.py']


def git(root, *args):
    return subprocess.check_output(['git', '-C', str(root), *args], text=True, timeout=60).strip()


def source_snapshot(root):
    if git(root, 'status', '--porcelain', '--untracked-files=all', '--', *INPUTS):
        raise ValueError('source inputs are uncommitted; commit before a provenance build')
    return git(root, 'rev-parse', 'HEAD')


def verify_snapshot(root, before):
    if source_snapshot(root) != before:
        raise ValueError('source commit changed during build; no receipt written')


def cache_values(build):
    values = {}
    for line in (build / 'CMakeCache.txt').read_text(encoding='utf-8').splitlines():
        m = re.match(r'^([^/#][^:]*):[^=]+=(.*)$', line)
        if m:
            values[m[1]] = m[2]
    return values


def verify_cache(root, build):
    values = cache_values(build)
    if Path(values.get('CMAKE_HOME_DIRECTORY', '')).resolve() != (root / 'tools/toolkit-probe').resolve():
        raise ValueError('build tree is configured for a different source directory')
    if values.get('COMMONLIB_PREBUILT', '').upper() not in ('OFF', 'FALSE', '0'):
        raise ValueError('source qualification requires COMMONLIB_PREBUILT=OFF')
    for runtime in ('SE', 'AE', 'VR'):
        if values.get('ENABLE_SKYRIM_' + runtime, '').upper() not in ('ON', 'TRUE', '1'):
            raise ValueError('configured build must support SE, AE and VR')
    return values


def vcpkg_commit(config):
    """The vcpkg checkout the toolchain file actually came from: the manifest baseline pins the port versions,
    but the tool itself (and the ports it falls back to) is whatever checkout the build used."""
    toolchain = config.get('CMAKE_TOOLCHAIN_FILE')
    if not toolchain:
        return 'not recorded: no vcpkg toolchain file'
    root = Path(toolchain).resolve().parents[2]
    try:
        return git(root, 'rev-parse', 'HEAD')
    except (OSError, subprocess.SubprocessError, ValueError):
        return 'not recorded: %s is not a git checkout' % root.name


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--repo', type=Path, default=Path(__file__).resolve().parents[1])
    p.add_argument('--build-dir', type=Path, required=True)
    p.add_argument('--receipt', type=Path, required=True)
    p.add_argument('--cmake', default='cmake')
    args = p.parse_args(argv)
    try:
        sha = source_snapshot(args.repo)
        config = verify_cache(args.repo, args.build_dir)
        commonlib = Path(config['COMMONLIB_SOURCE_DIR'])
        commonlib_sha = git(commonlib, 'rev-parse', 'HEAD')
        if commonlib_sha != COMMONLIB_COMMIT or git(commonlib, 'status', '--porcelain', '--untracked-files=no'):
            raise ValueError('CommonLib source differs from the pinned build input')
        manifest = json.loads((args.repo / 'tools/toolkit-probe/vcpkg.json').read_text())
        # Existing receipts must never masquerade as the result of this invocation.
        if args.receipt.exists():
            raise ValueError('receipt already exists; use a new receipt path')
        subprocess.run([args.cmake, '--build', str(args.build_dir), '--config', 'Release', '--parallel', '3'], check=True)
        ctest = str(Path(args.cmake).with_name('ctest.exe' if sys.platform == 'win32' else 'ctest')) if Path(args.cmake).is_absolute() else 'ctest'
        subprocess.run([ctest, '--test-dir', str(args.build_dir), '-C', 'Release', '--output-on-failure', '--no-tests=error'], check=True)
        verify_snapshot(args.repo, sha)
        if git(commonlib, 'rev-parse', 'HEAD') != commonlib_sha or git(commonlib, 'status', '--porcelain', '--untracked-files=no'):
            raise ValueError('CommonLib source changed during build')
        candidates = [args.build_dir / 'Release/ToolkitProbe.dll', args.build_dir / 'ToolkitProbe.dll']
        dll = next((p for p in candidates if p.is_file()), None)
        if dll is None:
            raise ValueError('build produced no ToolkitProbe.dll at the expected path')
        versions = []
        for info in (args.build_dir / 'CMakeFiles').glob('*/CMakeCXXCompiler.cmake'):
            versions.extend(re.findall(r'set\(CMAKE_CXX_COMPILER_VERSION "([^"]+)"\)', info.read_text()))
        record = {'schemaVersion': 1, 'sourceCommit': sha,
                  'dllSha256': hashlib.sha256(dll.read_bytes()).hexdigest(),
                  'dependencies': {'commonlibCommit': commonlib_sha, 'vcpkgBaseline': manifest['builtin-baseline'],
                                   'vcpkgCommit': vcpkg_commit(config)},
                  'toolchain': {'cmake': subprocess.check_output([args.cmake, '--version'], text=True).splitlines()[0],
                                'compilerVersion': sorted(set(versions)), 'configuration': 'Release', 'runtimes': ['SE', 'AE', 'VR'],
                                'generator': config.get('CMAKE_GENERATOR', 'not recorded'),
                                'triplet': config.get('VCPKG_TARGET_TRIPLET', 'not recorded'),
                                'msvcRuntime': config.get('CMAKE_MSVC_RUNTIME_LIBRARY', 'CMake default')}}
        with args.receipt.open('x', encoding='utf-8') as f:
            json.dump(record, f, indent=2, sort_keys=True)
            f.write('\n')
        print(json.dumps({'dll': str(dll), 'receipt': str(args.receipt), 'sourceCommit': sha}))
        return 0
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
        print('ToolkitProbe build refused: ' + str(exc), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
