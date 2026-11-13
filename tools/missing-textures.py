#!/usr/bin/env python3
"""missing-textures -- which textures do these meshes name that nothing in the load order provides? Read-only.

For each mesh (Data-relative paths, or every mesh under a folder/glob), House Carl's nif_inspect lists the texture-set
paths each shape embeds; asset_status then says, for each texture, whether a loose file or an active BSA provides it.
A texture nothing provides is reported with the meshes and shapes that use it. An archive House Carl could not read
makes the answer incomplete: the tool then says COULD NOT CHECK instead of trusting "absent".
Suggesting a replacement (the vanilla texture, a different mod's copy) is left to the agent and the user.

Needs the optional House Carl gateway (docs/housecarl.md).
Usage:  python tools/missing-textures.py MESH [MESH...] | --under 'meshes/armor/**/*.nif'  [--json]
Exit:   0 none missing · 1 missing textures or meshes (listed) · 2 could not check (named; a mesh or texture
        House Carl gave no answer for is could-not-check, never clean)
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile

HERE = Path(__file__).resolve().parent
SLOT = re.compile(r"^\s+tex\[\d+\](?: \([^)]*\))?: (?P<path>\S.*?)\s*$")
SHAPE = re.compile(r"^  '(?P<shape>.*)':\s*$")
BATCH = 50


def call(tool, arguments):
    """One House Carl gateway call through the toolkit CLI; returns the tool's text."""
    with tempfile.NamedTemporaryFile('w', suffix='.json', delete=False, encoding='utf-8') as f:
        json.dump(arguments, f)
    try:
        out = subprocess.run([sys.executable, str(HERE / 'housecarl.py'), 'call', tool, '--arguments-file', f.name],
                             capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=600)
    finally:
        Path(f.name).unlink()
    if out.returncode != 0:
        raise RuntimeError(tool + ' failed: ' + (out.stderr or out.stdout).strip()[-400:])
    envelope = json.loads(out.stdout)
    result = envelope.get('result', envelope)
    if result.get('is_error'):
        raise RuntimeError(tool + ' refused: ' + ''.join(c.get('text', '') for c in result.get('content', []))[:400])
    return ''.join(c.get('text', '') for c in result.get('content', []))


def norm(path):
    p = path.replace('/', '\\').strip()
    return p if p.lower().startswith('textures\\') else 'textures\\' + p


def textures_of(text, meshes):
    """{texture: [(mesh, shape)]} from nif_inspect's `paths` section, the meshes it says are ABSENT, and the meshes it
    did not answer for: never named, or named without a `version:` line (read) or ABSENT. A mesh in that last list
    makes the run COULD NOT CHECK; an unanswered mesh must never count as one with no missing textures."""
    uses, unread, mesh, shape = {}, [], None, None
    names = {m.replace('/', '\\').lower(): m for m in meshes}
    answered = set()
    for line in text.splitlines():
        key = line.strip().replace('/', '\\').lower()
        if not line.startswith(' ') and key.endswith('.nif'):
            mesh, shape = names.get(key), None     # a mesh not asked about ends the previous one's block too
            continue
        if mesh is not None and line.strip().startswith('ABSENT'):
            unread.append(mesh)
            answered.add(mesh)
            continue
        if mesh is not None and line.startswith('  version:'):
            answered.add(mesh)
            continue
        m = SHAPE.match(line)
        if m:
            shape = m.group('shape')
            continue
        m = SLOT.match(line)
        if m and mesh is not None:
            uses.setdefault(norm(m.group('path')), []).append((mesh, shape))
    return uses, unread, [m for m in meshes if m not in answered]


def meshes_under(selector):
    text = call('housecarl_asset_status', {'under': [selector], 'format': 'json', 'max_chars': 2000000})
    data = json.loads(text)
    if data.get('read_incomplete') or data.get('bsa_failures'):
        raise RuntimeError('House Carl could not read every archive while listing ' + selector)
    return [r['path'] for r in data['results'] if r.get('exists') and r['path'].lower().endswith('.nif')]


def check(meshes):
    uses, unread = {}, []
    for i in range(0, len(meshes), BATCH):
        part = meshes[i:i + BATCH]
        text = call('housecarl_nif_inspect', {'mesh_paths': part, 'sections': 'paths', 'max_chars': 2000000})
        found, missing_meshes, unanswered = textures_of(text, part)
        if unanswered:
            raise RuntimeError('nif_inspect gave no answer for %d mesh(es), e.g. %s' % (len(unanswered), unanswered[0]))
        for tex, rows in found.items():
            uses.setdefault(tex, []).extend(rows)
        unread += missing_meshes
    textures = sorted(uses, key=str.lower)
    absent, incomplete = [], False
    for i in range(0, len(textures), 500):
        data = json.loads(call('housecarl_asset_status', {'asset_paths': textures[i:i + 500], 'format': 'json',
                                                          'max_chars': 2000000}))
        incomplete = incomplete or bool(data.get('read_incomplete') or data.get('bsa_failures'))
        asked = {norm(t).lower() for t in textures[i:i + 500]}
        told = {norm(r['path']).lower() for r in data['results'] if isinstance(r.get('exists'), bool)}
        if asked - told:
            raise RuntimeError('asset_status gave no answer for %d texture(s), e.g. %s'
                               % (len(asked - told), sorted(asked - told)[0]))
        absent += [r['path'] for r in data['results'] if r.get('exists') is False]
    return {'meshes': len(meshes), 'unread_meshes': unread, 'textures': len(textures), 'incomplete': incomplete,
            'missing': [{'texture': t, 'used_by': [{'mesh': m, 'shape': s} for m, s in uses.get(t, uses.get(norm(t), []))]}
                        for t in absent]}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    ap.add_argument('meshes', nargs='*')
    ap.add_argument('--under')
    ap.add_argument('--json', action='store_true')
    args = ap.parse_args(argv)
    if not args.meshes and not args.under:
        ap.error('name meshes or --under')
    try:
        meshes = list(args.meshes) + (meshes_under(args.under) if args.under else [])
        out = check(meshes)
    except (OSError, RuntimeError, ValueError, KeyError) as exc:
        print('COULD NOT CHECK: ' + str(exc), file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(out, indent=1))
    else:
        print('%d meshes, %d textures named, %d missing' % (out['meshes'], out['textures'], len(out['missing'])))
        for row in out['missing']:
            print('  ' + row['texture'])
            for use in row['used_by'][:5]:
                print('      used by %s  (%s)' % (use['mesh'], use['shape']))
        for mesh in out['unread_meshes']:
            print('  mesh not found: ' + mesh)
    if out['incomplete']:
        print('COULD NOT CHECK: an archive could not be read, so "missing" may be wrong.', file=sys.stderr)
        return 2
    return 1 if out['missing'] or out['unread_meshes'] else 0


if __name__ == '__main__':
    sys.exit(main())
