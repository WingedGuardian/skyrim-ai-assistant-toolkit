#!/usr/bin/env python3
"""loot-advisor -- what LOOT would say about the active load order, without changing it. A read-only tool for the agent.

It loads the active plugins exactly as the game will (plugins.txt, the same file the guard and xelib tools resolve;
the enabled Mod Organizer 2 mod folders and overwrite when the install uses MO2), and asks libloot -- the library LOOT
itself is built on -- for:
  order    the order LOOT would sort them into, and which plugins that moves (the fewest that change position)
  issues   LOOT's masterlist messages for these plugins: warnings and errors, dirty plugins (ITMs, deleted references
           and navmeshes, with the cleaner LOOT names), requirements that are missing, incompatibilities that are both
           active, and the masterlist's general messages
The masterlist and its prelude are fetched from LOOT's GitHub repositories at run time, the newest branch this libloot
reads; the report names the branch and commit it used. When the fetch fails the last fetched copy is used and the
report says so. This tool NEVER writes the load order: it does not call libloot's set_load_order or write any of
plugins.txt, loadorder.txt or LOOT's own files. Wrye Bash / the mod manager stays the authority on order; on a save you
are keeping, read KNOWLEDGEBASE.local.md `## My preferences` before reordering anything.

Usage:  python tools/loot_advisor.py [--plugins-txt PATH] [--data DIR] [--json] [--offline]
Exit:   0 no moves and nothing but notes · 1 moves or findings (listed) · 2 could not check (named), never "clean"
        --plugins-txt must name a file called plugins.txt (libloot reads that name in the folder it is given)
"""
from __future__ import annotations

import argparse
import bisect
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import sys
import time
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parent))

import skyrim_paths                                              # noqa: E402
from skyguard import paths as sg_paths                           # noqa: E402
from libloot import libloot_loader                               # noqa: E402

REPOS = {"SkyrimVR": "skyrimvr", "SkyrimSE": "skyrimse"}
API = "https://api.github.com/repos/loot/{repo}/branches?per_page=100"
RAW = "https://raw.githubusercontent.com/loot/{repo}/{commit}/{file}"
CACHE = skyrim_paths.TOOLKIT_ROOT / ".skyguard" / "loot" / "masterlists"
UA = {"User-Agent": "skyrim-toolkit-loot-advisor", "Accept": "application/vnd.github+json"}
MAX_DOWNLOAD = 32 * 1024 * 1024        # a masterlist is ~2 MB; anything this large is not one


