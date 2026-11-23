#!/usr/bin/env python3
"""Talk to DevBench (port 8921) only when the game answering is YOUR game.

Port 8921 belongs to whichever SkyrimVR.exe is running. A helper that outlives its own run (a wait loop, a retry,
a follow-up script) would otherwise drive the next session's game: 2026-11-23 a leftover loop loaded a save into
another session's benchmark. Every call here first reads GET /api/health (answered off the main thread) and refuses
unless its `pid` is the game process your session launched.

    python tools/devbench_own.py pid [--creator PID]          # the game pid of the active vr-headless session
    python tools/devbench_own.py check --pid N                # exit 0 if 8921 is that game, 1 if not / no game
    python tools/devbench_own.py call TOOL JSON --pid N       # POST /api/tool/TOOL only if 8921 is that game

`pid` reads tools/.vr-headless-sessions/active.json -> the manifest's recorded SkyrimVR process. With --creator it
also requires that the session was started by that launcher process (manifest creator_pid), so a session started by
someone else is never mistaken for yours. Python callers: `from devbench_own import guard, call`.
Exit codes: 0 ok, 1 refused (not your game / no game / session restored), 2 usage or unreadable state.
"""
import argparse
import json
import sys
from pathlib import Path
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent.parent
SESSIONS = ROOT / "tools/.vr-headless-sessions"
BASE = "http://127.0.0.1:8921"


class NotYours(Exception):
    """The game on 8921 is not the expected one (or there is none)."""


def session_pid(creator=None, sessions=SESSIONS):
    """The SkyrimVR pid recorded by the active, unrestored vr-headless session."""
    active = sessions / "active.json"
    if not active.exists():
        raise NotYours("no active vr-headless session")
    path = Path(json.loads(active.read_text(encoding="utf-8"))["manifest"])     # vr-headless writes {"manifest": path}
    path.resolve().relative_to(sessions.resolve())
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if manifest.get("restored_at"):
        raise NotYours("the active session was already restored at %s" % manifest["restored_at"])
    if creator is not None and manifest.get("creator_pid") != int(creator):
        raise NotYours("the active session was started by process %s, not %s" % (manifest.get("creator_pid"), creator))
    games = [p for p in manifest.get("processes", []) if p.get("ProcessName") == "SkyrimVR"]
    if not games:
        raise NotYours("the active session has not recorded its game yet")
    return int(games[-1]["Id"])


def health(timeout=3):
    try:
        with urlopen(BASE + "/api/health", timeout=timeout) as answer:
            return json.load(answer)
    except OSError as error:
        raise NotYours("DevBench is not answering: %s" % error)


def guard(pid, read=health):
    """Raise NotYours unless the game answering on 8921 is process `pid`."""
    state = read()
    if int(state.get("pid", -1)) != int(pid):
        raise NotYours("port 8921 is game pid %s, not %s: refusing" % (state.get("pid"), pid))
    return state


def call(tool, body, pid, timeout=30, read=health):
    guard(pid, read)
    request = Request("%s/api/tool/%s" % (BASE, tool), json.dumps(body).encode(), {"Content-Type": "application/json"})
    with urlopen(request, timeout=timeout) as answer:
        return json.load(answer)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("pid")
    p.add_argument("--creator", type=int)
    p = sub.add_parser("check")
    p.add_argument("--pid", type=int, required=True)
    p = sub.add_parser("call")
    p.add_argument("tool")
    p.add_argument("body")
    p.add_argument("--pid", type=int, required=True)
    p.add_argument("--timeout", type=int, default=30)
    args = parser.parse_args(argv)
    try:
        if args.command == "pid":
            result = {"pid": session_pid(args.creator)}
        elif args.command == "check":
            result = guard(args.pid)
        else:
            result = call(args.tool, json.loads(args.body), args.pid, args.timeout)
    except NotYours as error:
        print(json.dumps({"refused": str(error)}))
        return 1
    except (OSError, ValueError, KeyError) as error:
        print(json.dumps({"error": "%s: %s" % (type(error).__name__, error)}))
        return 2
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
