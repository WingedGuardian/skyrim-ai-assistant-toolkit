#!/usr/bin/env python3
"""Snapshot the loaded character before a mod-update wave, again after it, and say what changed.

    python tools/game_snapshot.py capture before.json      # needs the game running with the save loaded
    python tools/game_snapshot.py capture after.json
    python tools/game_snapshot.py diff before.json after.json

`capture` asks DevBench (read-only `inspect` calls, nothing is changed in the game) for the player, inventory,
quests, active effects and game state, and writes them with the time and DevBench's identity. `diff` compares two
snapshots field by field. List entries are matched by their id (formId/formID/id/editorId/name) rather than by
position, so an item that moved in a list is not reported as changed. Fields that always differ between two reads
(frame counters, timestamps) are ignored.

Port: $DEVBENCH_PORT, else Data/SKSE/Plugins/devbench/runtime.json, else 8921 for VR and 8920 for SE/AE.
Exit codes: 0 ok (diff: identical) · 1 differences found · 2 usage, DevBench unreachable or unreadable snapshot.
"""
from __future__ import annotations

import datetime
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
KINDS = ("state", "player", "inventory", "quests", "effects")
VOLATILE = {"frame", "lastTaskFrame", "pendingTasks", "time", "timestamp", "gameTime", "realTime", "uptime",
            "blockedBy", "notices"}
ID_KEYS = ("formId", "formID", "id", "editorId", "editorID", "name")


def port() -> int:
    if os.environ.get("DEVBENCH_PORT"):
        return int(os.environ["DEVBENCH_PORT"])
    runtime = ROOT / "Data" / "SKSE" / "Plugins" / "devbench" / "runtime.json"
    try:
        return int(json.loads(runtime.read_text(encoding="utf-8"))["port"])
    except (OSError, ValueError, KeyError, TypeError):
        return 8921 if (ROOT / "SkyrimVR.exe").exists() else 8920


def inspect(kind: str, base: str, timeout: float = 15.0):
    request = urllib.request.Request(base + "/api/tool/inspect", data=json.dumps({"kind": kind}).encode(),
                                     headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.load(response)


def capture(out: Path, base: str | None = None) -> dict:
    base = base or "http://127.0.0.1:{}".format(port())
    snap = {"schema": 1, "taken": datetime.datetime.now().astimezone().isoformat(), "devbench": base, "kinds": {}}
    for kind in KINDS:
        try:
            snap["kinds"][kind] = inspect(kind, base)
        except urllib.error.HTTPError as error:      # an unsupported kind is recorded, not fatal
            snap["kinds"][kind] = {"_error": "HTTP {}".format(error.code)}
    state = snap["kinds"].get("state") or {}
    if isinstance(state, dict) and state.get("playerLoaded") is False:
        raise SystemExit("no save is loaded; load the save you are keeping, then capture")
    out.write_text(json.dumps(snap, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    return snap


def _key(entry):
    if isinstance(entry, dict):
        for name in ID_KEYS:
            if entry.get(name) not in (None, ""):
                return "{}={}".format(name, entry[name])
    return None


def flatten(value, path="", out=None) -> dict:
    """path -> leaf value. Lists of id-carrying dicts are keyed by id, others by position."""
    out = {} if out is None else out
    if isinstance(value, dict):
        for name, child in value.items():
            if name in VOLATILE:
                continue
            flatten(child, "{}.{}".format(path, name) if path else name, out)
    elif isinstance(value, list):
        keys = [_key(entry) for entry in value]
        keyed = all(keys) and len(set(keys)) == len(keys)
        for index, entry in enumerate(value):
            flatten(entry, "{}[{}]".format(path, keys[index] if keyed else index), out)
        if not value:
            out[path] = []
    else:
        out[path] = value
    return out


def diff(before: dict, after: dict) -> list:
    a, b = flatten(before.get("kinds", before)), flatten(after.get("kinds", after))
    rows = []
    for path in sorted(set(a) | set(b)):
        if path not in b:
            rows.append(("removed", path, a[path], None))
        elif path not in a:
            rows.append(("added", path, None, b[path]))
        elif a[path] != b[path]:
            rows.append(("changed", path, a[path], b[path]))
    return rows


def main(argv=None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    try:
        if len(args) == 2 and args[0] == "capture":
            snap = capture(Path(args[1]))
            print("captured {} ({})".format(args[1], ", ".join(sorted(snap["kinds"]))))
            return 0
        if len(args) == 3 and args[0] == "diff":
            before, after = (json.loads(Path(p).read_text(encoding="utf-8")) for p in args[1:])
            rows = diff(before, after)
            for kind, path, old, new in rows:
                print("{:8} {}: {} -> {}".format(kind, path, json.dumps(old), json.dumps(new)))
            print("{} difference(s)".format(len(rows)))
            return 1 if rows else 0
    except (OSError, ValueError, urllib.error.URLError) as error:
        print("ERROR: {}: {}".format(type(error).__name__, error), file=sys.stderr)
        return 2
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
