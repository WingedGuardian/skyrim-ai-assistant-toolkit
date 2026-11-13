#!/usr/bin/env python3
"""plugin-check -- is the active load order loadable? A read-only check for the AGENT before it acts on plugins.

It reads the active plugins (plugins.txt, the same file the guard and xelib tools resolve) and only each plugin's TES4
header, and reports what would stop the game loading them or make a patch built on them wrong:
  missing     an active plugin that is not on disk (Data, or an enabled Mod Organizer 2 mod folder or overwrite)
  unreadable  a file whose TES4 header cannot be read
  master-missing   a master that is not on disk at all
  master-inactive  a master on disk but not active
  master-order     a master that loads AFTER the plugin that needs it (in the engine's order: master-flagged
                   plugins load before the rest, whatever plugins.txt says)
It is a check for the agent, not a player warning: Wrye Bash and the mod manager already show these to the player.
It changes nothing and never fixes anything.

Usage:  python tools/plugin-check.py [--plugins-txt PATH] [--data DIR] [--json]
Exit:   0 nothing found · 1 findings (listed) · 2 could not check (named), never "clean"
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))

from housecarl_integration import Refusal                       # noqa: E402
from housecarl_integration.order import BASE, parse_plugins, plugin_header   # noqa: E402
import skyrim_paths                                              # noqa: E402
from skyguard import paths as sg_paths                           # noqa: E402


def implicit_masters(game: Path, data: Path):
    """Plugins the engine loads without a plugins.txt line, in its order: the base masters, SkyrimVR.esm on VR, and on
    SE/AE the Creation Club list in Skyrim.ccc."""
    names = list(BASE)
    if (data / 'SkyrimVR.esm').is_file():
        names.append('SkyrimVR.esm')
    ccc = game / 'Skyrim.ccc'
    if ccc.is_file():
        for line in ccc.read_text(encoding='utf-8', errors='replace').splitlines():
            name = line.strip()
            if name and (data / name).is_file() and name.casefold() not in {n.casefold() for n in names}:
                names.append(name)
    return names


def mo2_folders(environ):
    """Enabled MO2 mod folders (highest priority first) and overwrite, from the toolkit's paths file. Empty off MO2."""
    cfg = sg_paths.read_install(str(skyrim_paths.TOOLKIT_ROOT), environ)
    mods, overwrite = cfg.first('SKYRIM_MO2_MODS'), cfg.first('SKYRIM_MO2_OVERWRITE')
    profile = cfg.first('SKYRIM_LOADORDER_DIR')
    if not mods:
        return [], None
    folders = []
    modlist = Path(profile) / 'modlist.txt' if profile else None
    if modlist is not None and modlist.is_file():
        for line in modlist.read_text(encoding='utf-8-sig', errors='replace').splitlines():
            if line.startswith('+'):
                folders.append(Path(mods) / line[1:].strip())
    else:
        return None, 'MO2 is configured but its profile modlist.txt was not found: enabled mods unknown'
    if overwrite:
        folders.insert(0, Path(overwrite))
    return folders, None


def locate(name, data: Path, folders):
    """The file the game would load for plugin `name`: the highest-priority MO2 folder holding it, else Data."""
    for folder in folders:
        hit = folder / name
        if hit.is_file():
            return hit
    hit = data / name
    return hit if hit.is_file() else None


def check(plugins_txt: Path, data: Path, game: Path, folders):
    rows = parse_plugins(plugins_txt.read_bytes())
    listed_active = [n for n, active in rows if active]
    order = []
    for name in implicit_masters(game, data) + listed_active:
        if name.casefold() not in {n.casefold() for n in order}:
            order.append(name)
    findings, headers = [], {}
    for name in order:
        path = locate(name, data, folders)
        if path is None:
            if name in BASE[2:] and name not in listed_active:
                continue                                         # a DLC master the user does not own: not loaded
            findings.append({'kind': 'missing', 'plugin': name})
            continue
        try:
            headers[name] = plugin_header(path)
        except (OSError, Refusal) as exc:
            findings.append({'kind': 'unreadable', 'plugin': name, 'detail': str(exc)})
    # The engine loads every master-flagged plugin (the ESM flag, or an .esm/.esl name) before the others, whatever
    # plugins.txt says, so a master's position is judged in that order. A plugin with no readable header is placed
    # by its extension.
    def is_master(name):
        h = headers.get(name)
        return h['master'] if h is not None else name.lower().endswith(('.esm', '.esl'))
    order = [n for n in order if is_master(n)] + [n for n in order if not is_master(n)]
    position = {n.casefold(): i for i, n in enumerate(order)}
    for i, name in enumerate(order):
        header = headers.get(name)
        if header is None:
            continue
        for master in header['masters']:
            j = position.get(master.casefold())
            if j is None:
                kind = 'master-inactive' if locate(master, data, folders) is not None else 'master-missing'
                findings.append({'kind': kind, 'plugin': name, 'master': master})
            elif j > i:
                findings.append({'kind': 'master-order', 'plugin': name, 'master': master})
    return {'plugins_txt': str(plugins_txt), 'data': str(data), 'active': len(order), 'findings': findings}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    ap.add_argument('--plugins-txt')
    ap.add_argument('--data')
    ap.add_argument('--json', action='store_true')
    args = ap.parse_args(argv)
    try:
        game, _ = skyrim_paths.game_root()
        data = Path(args.data) if args.data else game / 'Data'
        plugins_txt = Path(args.plugins_txt) if args.plugins_txt else Path(
            sg_paths.plugins_txt(os.environ, [str(skyrim_paths.TOOLKIT_ROOT / sg_paths.PATHS_FILE)]))
        if not plugins_txt.is_file():
            raise ValueError('plugins.txt not found: ' + str(plugins_txt))
        if not data.is_dir():
            raise ValueError('Data folder not found: ' + str(data))
        folders, problem = mo2_folders(os.environ)
        if problem:
            raise ValueError(problem)
        report = check(plugins_txt, data, game, folders)
    except (OSError, ValueError, Refusal) as exc:
        print('COULD NOT CHECK: ' + str(exc) + ' -- this is not a clean result.', file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(report, indent=1))
    else:
        print('%d active plugins (%s); %d finding(s)' % (report['active'], report['plugins_txt'], len(report['findings'])))
        for f in report['findings']:
            print('  %-15s %s%s%s' % (f['kind'], f['plugin'], ' <- needs ' + f['master'] if 'master' in f else '',
                                      ': ' + f['detail'] if 'detail' in f else ''))
    return 1 if report['findings'] else 0


if __name__ == '__main__':
    sys.exit(main())
