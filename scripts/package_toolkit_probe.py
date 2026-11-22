#!/usr/bin/env python3
"""Package a source-bound optional mod. Does not install, activate or publish it."""
from __future__ import annotations

import argparse
import hashlib
import io
import json
from pathlib import Path
import struct
import subprocess
import sys
import zipfile

SOURCE = 'tools/toolkit-probe'


def git(repo: Path, *args: str) -> bytes:
    p = subprocess.run(['git', '-C', str(repo), *args], capture_output=True, timeout=60)
    if p.returncode:
        raise ValueError('git: ' + p.stderr.decode('utf-8', errors='replace').strip())
    return p.stdout


def package(repo: Path, commit: str, dll: Path, receipt: Path, output: Path) -> None:
    sha = git(repo, 'rev-parse', '--verify', commit + '^{commit}').decode().strip()
    if sha != git(repo, 'rev-parse', 'HEAD').decode().strip():
        raise ValueError('package commit must be the checked-out source commit')
    # The producer binds its build before compilation. This is an integrity check,
    # not an independent attestation that the compiler consumed those bytes.
    record = json.loads(receipt.read_text(encoding='utf-8'))
    if record.get('schemaVersion') != 1 or record.get('sourceCommit') != sha:
        raise ValueError('build receipt source commit mismatch')
    if not record.get('dependencies') or not record.get('toolchain'):
        raise ValueError('build receipt lacks dependency/toolchain provenance')
    if git(repo, 'status', '--porcelain', '--untracked-files=all', '--', SOURCE, 'LICENSE').strip():
        raise ValueError('uncommitted source inputs; commit before building and packaging')
    raw = dll.read_bytes()
    if hashlib.sha256(raw).hexdigest() != record.get('dllSha256'):
        raise ValueError('DLL hash differs from build receipt')
    if len(raw) < 64 or raw[:2] != b'MZ':
        raise ValueError('not an x64 PE DLL')
    pe = struct.unpack_from('<I', raw, 0x3c)[0]
    if (pe + 24 > len(raw) or raw[pe:pe + 4] != b'PE\0\0'
            or struct.unpack_from('<H', raw, pe + 4)[0] != 0x8664
            or not struct.unpack_from('<H', raw, pe + 22)[0] & 0x2000):
        raise ValueError('not an x64 PE DLL')
    source_members = {}
    # Read committed blobs directly: git archive may apply local export/eol attributes.
    entries = git(repo, 'ls-tree', '-rz', sha, '--', SOURCE, 'LICENSE')
    for entry in entries.split(b'\0'):
        if not entry:
            continue
        meta, name_bytes = entry.split(b'\t', 1)
        mode, kind, oid = meta.decode().split()
        if mode not in ('100644', '100755') or kind != 'blob':
            raise ValueError('source contains a link or unsupported git entry')
        name = name_bytes.decode('utf-8')
        source_members[name] = git(repo, 'cat-file', 'blob', oid)
    if SOURCE + '/README.md' not in source_members or 'LICENSE' not in source_members:
        raise ValueError('source archive lacks README or license')
    # Source/tools are recognized Data roots by mod installers. Keep exact source
    # blobs in an opaque nested archive, so only the DLL maps to game content.
    members = {'source.zip': zip_bytes(source_members)}
    members['Data/SKSE/Plugins/ToolkitProbe.dll'] = raw
    members['build-receipt.json'] = (json.dumps(record, indent=2, sort_keys=True) + '\n').encode()
    members['README.txt'] = (
        'ToolkitProbe optional DevBench extension\n'
        'Install through your mod manager or the toolkit mod-install workflow.\n'
        'Requires SKSE, appropriate Address Library and DevBench >= 1.35.\n'
        'Open source.zip, then tools/toolkit-probe/README.md for capability and qualification limits.\n'
        'Source and dependency licenses are included in source.zip; do not install that archive as a mod.\n'
        'Build provenance is not runtime acceptance evidence.\n'
    ).encode()
    # Assemble before opening output; never replace a prior artifact on validation failure.
    archive = zip_bytes(members)
    with output.open('xb') as f:
        f.write(archive)


def zip_bytes(members: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as z:
        for name, content in sorted(members.items()):
            info = zipfile.ZipInfo(name, (2026, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            z.writestr(info, content)
    return buf.getvalue()


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--repo', type=Path, default=Path(__file__).resolve().parents[1])
    p.add_argument('--commit', required=True)
    p.add_argument('--dll', type=Path, required=True)
    p.add_argument('--receipt', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    args = p.parse_args(argv)
    try:
        package(args.repo, args.commit, args.dll, args.receipt, args.output)
    except (OSError, ValueError, subprocess.SubprocessError, zipfile.BadZipFile) as exc:
        print('ToolkitProbe package refused: ' + str(exc), file=sys.stderr)
        return 2
    print(args.output)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
