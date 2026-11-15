#!/usr/bin/env python3
"""Talk to ReSaverHelper (ReSaver Plus's SKSE plugin) in the RUNNING game, over its local pipe.

    python tools/resaverhelper.py hello                      # helper version, game, edition, in game?
    python tools/resaverhelper.py quest 0x0002610C           # editor id, stage, running, completed, objectives
    python tools/resaverhelper.py followers                  # who follows you, and where
    python tools/resaverhelper.py progress | resolve <Plugin.esp> <localid> | getoption <name>
    python tools/resaverhelper.py setstage 0x0002610C 20 --apply   # changes the game: needs --apply
    python tools/resaverhelper.py refresh --apply            # journal refresh (missing objectives only)

Reads run freely. Commands that change the running game (setstage, complete, stop, reset, refresh, gather,
controls, followerset, teammate, beastrevert, ghostremove, notify, setoption) print what they would send unless
--apply is given. Saving (SAVEGAME, RESCUE) and Transplant (SNAPSHOT, APPLY) are refused here: saving stays a
manual player action, and Transplant belongs in ReSaver Plus's own window.

The pipe is \\\\.\\pipe\\ReSaverHelper (owner-only, local). Protocol: ReSaverPP helper/src/Protocol.h -- one tab-separated
request line, one reply line: <id> OK <fields...> or <id> ERR <message>. Windows only.
Exit codes: 0 OK reply (or dry run) · 1 ERR reply · 2 usage, refused command, or no helper listening.
"""
from __future__ import annotations

import sys

PIPE = r"\\.\pipe\ReSaverHelper"
READS = {"HELLO": 0, "QUEST": 1, "PROGRESS": 0, "FOLLOWERS": 0, "RESOLVE": 2, "GETOPTION": 1}
WRITES = {"SETSTAGE": (2, 4), "COMPLETE": (1, 3), "STOP": (1, 3), "RESET": (1, 3), "REFRESH": (0, 1),
          "GATHER": (0, 2), "CONTROLS": (0, 1), "FOLLOWERSET": (1, 2), "TEAMMATE": (2, 3), "BEASTREVERT": (1, 2),
          "GHOSTREMOVE": (1, 2), "NOTIFY": (1, 2), "SETOPTION": (2, 2)}
REFUSED = {"SAVEGAME": "saving stays a manual player action", "RESCUE": "RESCUE saves the game",
           "SNAPSHOT": "Transplant belongs in ReSaver Plus's own window", "APPLY": "Transplant belongs in ReSaver Plus's own window"}
# Defaults the protocol needs that a caller rarely cares about: <notify> and <message> (see Protocol.h).
DEFAULT_TAIL = {"SETSTAGE": ["hud", ""], "COMPLETE": ["hud", ""], "STOP": ["hud", ""], "RESET": ["hud", ""],
                "REFRESH": ["hud"], "CONTROLS": ["hud"], "GATHER": ["hud"], "FOLLOWERSET": ["hud"], "TEAMMATE": ["hud"],
                "BEASTREVERT": ["hud"]}


class Refused(Exception):
    pass


def escape(field: str) -> str:
    return str(field).replace("\\", "\\\\").replace("\t", "\\t").replace("\n", "\\n")


def unescape(field: str) -> str:
    out, i = [], 0
    while i < len(field):
        if field[i] == "\\" and i + 1 < len(field):
            out.append({"t": "\t", "n": "\n", "\\": "\\"}.get(field[i + 1], field[i + 1]))
            i += 2
        else:
            out.append(field[i])
            i += 1
    return "".join(out)


def build(command: str, args: list, apply: bool, request_id: int = 1) -> str:
    """The request line for COMMAND, after the safety rules. Raises Refused."""
    name = command.upper()
    if name in REFUSED:
        raise Refused("{} is refused here: {}".format(name, REFUSED[name]))
    if name in READS:
        if len(args) != READS[name]:
            raise Refused("{} takes {} argument(s)".format(name, READS[name]))
    elif name in WRITES:
        low, high = WRITES[name]
        if not low <= len(args) <= high:
            raise Refused("{} takes {} to {} argument(s)".format(name, low, high))
        args = list(args) + DEFAULT_TAIL.get(name, [])[len(args) - low:]
        if not apply:
            raise Refused("DRY RUN (changes the running game; add --apply): " + "\t".join([name, *map(str, args)]))
    else:
        raise Refused("unknown command {}".format(name))
    return "\t".join([str(request_id), name, *map(escape, args)]) + "\n"


def parse(reply: str, request_id: int = 1):
    """(ok, fields) from one reply line."""
    parts = reply.rstrip("\r\n").split("\t")
    if len(parts) < 2 or parts[0] != str(request_id) or parts[1] not in ("OK", "ERR"):
        raise ValueError("unexpected reply: {!r}".format(reply[:200]))
    return parts[1] == "OK", [unescape(p) for p in parts[2:]]


def exchange(line: str, path: str = PIPE, timeout: float = 30.0) -> str:
    """One request, one reply, over the helper's pipe."""
    if sys.platform != "win32":
        raise OSError("ReSaverHelper's pipe exists only on Windows")
    with open(path, "r+b", buffering=0) as pipe:
        pipe.write(line.encode("utf-8"))
        data = b""
        while not data.endswith(b"\n"):
            chunk = pipe.read(65536)
            if not chunk:
                break
            data += chunk
    return data.decode("utf-8", "replace")


def main(argv=None, transport=exchange) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    apply = "--apply" in args
    args = [a for a in args if a != "--apply"]
    if not args or args[0] in ("-h", "--help", "help"):
        print(__doc__)
        return 0 if args else 2
    try:
        line = build(args[0], args[1:], apply)
    except Refused as message:
        print(message, file=sys.stderr if not str(message).startswith("DRY RUN") else sys.stdout)
        return 0 if str(message).startswith("DRY RUN") else 2
    try:
        ok, fields = parse(transport(line))
    except FileNotFoundError:
        print("no ReSaverHelper is listening: the game must be running with ReSaverHelper installed", file=sys.stderr)
        return 2
    except (OSError, ValueError) as error:
        print("ERROR: {}".format(error), file=sys.stderr)
        return 2
    print(("OK" if ok else "ERR") + ("\t" + "\t".join(fields) if fields else ""))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
