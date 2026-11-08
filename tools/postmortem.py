#!/usr/bin/env python3
"""postmortem -- what happened in the last game session, from the logs it left: one read-only report.

  session  the newest Papyrus log: when it opened, its last entry
  crash    the newest CrashLoggerSSE dump, whether it is from THIS session, its signature and the knowledgebase's
           ruling on it (crash-triage's KNOWN list)
  papyrus  that log's errors and warnings, the most frequent shapes first (papyrus-triage's buckets)
  skse     every SKSE plugin the session did NOT load, and why (skse-load-status)
It reuses those three tools' parsers, so a fix to one is a fix here. It changes nothing.

Usage:  python tools/postmortem.py [--top N] [--json]
Exit:   0 report printed · 2 no session log found (named)
"""
from __future__ import annotations

import argparse
from datetime import datetime
import importlib.util
import json
from pathlib import Path
import re
import sys

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import skyrim_paths  # noqa: E402


def _load(name):
    spec = importlib.util.spec_from_file_location(name.replace('-', '_'), HERE / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


crash_triage, papyrus_triage, skse_status = _load('crash-triage'), _load('papyrus-triage'), _load('skse-load-status')
STAMP = re.compile(r'^\[(\d{2}/\d{2}/\d{4} - \d{2}:\d{2}:\d{2}[AP]M)\]')


def stamp(line):
    m = STAMP.match(line)
    return datetime.strptime(m.group(1), '%m/%d/%Y - %I:%M:%S%p') if m else None


def newest(paths):
    paths = [p for p in paths if p.is_file()]
    return max(paths, key=lambda p: p.stat().st_mtime) if paths else None


def report(papyrus_dir: Path, skse_dir: Path, top: int):
    log = newest(papyrus_dir.glob('Papyrus.*.log'))
    if log is None:
        raise FileNotFoundError('no Papyrus.*.log in ' + str(papyrus_dir))
    lines = log.read_text(encoding='utf-8', errors='replace').splitlines()
    stamps = [s for s in map(stamp, lines) if s]
    opened, last = (stamps[0], stamps[-1]) if stamps else (None, None)
    out = {'session': {'papyrus_log': str(log), 'opened': opened and opened.isoformat(' '),
                       'last_entry': last and last.isoformat(' ')}}
    # Crash: the newest dump, and whether it came after this session opened.
    dump = newest(p for p in skse_dir.iterdir() if crash_triage.CRASH_NAME_RE.match(p.name)) if skse_dir.is_dir() else None
    crash = None
    if dump is not None:
        when = datetime.fromtimestamp(dump.stat().st_mtime)
        parsed = crash_triage.parse(dump)
        crash = {'file': str(dump), 'written': when.isoformat(' ', 'seconds'),
                 'this_session': bool(opened and when >= opened)}
        if parsed is None:
            crash['signature'] = 'no exception line (not a crash dump this tool can read)'
        else:
            status, note = crash_triage.known(parsed)
            crash.update(signature='%s+%s' % (parsed['module'], parsed['offset']), type=parsed['type'],
                         known=status, note=note, complete=parsed['complete'], top_frames=parsed['frames'][:5])
    out['crash'] = crash
    # Papyrus: errors and warnings by shape.
    entries, _, _ = papyrus_triage.parse(log)
    papyrus = {}
    for severity in ('error', 'warning'):
        subset = [(msg, stack) for sev, msg, stack in entries if sev == severity]
        shapes, who, _, _ = papyrus_triage.bucket(subset)
        papyrus[severity] = {'count': len(subset),
                             'top': [{'shape': shape, 'count': n, 'benign': papyrus_triage.benign_reason(shape),
                                      'from': who[shape].most_common(1)[0][0] if who[shape] else ''}
                                     for shape, n in shapes.most_common(top)]}
    out['papyrus'] = papyrus
    # SKSE: what did not load.
    skse_log, looked, _ = skse_status.find_log()
    if skse_log is None:
        out['skse'] = {'log': None, 'note': looked}
    else:
        rows, _, checked, _ = skse_status.parse(skse_log.read_text(encoding='utf-8', errors='replace').splitlines())
        out['skse'] = {'log': str(skse_log), 'checked': checked,
                       'not_loaded': [{'dll': r['dll'], 'kind': r['kind'], 'status': r['status']}
                                      for r in rows if r['kind'] != 'loaded']}
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    ap.add_argument('--top', type=int, default=5)
    ap.add_argument('--json', action='store_true')
    args = ap.parse_args(argv)
    papyrus_dir, _ = skyrim_paths.resolve_dir('PAPYRUS_LOG_DIR', 'Logs/Script')
    skse_dir, _ = skyrim_paths.resolve_dir('SKSE_CRASH_DIR', 'SKSE')
    try:
        out = report(Path(papyrus_dir), Path(skse_dir), args.top)
    except (OSError, FileNotFoundError) as exc:
        print('COULD NOT REPORT: ' + str(exc), file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(out, indent=1, default=str))
        return 0
    s = out['session']
    print('Session: %s  (opened %s, last entry %s)' % (s['papyrus_log'], s['opened'], s['last_entry']))
    c = out['crash']
    if c is None:
        print('Crash: no crash dump found')
    else:
        print('Crash: %s %s, %s' % ('THIS SESSION' if c['this_session'] else 'older dump', c['written'], c['file']))
        if 'known' in c:
            print('  %s  %s  [%s] %s' % (c['signature'], c['type'], c['known'], c['note']))
            for frame in c['top_frames']:
                print('    ' + str(frame))
        else:
            print('  ' + c['signature'])
    for severity in ('error', 'warning'):
        p = out['papyrus'][severity]
        print('Papyrus %ss: %d' % (severity, p['count']))
        for row in p['top']:
            print('  %5d  %s%s%s' % (row['count'], row['shape'][:110], '  (from ' + row['from'] + ')' if row['from'] else '',
                                    '  [benign: ' + row['benign'] + ']' if row['benign'] else ''))
    k = out['skse']
    if k.get('log') is None:
        print('SKSE: no log (' + k['note'] + ')')
    else:
        print('SKSE: %d plugins checked, %d not loaded (%s)' % (k['checked'], len(k['not_loaded']), k['log']))
        for row in k['not_loaded']:
            print('  %-40s %s: %s' % (row['dll'], row['kind'], row['status']))
    return 0


if __name__ == '__main__':
    sys.exit(main())