def _plugin_check():
    spec = importlib.util.spec_from_file_location("plugin_check", Path(__file__).resolve().parent / "plugin-check.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def game_type(game: Path) -> str:
    if (game / "SkyrimVR.exe").is_file():
        return "SkyrimVR"
    if (game / "SkyrimSE.exe").is_file():
        return "SkyrimSE"
    raise ValueError("neither SkyrimVR.exe nor SkyrimSE.exe is in " + str(game))


def _get(url: str, timeout: float = 20.0) -> bytes:
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:           # noqa: S310 (fixed https hosts above)
        data = r.read(MAX_DOWNLOAD + 1)
    if len(data) > MAX_DOWNLOAD:
        raise ValueError("%s answered more than %d bytes; refused" % (url, MAX_DOWNLOAD))
    return data


def _replace(path: Path, data: bytes) -> None:
    part = path.with_name(path.name + ".part")
    part.write_bytes(data)
    os.replace(part, path)


def newest_branch(repo: str, readable: tuple, fetch=_get):
    """(branch, commit) of the newest vX.Y branch no newer than what this libloot reads."""
    rows = json.loads(fetch(API.format(repo=repo)))
    best = None
    for row in rows:
        m = re.fullmatch(r"v(\d+)\.(\d+)", row.get("name", ""))
        if not m:
            continue
        version = (int(m.group(1)), int(m.group(2)))
        if version <= readable and (best is None or version > best[0]):
            sha = str(row["commit"]["sha"])
            if not re.fullmatch(r"[0-9a-f]{40}", sha):
                raise ValueError("loot/%s branch %s has a malformed commit id" % (repo, row["name"]))
            best = (version, row["name"], sha)
    if best is None:
        raise ValueError("loot/%s has no branch this libloot (%d.%d) can read" % ((repo,) + readable))
    return best[1], best[2]


def _branch_version(branch) -> tuple | None:
    m = re.fullmatch(r"v(\d+)\.(\d+)", str(branch or ""))
    return (int(m.group(1)), int(m.group(2))) if m else None


def masterlist(game: str, readable: tuple, offline: bool = False, fetch=_get, cache: Path | None = None,
               validate=None):
    """Paths of the masterlist and prelude to use, plus where they came from.

    validate(masterlist, prelude) parses a fresh download BEFORE meta.json names it: a branch this libloot turns out
    not to read never becomes the cached copy (the fetch then falls back to the cache). A cached copy is used only
    when its branch is one this libloot reads."""
    folder = (CACHE if cache is None else cache) / REPOS[game]
    meta_path = folder / "meta.json"
    if not offline:
        try:
            branch, commit = newest_branch(REPOS[game], readable, fetch)
            pbranch, pcommit = newest_branch("prelude", readable, fetch)
            ml = fetch(RAW.format(repo=REPOS[game], commit=commit, file="masterlist.yaml"))
            pre = fetch(RAW.format(repo="prelude", commit=pcommit, file="prelude.yaml"))
            # One folder per masterlist+prelude commit pair, and meta.json (which names it) written last: a run that
            # stops part-way leaves the previous meta pointing at a complete pair, never at a half-written one.
            store = folder / ("%s-%s" % (commit[:12], pcommit[:12]))
            store.mkdir(parents=True, exist_ok=True)
            _replace(store / "masterlist.yaml", ml)
            _replace(store / "prelude.yaml", pre)
            if validate is not None:
                try:
                    validate(store / "masterlist.yaml", store / "prelude.yaml")
                except Exception as exc:                          # noqa: BLE001 (libloot raises its own types)
                    raise ValueError("the fetched masterlist %s@%s did not parse: %s: %s"
                                     % (branch, commit[:7], type(exc).__name__, exc)) from None
            meta = {"masterlist": {"repo": REPOS[game], "branch": branch, "commit": commit},
                    "prelude": {"repo": "prelude", "branch": pbranch, "commit": pcommit},
                    "folder": store.name, "fetched_unix": int(time.time())}
            previous = json.loads(meta_path.read_text(encoding="utf-8")).get("folder") if meta_path.is_file() else None
            _replace(meta_path, json.dumps(meta, indent=1).encode("utf-8"))
            for old in folder.iterdir():          # this tool's own download cache: keep the current and previous pair
                if old.is_dir() and old.name not in (store.name, previous) and re.fullmatch(r"[0-9a-f]{12}-[0-9a-f]{12}", old.name):
                    shutil.rmtree(old)
            return store / "masterlist.yaml", store / "prelude.yaml", dict(meta, from_cache=False)
        except (OSError, ValueError, KeyError) as exc:
            reason = "%s: %s" % (type(exc).__name__, exc)
        else:
            reason = ""
    else:
        reason = "--offline"
    meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.is_file() else {}
    store = folder / str(meta.get("folder", ""))
    if not (meta.get("folder") and (store / "masterlist.yaml").is_file() and (store / "prelude.yaml").is_file()):
        raise ValueError("no masterlist: the fetch failed (%s) and none was fetched before" % reason)
    for part in ("masterlist", "prelude"):
        version = _branch_version((meta.get(part) or {}).get("branch"))
        if version is None or version > readable:
            raise ValueError("the cached %s is from branch %s, which this libloot (%d.%d) does not read; the fetch "
                             "failed (%s)" % ((part, (meta.get(part) or {}).get("branch")) + tuple(readable) + (reason,)))
    return store / "masterlist.yaml", store / "prelude.yaml", dict(meta, from_cache=True, fetch_problem=reason)


def moves(current, proposed):
    """The plugins whose relative position LOOT changes: everything outside the longest run the two orders share."""
    pos = {p: i for i, p in enumerate(current)}
    seq = [pos[p] for p in proposed if p in pos]
    tails, idx, prev = [], [], [-1] * len(seq)
    for i, x in enumerate(seq):
        k = bisect.bisect_left(tails, x)
        if k == len(tails):
            tails.append(x); idx.append(i)
        else:
            tails[k] = x; idx[k] = i
        prev[i] = idx[k - 1] if k else -1
    keep, i = set(), idx[-1] if idx else -1
    while i >= 0:
        keep.add(current[seq[i]]); i = prev[i]
    out = []
    for n, p in enumerate(proposed):
        if p not in keep:
            out.append({"plugin": p, "from": pos.get(p), "to": n, "after": proposed[n - 1] if n else None})
    return out


def _text(loot, content):
    try:
        return loot.select_message_content(content, "en").text
    except Exception:                                             # noqa: BLE001 (a message with no English text)
        return content[0].text if content else ""


PLUGIN_EXTS = (".esp", ".esm", ".esl")


def _name(f) -> str:
    n = f.name
    return n.as_str() if hasattr(n, "as_str") else str(n)


def present_check(active, data: Path, folders):
    """A plugin counts when it is active; any other file LOOT names (an SKSE DLL, `../sksevr_loader.exe`) when it is on
    disk below Data -- `..` reaching the game folder -- or in an enabled MO2 mod folder."""
    plugins = {p.casefold() for p in active}

    def present(name: str) -> bool:
        if name.lower().endswith(PLUGIN_EXTS):
            return name.casefold() in plugins
        return any((Path(base) / name).exists() for base in [data] + [Path(f) for f in folders])
    return present


def issues(loot, db, active, present=None):
    out = []
    present = present or present_check(active, Path("."), [])
    for m in db.general_messages(True, True):
        out.append({"kind": "general", "type": str(m.message_type).split(".")[-1], "text": _text(loot, m.content)})
    for name in active:
        md = db.plugin_metadata(name, True, True)
        if md is None:
            continue
        for m in md.messages:
            out.append({"kind": "message", "plugin": name, "type": str(m.message_type).split(".")[-1],
                        "text": _text(loot, m.content)})
        for d in md.dirty_info:
            out.append({"kind": "dirty", "plugin": name, "itm": d.itm_count, "deleted_references": d.deleted_reference_count,
                        "deleted_navmeshes": d.deleted_navmesh_count, "cleaner": d.cleaning_utility})
        for r in md.requirements:
            if not present(_name(r)):
                out.append({"kind": "requirement-missing", "plugin": name, "needs": _name(r),
                            "detail": r.display_name or ""})
        for c in md.incompatibilities:
            if present(_name(c)):
                out.append({"kind": "incompatible", "plugin": name, "with": _name(c), "detail": c.display_name or ""})
    return out


def advise(plugins_txt: Path, data: Path, game: Path, folders, offline=False, loot=None, fetch=_get):
    if plugins_txt.name.casefold() != "plugins.txt":
        # libloot reads the file named Plugins.txt in the folder it is given, whatever path was passed here; a report
        # naming another file would describe a file libloot never read.
        raise ValueError("libloot reads only a file named plugins.txt; %s is named otherwise (copy it to a folder as "
                         "plugins.txt and pass that)" % plugins_txt)
    loot = loot or libloot_loader.load()
    kind = game_type(game)
    readable = (loot.LIBLOOT_VERSION_MAJOR, loot.LIBLOOT_VERSION_MINOR)
    g = loot.Game(getattr(loot.GameType, kind), game, plugins_txt.parent)
    ml, pre, source = masterlist(kind, readable, offline, fetch,
                                 validate=lambda m, p: g.database().load_masterlist_with_prelude(m, p))
    if folders:
        g.set_additional_data_paths([Path(f) for f in folders])
    g.load_current_load_order_state()
    order = list(g.load_order())
    active = [p for p in order if g.is_plugin_active(p)]
    pc = _plugin_check()
    paths, missing = [], []
    for name in active:
        found = pc.locate(name, data, folders)
        (paths if found else missing).append(found or name)
    if missing:
        raise ValueError("active plugins not on disk (run plugin-check first): " + ", ".join(missing[:5]))
    g.load_plugins(paths)
    db = g.database()
    db.load_masterlist_with_prelude(ml, pre)
    proposed = list(g.sort_plugins(active))
    return {"game": kind, "libloot": loot.libloot_version(), "libloot_revision": loot.libloot_revision(),
            "masterlist": source, "plugins_txt": str(plugins_txt), "active": len(active),
            "moves": moves(active, proposed), "issues": issues(loot, db, active, present_check(active, data, folders)), "proposed": proposed,
            "note": "Advisory only: nothing was reordered. Wrye Bash / the mod manager owns the order; on a kept save "
                    "read KNOWLEDGEBASE.local.md `## My preferences` before moving anything."}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--plugins-txt")
    ap.add_argument("--data")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--offline", action="store_true", help="use the last fetched masterlist; no network")
    args = ap.parse_args(argv)
    try:
        game, _ = skyrim_paths.game_root()
        data = Path(args.data) if args.data else game / "Data"
        plugins_txt = Path(args.plugins_txt) if args.plugins_txt else Path(
            sg_paths.plugins_txt(os.environ, [str(skyrim_paths.TOOLKIT_ROOT / sg_paths.PATHS_FILE)]))
        if not plugins_txt.is_file():
            raise ValueError("plugins.txt not found: " + str(plugins_txt))
        folders, problem = _plugin_check().mo2_folders(os.environ)
        if problem:
            raise ValueError(problem)
        report = advise(plugins_txt, data, game, folders, args.offline)
    except (OSError, ValueError, libloot_loader.Unavailable) as exc:
        print("COULD NOT CHECK: " + str(exc) + " -- this is not a clean result.", file=sys.stderr)
        return 2
    except Exception as exc:                                      # noqa: BLE001 (libloot raises its own types)
        print("COULD NOT CHECK: libloot: %s: %s -- this is not a clean result." % (type(exc).__name__, exc),
              file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(report, indent=1))
    else:
        src = report["masterlist"]
        print("LOOT %s on %d active plugins; masterlist %s %s@%s%s" % (
            report["libloot"], report["active"], src["masterlist"]["repo"], src["masterlist"]["branch"],
            src["masterlist"]["commit"][:7], " (CACHED: " + src.get("fetch_problem", "") + ")" if src.get("from_cache") else ""))
        print("%d plugin(s) LOOT would move; %d issue(s)" % (len(report["moves"]), len(report["issues"])))
        for m in report["moves"]:
            print("  move   %s -> after %s" % (m["plugin"], m["after"] or "(top)"))
        for i in report["issues"]:
            detail = {k: v for k, v in i.items() if k not in ("kind", "plugin")}
            print("  %-19s %s %s" % (i["kind"], i.get("plugin", ""), json.dumps(detail, ensure_ascii=False)))
        print(report["note"])
    # A masterlist "say" message is information, not a finding: it is listed, and does not make the exit 1.
    findings = [i for i in report["issues"] if str(i.get("type", "")).lower() != "say"]
    return 1 if report["moves"] or findings else 0


if __name__ == "__main__":
    sys.exit(main())
