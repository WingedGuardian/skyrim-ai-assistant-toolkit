#!/usr/bin/env python3
"""Shared ownership claims: who holds a shared slot, since when, and whether that holder is still alive.

Several agent sessions share one install. Before one of them changes shared configuration or runs a live test, it
claims a SLOT in a small JSON ledger; the others leave that work alone until the slot is free again. A slot holding
only a name cannot say whether its holder is still working, so a claim made through this module also records:

    claim_details["slot:<slot>"] = {owner, kind, pid, pid_start, session, reason, claimed_at, last_activity}

The slot key itself stays a plain owner string (or null), so tools that only read the slot keep working.

Another session may release someone else's claim ONLY when all of these hold (`status` verdict quiet-releasable):
  1. the recorded holding process, if one was recorded, is not running (same PID and start time);
  2. no game, VR or loader process is running;
  3. at least QUIET_MINUTES have passed since the claim's last activity.
A claim with no details (written by an older tool: a name only) is `legacy-ask-owner` and is never released by age.
So are details that another tool may have outdated: every write here stamps the file's `updated` field and records
that stamp; when the file's `updated` no longer matches, some other writer changed the file since, and the details
cannot be trusted until their owner's next `claim` or `touch`. Anything that cannot be read is `unknown`, never
releasable. Every release is appended to `<ledger>.claims-log.jsonl` and remembered under `claim_releases`, so the
old owner's next `check` or `touch` says who released it and when.

A holder keeps its claim alive with `touch` (or a repeated `claim`); `check` only reads.

    python tools/shared_claims.py [--ledger FILE] status
    python tools/shared_claims.py [--ledger FILE] claim SLOT --owner NAME --reason TEXT (--pid N | --session-claim)
    python tools/shared_claims.py [--ledger FILE] touch --owner NAME
    python tools/shared_claims.py [--ledger FILE] check SLOT --owner NAME
    python tools/shared_claims.py [--ledger FILE] release SLOT --owner NAME
    python tools/shared_claims.py [--ledger FILE] release-stale SLOT --by NAME

Which ledger: --ledger, else $SHARED_CLAIMS_LEDGER, else the `ledger = PATH` line of the machine-local
<toolkit>/.claude/shared-claims.conf (a relative PATH is taken from the toolkit folder), else
<toolkit>/.claude/shared-claims.json. Lock: $SHARED_CLAIMS_LOCK, else the ledger with its suffix replaced by
`.gateway.lock` (one lock for every cooperating writer of the file).
Exit codes: 0 ok, 3 refused (the JSON on stdout says why), 2 usage, unreadable ledger or unknown state.
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

TOOLKIT_ROOT = Path(__file__).resolve().parent.parent
CONF = TOOLKIT_ROOT / ".claude" / "shared-claims.conf"
DEFAULT_LEDGER = TOOLKIT_ROOT / ".claude" / "shared-claims.json"
SLOTS = ("active_shared_config_owner", "active_live_test_owner")
DETAILS, RELEASES, WRITTEN = "claim_details", "claim_releases", "_written_updated"
QUIET_MINUTES = 30
LOCK_TIMEOUT = 15.0
# The game, its loader and the SteamVR runtime (vr-headless VR_NAMES). A running one means the install is in use.
GAME_IMAGES = ("skyrimvr.exe", "sksevr_loader.exe", "vrserver.exe", "vrmonitor.exe", "vrcompositor.exe",
               "vrstartup.exe", "vrwebhelper.exe", "vrdashboard.exe", "steamvr_room_setup.exe")
CREATE_NO_WINDOW = 0x08000000


def key(slot: str) -> str:
    """The details key for a slot. Never the slot name itself: other tools match '"<slot>":' and need one hit."""
    return "slot:" + slot


class Refused(Exception):
    """The claim operation is not allowed; the message says why."""


class Unknown(Exception):
    """A fact the rule needs could not be read. Never treated as 'dead' or 'not running'."""


def now_utc() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def stamp(moment: datetime.datetime) -> str:
    return moment.astimezone(datetime.timezone.utc).isoformat()


def parse(value: str) -> datetime.datetime:
    moment = datetime.datetime.fromisoformat(value)
    return moment if moment.tzinfo else moment.replace(tzinfo=datetime.timezone.utc)


# --------------------------------------------------------------------------- processes

def process_start(pid: int):
    """The creation time of a running PID as a string, or None when it is not running. Unknown raises."""
    if sys.platform != "win32":
        try:
            os.kill(int(pid), 0)
        except ProcessLookupError:
            return None
        except OSError as error:
            raise Unknown("process {} could not be checked: {}".format(pid, error)) from error
        try:
            ticks = Path("/proc/{}/stat".format(int(pid))).read_text().rsplit(")", 1)[1].split()[19]
            return "ticks:" + ticks
        except (OSError, IndexError) as error:
            raise Unknown("process {} start could not be read: {}".format(pid, error)) from error
    return _windows_start(int(pid))


def _windows_start(pid: int):
    """Self-contained (the toolkit's process helpers differ between installs): OpenProcess + GetProcessTimes."""
    import ctypes
    from ctypes import wintypes
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel32.OpenProcess(0x1000 | 0x00100000, False, pid)   # QUERY_LIMITED_INFORMATION | SYNCHRONIZE
    if not handle:
        error = ctypes.get_last_error()
        if error == 87:                                                # ERROR_INVALID_PARAMETER: no such PID
            return None
        raise Unknown("process {} could not be opened (error {})".format(pid, error))
    try:
        times = [wintypes.FILETIME() for _ in range(4)]
        if not kernel32.GetProcessTimes(handle, *[ctypes.byref(t) for t in times]):
            raise Unknown("process {} times could not be read (error {})".format(pid, ctypes.get_last_error()))
        state = kernel32.WaitForSingleObject(handle, 0)
        if state == 0:                                                 # signalled: it has exited
            return None
        if state != 0x102:                                             # not WAIT_TIMEOUT: cannot tell
            raise Unknown("process {} state could not be read".format(pid))
        ticks = (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime
        created = datetime.datetime(1601, 1, 1, tzinfo=datetime.timezone.utc) + datetime.timedelta(
            microseconds=ticks // 10)
        return stamp(created)
    finally:
        kernel32.CloseHandle(handle)


def process_alive(pid: int, start: str) -> bool:
    """Same PID AND same start time as recorded. Unknown raises -- including a start recorded on another platform."""
    current = process_start(pid)
    if current is None:
        return False
    if current.startswith("ticks:") != start.startswith("ticks:"):
        raise Unknown("process {} was recorded on another platform; its life cannot be checked here".format(pid))
    if current.startswith("ticks:"):
        return current == start
    return abs((parse(current) - parse(start)).total_seconds()) <= 0.001


def running_games() -> list:
    """Image names of running game/VR/loader processes. A failed read raises Unknown, never returns []."""
    if sys.platform != "win32":
        raise Unknown("the Windows game processes cannot be seen from here")
    try:
        result = subprocess.run(["tasklist", "/FO", "CSV", "/NH"], capture_output=True, text=True, check=True,
                                timeout=20, creationflags=CREATE_NO_WINDOW)
    except (OSError, subprocess.SubprocessError) as error:
        raise Unknown("the process list could not be read: {}".format(error)) from error
    import csv
    import io
    names = [row[0].casefold() for row in csv.reader(io.StringIO(result.stdout)) if row]
    if not names:
        raise Unknown("the process list came back empty")
    return sorted({name for name in names if name in GAME_IMAGES})


# --------------------------------------------------------------------------- ledger file

def _conf_ledger():
    """The `ledger = PATH` of the machine-local conf file, or None. A conf without that line raises."""
    if not CONF.is_file():
        return None
    for line in CONF.read_text(encoding="utf-8-sig").splitlines():
        name, sep, value = line.partition("=")
        if sep and name.strip() == "ledger" and value.strip():
            path = Path(value.strip().strip('"'))
            return path if path.is_absolute() else TOOLKIT_ROOT / path
    raise ValueError("{} has no `ledger = PATH` line".format(CONF))


def resolve_ledger(explicit=None):
    """(path, configured). configured is False only for the built-in default, which may legitimately not exist."""
    if explicit:
        return Path(explicit), True
    if os.environ.get("SHARED_CLAIMS_LEDGER"):
        return Path(os.environ["SHARED_CLAIMS_LEDGER"]), True
    conf = _conf_ledger()
    if conf is not None:
        return conf, True
    return DEFAULT_LEDGER, False


def ledger_path(explicit=None) -> Path:
    return resolve_ledger(explicit)[0]


def lock_path(ledger: Path) -> Path:
    return Path(os.environ.get("SHARED_CLAIMS_LOCK") or ledger.with_suffix(".gateway.lock"))


class _Lock:
    """Exclusive lock on byte 0 of the lock file, the same byte the other cooperating writers lock."""

    def __init__(self, path: Path, timeout: float = LOCK_TIMEOUT):
        self.path, self.timeout, self.stream = path, timeout, None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.is_symlink():
            raise Refused("lock file is a link: {}".format(self.path))
        self.stream = self.path.open("a+b")
        if self.stream.seek(0, 2) == 0:
            self.stream.write(b"\0")
            self.stream.flush()
        deadline = time.monotonic() + self.timeout
        while True:
            self.stream.seek(0)
            try:
                if sys.platform == "win32":
                    import msvcrt
                    msvcrt.locking(self.stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.lockf(self.stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB, 1, 0)
                return self
            except OSError:
                if time.monotonic() >= deadline:
                    self.stream.close()
                    raise Refused("the ledger lock is busy: {}".format(self.path))
                time.sleep(0.1)

    def __exit__(self, *_):
        try:
            self.stream.seek(0)
            if sys.platform == "win32":
                import msvcrt
                msvcrt.locking(self.stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.lockf(self.stream.fileno(), fcntl.LOCK_UN, 1, 0)
        finally:
            self.stream.close()


class Ledger:
    """The ledger's JSON plus the byte layout to write it back in (BOM, newline style)."""

    def __init__(self, path: Path):
        self.path = path
        if path.exists():
            raw = path.read_bytes()
            self.bom = raw.startswith(b"\xef\xbb\xbf")
            self.crlf = raw.count(b"\r\n") > raw.count(b"\n") // 2
            self.value = json.loads(raw.decode("utf-8-sig"))
        else:
            self.bom, self.crlf = False, False
            self.value = {"updated": None, **{slot: None for slot in SLOTS}}
        if type(self.value) is not dict:
            raise ValueError("the ledger is not a JSON object: {}".format(path))
        for name in (DETAILS, RELEASES):
            if name in self.value and type(self.value[name]) is not dict:
                raise ValueError("the ledger's {} is not an object: {}".format(name, path))

    def table(self, name) -> dict:
        return self.value.setdefault(name, {})

    def trusted(self) -> bool:
        """Whether the details were written by this module and nothing has rewritten the file since."""
        written = (self.value.get(DETAILS) or {}).get(WRITTEN)
        return written is not None and written == self.value.get("updated")

    def details(self, slot):
        row = (self.value.get(DETAILS) or {}).get(key(slot))
        return row if isinstance(row, dict) else None

    def save(self, moment) -> None:
        # Details for a free slot are always dropped; the stamp binds the remaining ones to this exact file state.
        table = self.table(DETAILS)
        for slot in SLOTS:
            if self.value.get(slot) is None:
                table.pop(key(slot), None)
        mark = moment.astimezone().isoformat(timespec="microseconds")
        self.value["updated"] = mark
        table[WRITTEN] = mark
        text = json.dumps(self.value, indent=1, ensure_ascii=False) + "\n"
        if self.crlf:
            text = text.replace("\n", "\r\n")
        data = (b"\xef\xbb\xbf" if self.bom else b"") + text.encode("utf-8")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), prefix="." + self.path.name + ".")
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
            for attempt in range(20):        # a reader holding the file open briefly blocks a replace on Windows
                try:
                    os.replace(tmp, str(self.path))
                    break
                except PermissionError:
                    if attempt == 19:
                        raise
                    time.sleep(0.05)
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)


def _log(ledger: Path, row: dict) -> None:
    """Append to the release history. The ledger change is already made; a failed append warns, never undoes it."""
    try:
        with open(str(ledger) + ".claims-log.jsonl", "a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    except OSError as error:
        print("warning: claims log not written: {}".format(error), file=sys.stderr)


def _owner_name(owner) -> str:
    if not owner or not isinstance(owner, str) or not owner.isascii() or not owner.isprintable():
        raise Refused("an owner name must be non-empty printable ASCII")
    return owner


# --------------------------------------------------------------------------- the rule

def verdict(ledger: Ledger, slot: str, now=None, alive=process_alive, games=running_games) -> dict:
    """One slot's holder and verdict: free, live, quiet-releasable, legacy-ask-owner or unknown."""
    now = now or now_utc()
    owner = ledger.value.get(slot)
    row = {"slot": slot, "owner": owner}
    if owner is None:
        return dict(row, verdict="free")
    details = ledger.details(slot)
    if details is None or details.get("owner") != owner:
        return dict(row, verdict="legacy-ask-owner",
                    why="the claim records no holder or activity; ask its owner or the user")
    if not ledger.trusted():
        return dict(row, verdict="legacy-ask-owner", details=details,
                    why="another tool rewrote the ledger since this claim was recorded; ask its owner or the user")
    row.update(details=details)
    try:
        quiet = (now - parse(details["last_activity"])).total_seconds() / 60.0
    except (KeyError, TypeError, ValueError):
        return dict(row, verdict="unknown", why="the claim's last activity cannot be read")
    row["quiet_minutes"] = round(quiet, 1)
    pid = details.get("pid")
    if pid is not None:
        try:
            if alive(int(pid), str(details.get("pid_start") or "")):
                return dict(row, verdict="live", why="the holding process {} is running".format(pid))
        except (Unknown, ValueError, TypeError) as error:
            return dict(row, verdict="unknown", why=str(error))
    try:
        running = games()
    except Unknown as error:
        return dict(row, verdict="unknown", why=str(error))
    if running:
        return dict(row, verdict="live", why="game or VR running: " + ", ".join(running))
    if quiet < QUIET_MINUTES:
        return dict(row, verdict="live", why="last activity {:.1f} min ago (< {})".format(quiet, QUIET_MINUTES))
    return dict(row, verdict="quiet-releasable",
                why="no holding process, nothing running, quiet {:.1f} min".format(quiet))


def status(path=None, now=None, alive=process_alive, games=running_games) -> list:
    ledger = Ledger(ledger_path(path))
    return [verdict(ledger, slot, now, alive, games) for slot in SLOTS]


def claim(slot, owner, reason, pid=None, session=None, path=None, also_free=(), now=None) -> dict:
    """Claim SLOT for OWNER. Refuses a foreign holder (of SLOT or of any slot in also_free). Re-entrant for OWNER."""
    if slot not in SLOTS:
        raise Refused("unknown slot {!r}".format(slot))
    owner = _owner_name(owner)
    ledger_file = ledger_path(path)
    now = now or now_utc()
    start = process_start(pid) if pid is not None else None
    if pid is not None and start is None:
        raise Refused("the claiming process {} is not running".format(pid))
    with _Lock(lock_path(ledger_file)):
        ledger = Ledger(ledger_file)
        for name in (slot, *also_free):
            if ledger.value.get(name) not in (None, owner):
                raise Refused("{} is held by {!r}".format(name, ledger.value.get(name)))
        previous = ledger.details(slot)
        same = (previous is not None and previous.get("owner") == owner and ledger.value.get(slot) == owner
                and previous.get("claimed_at"))
        row = {"owner": owner, "kind": "process" if pid is not None else "session", "pid": pid, "pid_start": start,
               "session": session, "reason": reason,
               "claimed_at": previous["claimed_at"] if same else stamp(now), "last_activity": stamp(now)}
        ledger.value[slot] = owner
        ledger.table(DETAILS)[key(slot)] = row
        ledger.table(RELEASES).pop(key(slot), None)
        ledger.save(now)
        _log(ledger_file, {"at": stamp(now), "event": "claim", "slot": slot, **row})
    return row


def touch(owner, path=None, now=None) -> list:
    """Record activity on every slot OWNER holds. Refuses when it holds none (it has lost its claim)."""
    ledger_file = ledger_path(path)
    now = now or now_utc()
    with _Lock(lock_path(ledger_file)):
        ledger = Ledger(ledger_file)
        held = [slot for slot in SLOTS if ledger.value.get(slot) == owner and (ledger.details(slot) or {}).get(
            "owner") == owner]
        if not held:
            raise Refused(_taken_message(ledger, None, owner))
        for slot in held:
            ledger.details(slot)["last_activity"] = stamp(now)
        ledger.save(now)
    return held


def check(slot, owner, path=None) -> dict:
    """The holder's read-only check before an operation: refuses, naming who released it, when OWNER lost SLOT."""
    ledger = Ledger(ledger_path(path))
    if ledger.value.get(slot) != owner:
        raise Refused(_taken_message(ledger, slot, owner))
    return {"slot": slot, "owner": owner, "held": True}


def _taken_message(ledger, slot, owner) -> str:
    releases = ledger.value.get(RELEASES) or {}
    for name in ([slot] if slot else SLOTS):
        last = releases.get(key(name))
        if isinstance(last, dict) and last.get("prior_owner") == owner:
            return "{} no longer holds {}: released by {} at {} ({})".format(
                owner, name, last.get("by"), last.get("at"), last.get("why"))
    held = ledger.value.get(slot) if slot else None
    return "{} does not hold {}{}".format(owner, slot or "any slot", "; held by {!r}".format(held) if held else "")


def release(slot, owner, path=None, now=None) -> dict:
    """The owner's own release. A free slot is a no-op; a foreign holder refuses."""
    ledger_file = ledger_path(path)
    now = now or now_utc()
    with _Lock(lock_path(ledger_file)):
        ledger = Ledger(ledger_file)
        holder = ledger.value.get(slot)
        if holder is None:
            return {"slot": slot, "released": False, "already_free": True}
        if holder != owner:
            raise Refused("{} is held by {!r}, not {!r}".format(slot, holder, owner))
        ledger.value[slot] = None
        ledger.save(now)
        _log(ledger_file, {"at": stamp(now), "event": "release", "slot": slot, "owner": owner})
    return {"slot": slot, "released": True}


def release_stale(slot, by, path=None, now=None, alive=process_alive, games=running_games) -> dict:
    """Release another session's claim, only when the rule says quiet-releasable. Logged and remembered."""
    by = _owner_name(by)
    ledger_file = ledger_path(path)
    now = now or now_utc()
    try:                                  # the slow process list is read before the lock, which others wait on
        seen = games()
        observed = lambda: seen  # noqa: E731
    except Unknown as error:
        failure = error

        def observed():
            raise failure
    with _Lock(lock_path(ledger_file)):
        ledger = Ledger(ledger_file)
        result = verdict(ledger, slot, now, alive, observed)
        if result["verdict"] != "quiet-releasable":
            raise Refused("{} is {}: {}".format(slot, result["verdict"], result.get("why", "")))
        record = {"prior_owner": result["owner"], "by": by, "at": stamp(now), "why": result["why"],
                  "prior_details": result["details"]}
        ledger.value[slot] = None
        ledger.table(RELEASES)[key(slot)] = record
        ledger.save(now)
        _log(ledger_file, {"event": "release-stale", "slot": slot, **record})
    return dict(record, slot=slot, released=True)


# --------------------------------------------------------------------------- CLI

def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--ledger")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status")
    p = sub.add_parser("claim")
    p.add_argument("slot", choices=SLOTS)
    p.add_argument("--owner", required=True)
    p.add_argument("--reason", required=True)
    p.add_argument("--session")
    group = p.add_mutually_exclusive_group(required=True)
    group.add_argument("--pid", type=int, help="the process whose life bounds the claim")
    group.add_argument("--session-claim", action="store_true", help="no process; released only by owner or quiet rule")
    p = sub.add_parser("touch")
    p.add_argument("--owner", required=True)
    for name in ("check", "release"):
        p = sub.add_parser(name)
        p.add_argument("slot", choices=SLOTS)
        p.add_argument("--owner", required=True)
    p = sub.add_parser("release-stale")
    p.add_argument("slot", choices=SLOTS)
    p.add_argument("--by", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "status":
            result = status(args.ledger)
        elif args.command == "claim":
            result = claim(args.slot, args.owner, args.reason, pid=args.pid, session=args.session, path=args.ledger)
        elif args.command == "touch":
            result = touch(args.owner, args.ledger)
        elif args.command == "check":
            result = check(args.slot, args.owner, args.ledger)
        elif args.command == "release":
            result = release(args.slot, args.owner, args.ledger)
        else:
            result = release_stale(args.slot, args.by, args.ledger)
    except Refused as error:
        print(json.dumps({"refused": str(error)}, ensure_ascii=False))
        return 3
    except (OSError, ValueError, Unknown, KeyError, TypeError, AttributeError) as error:
        print(json.dumps({"error": "{}: {}".format(type(error).__name__, error)}, ensure_ascii=False))
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
