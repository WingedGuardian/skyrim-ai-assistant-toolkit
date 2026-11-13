#!/usr/bin/env python3
"""Save vault: every version of every save, kept forever, without reading a single command.

Replaces the command-text save guard (user ruling 2026-11-10: measured over 1,602 sessions, its 191 refusals changed
no real save). Nothing here decides whether an agent may run something. A watcher copies each save pair once it has
settled, so a later change of ANY kind (a script, a tool, a typo, the game) leaves the earlier version recoverable.

How a version is taken without disturbing the game (MEASURED 2026-11-10, scratch files): any handle on a live save's
NAME -- a reader, and even the attribute handle os.stat() opens -- makes the game's "write X.ess.tmp, replace X.ess"
fail, with MoveFileEx and with ReplaceFileW alike. So the vault never opens a live save by its name:
  * folders are listed with os.scandir, whose sizes and times come from the directory listing (no open on Windows);
  * a settled member gets a second name in the vault's staging folder (a hard link, ~1 ms), and only THAT name is
    read; the link is removed before the capture returns. A handle on the link does not block the game's replace,
    and the link keeps the old bytes after it (measured);
  * a locked save cannot be hard-linked, and the game cannot replace it either, so it is copied directly;
  * a save on another volume than the vault is copied directly only while the game is NOT running; otherwise it waits.

Storage is modding_harness.preservation.SaveStore (content-addressed blobs, verified copies, no-clobber publication,
immutable transactions). On top of it: an append-only event log (events/YYYY-MM.jsonl: each version, each pair that
vanished, each legacy import, each restore), the index of what is kept (status/kept.json, changed only under the
capture lock, rebuildable from the events) and a heartbeat (status/heartbeat.json) that says whether the watcher runs.

    save_vault.py sweep [--now] [--pair NAME]   one pass
    save_vault.py watch                          the long-running watcher (logon task: install-task)
    save_vault.py status [NAME] [--vanished]     liveness, counts, one save's history
    save_vault.py restore NAME [--at T | --id ID] [--to DIR | --as STEM | --replace] [--apply]   dry run by default
    save_vault.py verify [--sample N]            re-hash stored versions (all by default)
    save_vault.py import-legacy                  copy the 3.x guard's backups in (never changes them)
    save_vault.py install-task | uninstall-task  Windows logon task for the watcher

The vault removes nothing it stored. Its staging links are scratch it made itself and are removed in `finally` (and,
after a crash, at the next watcher start).
"""
from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import os
import random
import shutil
import subprocess
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path
from xml.sax.saxutils import escape

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from modding_harness import durable  # noqa: E402
from modding_harness.locking import exclusive  # noqa: E402
from modding_harness.preservation import PreservationFailed, SaveStore  # noqa: E402
from modding_harness.profiles import canonical  # noqa: E402

EXTS = (".ess", ".skse")
POLL_S = 15            # seconds between watcher passes
SETTLE_S = 20          # a member must be this old, and its signature unchanged this long, before it is copied
FORCE_MIN_AGE_S = 2    # even --now waits this long after the newest write (a pair mid-replace is not one version)
PAIR_GRACE_S = 60      # a lone .ess (or .skse) waits this long for its partner before it is kept alone
TMP_LIVE_S = 300       # a .tmp sibling younger than this means the game is writing the pair; older is a leftover
VANISH_MISSES = 2      # passes a pair must be absent (its folder readable) before it is recorded vanished
FAIL_REPORT = 3        # passes in a row a pair may fail to capture before the heartbeat says not ok
BUSY_TRIES = 5         # captures tried per pass while the game runs (one success ends the pass)
STALE_S = 120          # a heartbeat older than this means the watcher is not running
BEAT_S = 30            # a pass longer than this refreshes the heartbeat as it goes
FOLDER_REFRESH_S = 300
MARKED_DEPTH = 4
BUSY_PROCESSES = ("skyrimvr.exe", "skyrimse.exe", "skyrim.exe", "vrserver.exe", "vrcompositor.exe")
CAPTURE_RETRIES = 3
DEFAULT_RESERVE_GB = 2    # the store's own default (preservation.DEFAULT_RESERVE); SAVE_VAULT_RESERVE_GB overrides


class Deferred(Exception):
    """Not now (another volume while the game runs); the pair stays pending, it is not a failure."""


def default_root(env=None):
    env = os.environ if env is None else env
    if env.get("SAVE_VAULT_DIR"):
        return Path(env["SAVE_VAULT_DIR"])
    if os.name == "nt":
        base = env.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    else:
        base = env.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "modding-agent-harness" / "save-vault" / "skyrim"


def now_text(ts=None):
    return datetime.fromtimestamp(time.time() if ts is None else ts).astimezone().isoformat(timespec="seconds")


def parse_time(text):
    text = text.strip()
    if text[-1:] in ("Z", "z"):                    # Python 3.10's fromisoformat rejects a trailing Z
        text = text[:-1] + "+00:00"
    value = datetime.fromisoformat(text)
    if value.tzinfo is None:
        value = value.astimezone()
    return value.timestamp()


def _event_time(event):
    """An event's time for sorting and --at; one unreadable time (a hand-edited or foreign legacy record) must not
    make status or restore fail for that save, so it sorts first."""
    try:
        return parse_time(event["time"])
    except (KeyError, TypeError, ValueError, OverflowError, OSError):
        return 0.0


def busy():
    """True while the game or SteamVR runs, AND when that cannot be told: unknown counts as running."""
    command = ["tasklist", "/NH", "/FO", "CSV"] if os.name == "nt" else ["ps", "-eo", "comm"]
    try:
        out = subprocess.run(command, capture_output=True, text=True, errors="replace", timeout=10,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout.lower()
    except (OSError, subprocess.SubprocessError):
        return True
    if not out.strip():
        return True
    if os.name == "nt":
        return any('"' + name + '"' in out for name in BUSY_PROCESSES)
    names = {line.strip() for line in out.splitlines()}
    return any(name[:-4] in names or name in names for name in BUSY_PROCESSES)


def lower_priority():
    """Below-normal CPU and background I/O for the watcher (the game must never wait on it)."""
    try:
        if os.name == "nt":
            import ctypes
            handle = ctypes.windll.kernel32.GetCurrentProcess()
            ctypes.windll.kernel32.SetPriorityClass(handle, 0x00004000)        # BELOW_NORMAL_PRIORITY_CLASS
            ctypes.windll.kernel32.SetPriorityClass(handle, 0x00100000)        # PROCESS_MODE_BACKGROUND_BEGIN
        else:
            os.nice(10)
    except (OSError, AttributeError, ValueError):
        pass


def _atomic_text(path, text):
    part = Path(path).with_name("." + Path(path).name + "-" + uuid.uuid4().hex)
    part.write_text(text, encoding="utf-8")
    os.replace(part, path)


# --------------------------------------------------------------------------- discovery

def discover(project, extra=(), env=None):
    """Every existing folder the game may read saves from, plus folders marked with .protect-saves under project.

    SAVE_VAULT_FOLDERS (os.pathsep list) replaces discovery outright: tests, and setups the discovery cannot see."""
    env = dict(os.environ if env is None else env)
    if env.get("SAVE_VAULT_FOLDERS"):
        found = [Path(p) for p in env["SAVE_VAULT_FOLDERS"].split(os.pathsep) if p]
    else:
        from skyguard import paths, save_sentinel
        cfg = paths.read_install(str(project), env)
        roots = paths.roots(str(project), str(project), cfg)
        found = list(save_sentinel.save_folders(roots, env))
        # SKYRIM_SAVE_DIRS (env file or environment, os.pathsep-separated): save folders discovery cannot see
        for value in paths._values(cfg, "SKYRIM_SAVE_DIRS") + [env.get("SKYRIM_SAVE_DIRS", "")]:
            found += [Path(v) for v in value.split(os.pathsep) if v]
        found += [Path(p) for p in save_sentinel.marked_folders(project, MARKED_DEPTH)]
    found += [Path(p) for p in extra]
    unique = {}
    for folder in found:
        try:
            if folder.is_dir():
                unique.setdefault(os.path.normcase(os.path.abspath(folder)), folder.absolute())
        except OSError:
            continue
    return list(unique.values())


def config_root(env=None):
    """The INI vault sits beside the save vault: <save vault>-config."""
    root = default_root(env)
    return root.with_name(root.name + "-config")


def discover_config(project, env=None):
    """The game's config folders (Documents/My Games/<edition>, as Windows reports Documents, plus SKYRIM_CONFIG_DIR):
    the INIs there have no other copy anywhere (user ruling 2026-11-10: keep every version of them too)."""
    env = dict(os.environ if env is None else env)
    if env.get("SAVE_VAULT_CONFIG_FOLDERS"):
        found = [Path(p) for p in env["SAVE_VAULT_CONFIG_FOLDERS"].split(os.pathsep) if p]
    else:
        from skyguard import mcp_rules, paths, save_sentinel
        cfg = paths.read_install(str(project), env)
        known = [] if env.get("SKYGUARD_NO_KNOWN_FOLDERS") == "1" else save_sentinel._known_documents()
        docs = list(dict.fromkeys(mcp_rules._documents(env) + known))
        found = [Path(d) / "My Games" / g for d in docs for g in paths.GAME_FOLDERS]
        for value in paths._values(cfg, "SKYRIM_CONFIG_DIR") + [env.get("SKYRIM_CONFIG_DIR", "")]:
            found += [Path(v) for v in value.split(os.pathsep) if v]
    unique = {}
    for folder in found:
        try:
            if folder.is_dir():
                unique.setdefault(os.path.normcase(os.path.abspath(folder)), folder.absolute())
        except OSError:
            continue
    return list(unique.values())


INI_EXTS = (".ini",)


# --------------------------------------------------------------------------- the vault

class Vault:
    def __init__(self, root, folders, is_busy=busy, clock=time.time, reserve_gb=None, exts=EXTS):
        """exts: the member suffixes of one kept item, in order (saves: .ess + .skse as a pair; INIs: .ini alone)."""
        self.exts = tuple(exts)
        self.root = Path(root).absolute()
        self.folders = [Path(f).absolute() for f in folders]
        self.is_busy = is_busy
        self.clock = clock
        for name in ("events", "status", "staging-links"):
            (self.root / name).mkdir(parents=True, exist_ok=True)
        if reserve_gb is None:
            reserve_gb = float(os.environ.get("SAVE_VAULT_RESERVE_GB", DEFAULT_RESERVE_GB))
        self.store = SaveStore(self.root, destination=self.root / "store", save_roots=self.folders,
                               reserve=int(reserve_gb * 1024 ** 3))
        self.state_path = self.root / "status" / "state.json"      # soft: settle clocks, misses, failures
        self.kept_path = self.root / "status" / "kept.json"        # authority: what is kept (under capture.lock)
        self.state = self._load_json(self.state_path, {"v": 1, "seen": {}, "misses": {}, "fails": {}})
        self._busy = None

    # ---- files
    @staticmethod
    def _load_json(path, default):
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
            if isinstance(data, dict) and data.get("v") == 1:
                for k, v in default.items():
                    data.setdefault(k, v)
                return data
        except (OSError, ValueError):
            pass
        return json.loads(json.dumps(default))

    def _capture_lock(self, wait=60):
        return exclusive(self.root / "status" / "capture.lock", wait)

    def kept(self):
        """{key: {sig, id, folder, stem, gone}} plus the legacy ids done; rebuilt from the events when missing."""
        data = self._load_json(self.kept_path, {"v": 1, "pairs": None, "legacy": None})
        if data["pairs"] is None or data["legacy"] is None:
            data = {"v": 1, "pairs": {}, "legacy": []}
            for event in self.events():
                key = event.get("key")
                if event.get("event") == "version" and key:
                    data["pairs"][key] = {"sig": event["sig"], "id": event["id"], "folder": event["folder"],
                                          "stem": event["stem"], "gone": False}
                elif event.get("event") == "vanished" and key in data["pairs"]:
                    data["pairs"][key]["gone"] = True
                elif event.get("event") == "import" and event.get("legacy_id"):
                    data["legacy"].append(event["legacy_id"])
        return data

    def _save_kept(self, data):
        _atomic_text(self.kept_path, json.dumps(data, sort_keys=True))

    def _save_state(self):
        _atomic_text(self.state_path, json.dumps(self.state, sort_keys=True))

    def record(self, event):
        """Append one event under the events lock (two processes never interleave a line), fsynced before return."""
        event = dict(event)
        event.setdefault("v", 1)
        event.setdefault("id", datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6])
        event.setdefault("time", now_text(self.clock()))
        path = self.root / "events" / (datetime.fromtimestamp(self.clock()).strftime("%Y-%m") + ".jsonl")
        with exclusive(self.root / "status" / "events.lock", 30):
            with open(path, "ab") as stream:
                stream.write((json.dumps(event, sort_keys=True) + "\n").encode("utf-8"))
                stream.flush()
                os.fsync(stream.fileno())
        return event

    def events(self):
        for path in sorted((self.root / "events").glob("*.jsonl")):
            with open(path, encoding="utf-8") as stream:
                for line in stream:
                    line = line.strip()
                    if line:
                        try:
                            yield json.loads(line)
                        except ValueError:
                            continue

    def heartbeat(self, ok, pending, error="", notes=()):
        try:
            free = shutil.disk_usage(self.root).free
        except OSError:
            free = -1
        watched = len(self.folders)
        if watched == 0:
            ok, error = False, (error + "; " if error else "") + "no folder is watched"
        data = {"pid": os.getpid(), "last_sweep": self.clock(), "time": now_text(self.clock()), "ok": ok,
                "pending": pending, "error": error, "notes": list(notes), "watched": watched,
                "folders": [str(f) for f in self.folders], "free_bytes": free, "reserve_bytes": self.store.reserve}
        _atomic_text(self.root / "status" / "heartbeat.json", json.dumps(data, sort_keys=True))

    def game_running(self):
        if self._busy is None:
            self._busy = bool(self.is_busy())
        return self._busy

    # ---- scanning (directory listings only: never a handle on a live save's name)
    @staticmethod
    def key_of(folder, stem):
        return canonical(folder) + "|" + stem.casefold()

    def scan(self, folders=None):
        """({key: pair}, readable folders, stale .tmp names) from os.scandir alone. Unreadable folders: self.unreadable."""
        pairs, readable, stale = {}, set(), []
        self.unreadable = []
        now = self.clock()
        for folder in (self.folders if folders is None else folders):
            try:
                with os.scandir(folder) as it:
                    entries = list(it)
            except OSError:
                self.unreadable.append(str(folder))
                continue
            ckey = canonical(folder)                               # once per folder (a directory handle only)
            readable.add(ckey)
            tmp_age = {}
            for entry in entries:
                low = entry.name.lower()
                if low.endswith(".tmp"):
                    try:
                        tmp_age[low[:-4]] = now - entry.stat(follow_symlinks=False).st_mtime
                    except OSError:
                        tmp_age[low[:-4]] = 0.0
            for entry in entries:
                low = entry.name.lower()
                ext = next((e for e in self.exts if low.endswith(e)), None)  # exact suffix: X.ess.tmp never matches
                if ext is None:
                    continue
                try:
                    if not entry.is_file(follow_symlinks=False):
                        continue
                    st = entry.stat(follow_symlinks=False)
                except OSError:
                    continue
                stem = entry.name[:-len(ext)]
                key = ckey + "|" + stem.casefold()
                pair = pairs.setdefault(key, {"folder": str(folder), "ckey": ckey, "stem": stem, "members": {},
                                              "tmp": False})
                pair["members"][ext] = {"path": os.path.join(str(folder), entry.name), "size": st.st_size,
                                        "mtime_ns": st.st_mtime_ns, "mtime": st.st_mtime}
            for key, pair in pairs.items():
                if pair["ckey"] != ckey:
                    continue
                for e in self.exts:
                    age = tmp_age.get((pair["stem"] + e).lower())
                    if age is None:
                        continue
                    if age < TMP_LIVE_S:
                        pair["tmp"] = True                       # the game is writing this pair now
                    else:
                        stale.append(os.path.join(str(folder), pair["stem"] + e + ".tmp"))
        for pair in pairs.values():
            pair["sig"] = [[pair["members"][e]["size"], pair["members"][e]["mtime_ns"]] if e in pair["members"] else None
                           for e in self.exts]
            pair["newest"] = max(m["mtime"] for m in pair["members"].values())
        return pairs, readable, stale

    def due(self, key, pair, now, kept, force=False, ignore_tmp=False):
        """Settled and not yet kept. force (only while the game is closed) shortens the waits, never skips them."""
        seen = self.state["seen"].get(key)
        if seen is None:
            # first sight: no history, so the age of the files is the evidence
            self.state["seen"][key] = [pair["sig"], now - SETTLE_S]
        elif seen[0] != pair["sig"]:
            self.state["seen"][key] = [pair["sig"], now]          # changed since the last look: restart the clock
        if pair["tmp"] and not ignore_tmp:
            return False
        was = kept["pairs"].get(key)
        if was and was.get("sig") == pair["sig"] and not was.get("gone"):
            return False
        if force:
            return now - pair["newest"] >= FORCE_MIN_AGE_S
        if now - self.state["seen"][key][1] < SETTLE_S:
            return False
        if now - pair["newest"] < SETTLE_S:
            return False
        if len(pair["members"]) < len(self.exts) and now - pair["newest"] < PAIR_GRACE_S:
            return False                                           # wait for the partner
        return True

    # ---- capture
    def _same_volume(self, folder):
        """A directory stat on each side (measured harmless to the game's replace); never the save itself."""
        try:
            return os.stat(folder).st_dev == os.stat(self.root).st_dev
        except OSError:
            return False

    def _writing_now(self, pair):
        """A .tmp sibling younger than TMP_LIVE_S: the game started saving this pair. Read from a directory listing
        (os.scandir: on Windows the times come from the listing itself), so no handle is opened on the .tmp, the
        source of the game's own rename, at the moment the game is about to rename it."""
        wanted = {(pair["stem"] + ext + ".tmp").casefold() for ext in self.exts}
        try:
            with os.scandir(pair["folder"]) as it:
                for entry in it:
                    if entry.name.casefold() in wanted:
                        try:
                            if self.clock() - entry.stat(follow_symlinks=False).st_mtime < TMP_LIVE_S:
                                return True
                        except OSError:
                            return True                    # cannot tell: treat it as being written
        except OSError:
            return True
        return False

    def capture(self, key, pair, ignore_tmp=False):
        """Keep one settled version of a pair; its event, or None if the files moved on (or it is already kept).

        The only open of a live save by its name is the os.link per member (unavoidable: a file is opened to be
        named again). Right before it, a fresh look at the .tmp names (never the save) skips a pair the game has
        started to save; the game writes that .tmp for many milliseconds before its replace, the link takes
        microseconds. The store is handed the LINK as the member's original, so its own path resolution touches
        only the link; the live paths are recorded in the event, and restore() writes from there."""
        with self._capture_lock():
            kept = self.kept()
            was = kept["pairs"].get(key)
            if was and was.get("sig") == pair["sig"] and not was.get("gone"):
                return None                                        # another process kept it meanwhile
            same_volume = self._same_volume(pair["folder"])
            if not ignore_tmp and self._writing_now(pair):
                return None
            links, members, held = [], [], []
            try:
                # Name every member first (microseconds apart), so the pair cannot straddle one of the game's saves;
                # check and hash only afterwards.
                for ext in self.exts:
                    member = pair["members"].get(ext)
                    if member is None:
                        continue
                    # uuid + extension only: Skyrim save names run past 150 characters, and the vault's own path
                    # plus the whole name would cross MAX_PATH where long paths are not enabled.
                    link = self.root / "staging-links" / (uuid.uuid4().hex + ext)
                    links.append(link)
                    try:
                        if not same_volume:
                            raise OSError("another volume")
                        os.link(member["path"], link)
                        held.append((ext, member, link, False))
                    except OSError as exc:
                        if same_volume and self.is_busy():
                            # A same-volume link that fails while the game may run is not proof of a lock (a long
                            # path, a file system without links): reading the live save by name now could block the
                            # game's replace, so it waits for the game to close.
                            raise Deferred("could not link it ({}); waits until the game is closed".format(exc))
                        held.append((ext, member, link, True))
                for ext, member, link, copied in held:
                    if copied:
                        # Locked (the game cannot replace it either) or on another volume (only with the game
                        # closed, asked again right here): one whole-file read of the live save, whose own stat must
                        # still be the settled one afterwards.
                        if not same_volume and self.is_busy():
                            raise Deferred("on another volume than the vault; waits until the game is closed")
                        durable.copy_verified(member["path"], link)
                        st = os.stat(member["path"])
                    else:
                        st = os.stat(link)
                    if [st.st_size, st.st_mtime_ns] != [member["size"], member["mtime_ns"]]:
                        return None                                        # a newer write: the next pass takes it
                for ext in self.exts:
                    entry = next(((m, l) for e, m, l, _ in held if e == ext), None)
                    if entry is None:
                        absent = str(self.root / "staging-links" / (uuid.uuid4().hex + "-absent" + ext))
                        members.append({"original": absent, "missing": True, "source": absent})
                        continue
                    member, link = entry
                    members.append({"original": str(link), "source": str(link), "missing": False,
                                    "sha256": durable.digest(link), "size": member["size"]})
                # The links are the version from here on. If the game replaces the live save meanwhile, the link
                # keeps the old bytes, but on POSIX its change time moves (one fewer name) and the store's recheck
                # fails: retry on the SAME links, never re-link (the live file is already the next version).
                for attempt in range(CAPTURE_RETRIES):
                    try:
                        manifest = self.store.import_records([members], {"vault": "version", "pair": pair["stem"]})
                        break
                    except PreservationFailed:
                        if attempt == CAPTURE_RETRIES - 1:
                            raise
            finally:
                for link in links:
                    try:
                        link.unlink()
                    except FileNotFoundError:
                        pass
            recs = {Path(r["path"]).suffix.lower(): r for r in manifest["groups"][0]["members"]}
            info = {}
            for ext in self.exts:
                rec = recs.get(ext)
                info[ext[1:]] = None if rec is None or rec["missing"] else {
                    "original": pair["members"][ext]["path"], "sha256": rec["sha256"], "size": rec["size"],
                    "mtime_ns": pair["members"][ext]["mtime_ns"]}
            missing = [e for e in self.exts if info[e[1:]] is None]
            pair_sha = hashlib.sha256("|".join((info[e[1:]] or {}).get("sha256", "-") for e in self.exts).encode()).hexdigest()
            note = ""
            if missing:
                note = "co-save missing" if missing == [".skse"] else ", ".join(m[1:] for m in missing) + " missing"
            event = self.record(dict({"event": "version", "key": key, "folder": pair["folder"], "stem": pair["stem"],
                                      "sig": pair["sig"], "pair_sha256": pair_sha, "transaction": manifest["id"],
                                      "prev": (was or {}).get("id"), "game_running": self.game_running(), "note": note},
                                     **{e[1:]: info[e[1:]] for e in self.exts}))
            kept["pairs"][key] = {"sig": pair["sig"], "id": event["id"], "folder": pair["folder"],
                                  "stem": pair["stem"], "gone": False}
            self._save_kept(kept)
            return event

    def clean_staging(self):
        """Links left by a process killed mid-capture (each is a second name for a live save). Watcher start only."""
        with self._capture_lock():
            for link in (self.root / "staging-links").iterdir():
                try:
                    link.unlink()
                except OSError:
                    pass

    # ---- one pass
    def sweep(self, force=False, only=None, limit=None, beat=True):
        """One pass. beat: write the heartbeat. Only the watcher does; a one-off CLI sweep must not make a dead
        watcher look alive. A long pass (the first one after a seed, the game closed) refreshes it every BEAT_S."""
        self._busy = None
        now = self.clock()
        beaten = now
        if force and self.game_running():
            force = False                                  # never shorten the waits while the game may be writing
        pairs, readable, stale = self.scan()
        kept = self.kept()
        queue = []
        for key, pair in pairs.items():
            if only and pair["stem"].casefold() != only.casefold():
                continue
            if self.due(key, pair, now, kept, force):
                queue.append((pair["newest"], key, pair))
        queue.sort(reverse=True)                          # newest first: the slot the game just wrote
        if limit is None:
            limit = 1 if self.game_running() else len(queue)
        tries = BUSY_TRIES if self.game_running() else len(queue)
        captured, errors, deferred, tried = [], [], 0, 0
        volume = {}
        for _, key, pair in queue:
            if len(captured) >= limit or tried >= tries:
                break
            if pair["folder"] not in volume:
                volume[pair["folder"]] = self._same_volume(pair["folder"])
            if not volume[pair["folder"]] and self.game_running():
                deferred += 1                                  # one process check per pass, not one per pair
                continue
            try:
                event = self.capture(key, pair)
            except Deferred:
                deferred += 1                                  # not a try: it must not starve same-volume pairs
                continue
            except (OSError, PreservationFailed, TimeoutError) as exc:
                errors.append("{}: {}".format(pair["stem"], exc))
                event = None
            tried += 1
            if beat and self.clock() - beaten >= BEAT_S:
                beaten = self.clock()
                self.heartbeat(True, len(queue) - len(captured), "", ["a long pass is in progress"])
            if event:
                captured.append(event)
                self.state["fails"].pop(key, None)
            else:
                kept_now = self.kept()["pairs"].get(key)
                if not (kept_now and kept_now.get("sig") == pair["sig"]):
                    self.state["fails"][key] = self.state["fails"].get(key, 0) + 1
        vanished = self._vanished(pairs, readable) if only is None else []
        kept_after = self.kept()["pairs"]
        for k in list(self.state["fails"]):
            # a pair that is gone, or kept as it is now (by any process), is no longer failing
            if k not in pairs or (kept_after.get(k) or {}).get("sig") == pairs[k]["sig"]:
                self.state["fails"].pop(k, None)
        self._save_state()
        pending = len(queue) - len(captured)
        failing = sorted(pairs[k]["stem"] if k in pairs else k.split("|")[-1]
                         for k, n in self.state["fails"].items() if n >= FAIL_REPORT)
        notes = ["stale tmp (ignored): " + p for p in stale[:10]]
        if deferred:
            notes.append("{} item(s) wait for the game to close (another volume, or a link that failed)".format(deferred))
        problem = "; ".join(errors + ["folder unreadable: " + f for f in self.unreadable])
        if failing:
            problem = (problem + "; " if problem else "") + "{} item(s) failed {}+ passes: {}".format(
                len(failing), FAIL_REPORT, ", ".join(failing[:10]))
        if beat:
            self.heartbeat(not problem, pending, problem[:2000], notes)
        return {"captured": captured, "vanished": vanished, "pending": pending, "errors": errors,
                "deferred": deferred, "failing": failing, "stale_tmp": stale}

    def _vanished(self, pairs, readable):
        found = []
        with self._capture_lock():
            kept = self.kept()
            changed = False
            for key, was in kept["pairs"].items():
                misses = self.state["misses"]
                if was.get("gone"):
                    if key in pairs:
                        was["gone"] = False                   # back again; a changed signature gets a new version
                        misses.pop(key, None)
                        changed = True
                    continue
                if key in pairs:
                    misses.pop(key, None)
                    continue
                if canonical(was["folder"]) not in readable:
                    continue                                  # an unreadable or missing folder proves nothing
                misses[key] = misses.get(key, 0) + 1
                if misses[key] >= VANISH_MISSES:
                    was["gone"] = True
                    misses.pop(key, None)
                    changed = True
                    found.append(self.record({"event": "vanished", "key": key, "folder": was["folder"],
                                              "stem": was["stem"], "last": was["id"], "captured": True}))
            if changed:
                self._save_kept(kept)
        return found

    # ---- queries
    def versions(self, name, folder=None):
        """Every stored version of a save, oldest first: watcher versions and imported legacy backups alike."""
        stem = name
        for ext in self.exts:
            if stem.lower().endswith(ext):
                stem = stem[:-len(ext)]
        want = canonical(folder) if folder else None
        found = []
        for e in self.events():
            if e.get("event") not in ("version", "import") or e.get("stem", "").casefold() != stem.casefold():
                continue
            if want is not None and canonical(e["folder"]) != want:
                continue
            if e["event"] == "import":
                e = dict(e, legacy=True, time=e.get("legacy_time") or e["time"])
            found.append(e)
        found.sort(key=_event_time)
        return found

    def choose(self, name, at=None, event_id=None, folder=None):
        versions = self.versions(name, folder)
        if event_id:
            versions = [e for e in versions if e["id"] == event_id]
        elif at is not None:
            versions = [e for e in versions if _event_time(e) <= at]
        if not versions:
            raise LookupError("no stored version of {} matches".format(name))
        folders = {canonical(e["folder"]) for e in versions}
        if len(folders) > 1 and not event_id:
            raise LookupError("{} exists in {} folders; pass --in-folder or --id".format(name, len(folders)))
        return versions[-1]

    # ---- restore
    @staticmethod
    def _listed(folder):
        """Lower-cased names in a folder, from one directory listing (no handle on any save)."""
        try:
            with os.scandir(folder) as it:
                return {e.name.lower() for e in it}
        except OSError:
            return set()

    def restore(self, name, at=None, event_id=None, folder=None, to=None, as_stem=None, replace=False, apply=False):
        """Dry run unless apply. Writes come from the verified stored copies to the live paths the event recorded.

        A present pair needs replace (which keeps the current one as a vault version first) or as_stem / to. Anything
        written into the save folder waits for the game to be closed; that check comes before any look at the saves."""
        event = self.choose(name, at, event_id, folder)
        live = [Path(event["folder"]) / (event["stem"] + e) for e in self.exts]
        if to:
            if apply and self.is_busy() and canonical(to) in {canonical(f) for f in self.folders + [Path(event["folder"])]}:
                raise PreservationFailed("--to names a save folder: it waits until the game is closed")
            targets = [Path(to) / (event["stem"] + e) for e in self.exts]
            clash = [str(x) for x in targets if x.name.lower() in self._listed(to)]
            if clash:
                raise FileExistsError("already exists: " + ", ".join(clash))
            plan = {"mode": "copy-to", "targets": [str(x) for x in targets]}
        else:
            if apply and self.is_busy():
                raise PreservationFailed("the game (or SteamVR) is running, or that cannot be told: restore into the "
                                         "save folder refused; use --to, or close the game")
            names = self._listed(event["folder"])
            if as_stem:
                if any(c in as_stem for c in "/\\:") or as_stem in (".", ".."):
                    raise ValueError("--as takes a save NAME, not a path")
                targets = [Path(event["folder"]) / (as_stem + e) for e in self.exts]
                clash = [str(x) for x in targets if x.name.lower() in names]
                if clash:
                    raise FileExistsError("already exists: " + ", ".join(clash))
                plan = {"mode": "as", "targets": [str(x) for x in targets]}
            else:
                present = [x for x in live if x.name.lower() in names]
                if present:
                    if not replace:
                        raise FileExistsError("{} is present; pass --replace (keeps the current pair first), --as or "
                                              "--to".format(", ".join(map(str, present))))
                    lacking = [str(x) for x, e in zip(live, self.exts) if x in present and event.get(e[1:]) is None]
                    if lacking:
                        raise FileExistsError("that version has no {}; replacing would leave the present one "
                                              "orphaned: {}. Use --as or --to".format(
                                                  "co-save" if lacking[0].endswith(".skse") else ".ess", ", ".join(lacking)))
                    plan = {"mode": "replace", "targets": [str(x) for x in live]}
                else:
                    plan = {"mode": "put-back", "targets": [str(x) for x in live]}
        plan.update(version=event["id"], time=event["time"], transaction=event["transaction"], apply=apply,
                    legacy=bool(event.get("legacy")))
        if not apply:
            return plan
        if plan["mode"] == "replace":
            # The current pair is kept before it is replaced: scanned in the EVENT's folder (this run may watch other
            # folders), and the replace refused unless that exact pair is now kept.
            key = self.key_of(event["folder"], event["stem"])
            pairs, _, _ = self.scan(folders=[Path(event["folder"])])
            if key not in pairs:
                raise PreservationFailed("the present {} could not be read to keep it first; nothing was replaced"
                                         .format(event["stem"]))
            if self.due(key, pairs[key], self.clock(), self.kept(), force=True, ignore_tmp=True):
                self.capture(key, pairs[key], ignore_tmp=True)    # the current pair is kept before it is replaced
                self._save_state()
            now_kept = self.kept()["pairs"].get(key) or {}
            if now_kept.get("sig") != pairs[key]["sig"]:
                raise PreservationFailed("the current pair changed just now and is not kept yet; nothing was "
                                         "replaced. Retry in a few seconds.")
            # What the set-aside members must hash to before they may go: the kept version's own bytes. A size and
            # time match is not proof (a same-size rewrite that restores the old time, or a write after this check).
            held = [e for e in self.versions(event["stem"], folder=event["folder"]) if e["id"] == now_kept.get("id")]
            if not held:
                raise PreservationFailed("the current pair's kept version could not be found; nothing was replaced")
            kept_sha = {str(t): (held[0].get(e[1:]) or {}).get("sha256") for t, e in zip(plan["targets"], self.exts)}
        else:
            kept_sha = {}
        if plan["mode"] == "copy-to":
            Path(to).mkdir(parents=True, exist_ok=True)
        plan["result"] = self._write(event, plan["targets"], overwrite=plan["mode"] == "replace", kept_sha=kept_sha)
        self.record({"event": "restored", "key": self.key_of(event["folder"], event["stem"]), "version": event["id"],
                     "mode": plan["mode"], "targets": plan["targets"]})
        return plan

    def _write(self, event, targets, overwrite=False, kept_sha=None):
        """Every stored member is staged beside its target and hash-checked FIRST; only then are they published
        (no-clobber unless overwrite), so a bad copy never leaves half a pair. A publish failing midway is recorded.

        overwrite: the current members are set aside first (a restore-begin event names them before anything moves),
        and a set-aside member is removed only when its bytes hash to kept_sha[target], the kept version's own;
        otherwise it stays beside the save and a restore-kept-aside event names it."""
        kept_sha = kept_sha or {}
        staged, published, kept_aside = [], [], []
        try:
            for ext, target in zip(self.exts, targets):
                member = event.get(ext[1:])
                if member is None:
                    continue
                part = Path(target).with_name(".vault-" + uuid.uuid4().hex)
                staged.append((part, target))
                info = durable.copy_verified(self.store.root / "blobs" / member["sha256"], part)
                if info["sha256"] != member["sha256"]:
                    raise PreservationFailed("stored version failed verification")
            moved = []                                     # replace: the current members, set aside first
            asides = {str(t): Path(t).with_name(".vault-old-" + uuid.uuid4().hex) for _, t in staged
                      if overwrite and os.path.lexists(t)}
            if asides:
                self.record({"event": "restore-begin", "version": event["id"],
                             "set_aside": {t: str(a) for t, a in asides.items()}})
            try:
                if overwrite:
                    for part, target in staged:
                        if str(target) in asides:
                            aside = asides[str(target)]
                            os.replace(target, aside)      # a locked save refuses here, before anything changed
                            moved.append((aside, target))
                for part, target in staged:
                    durable.publish_new(part, target)          # no-clobber: the place is free (set aside or absent)
                    published.append(str(target))
            except OSError as exc:
                # all or nothing: drop what this restore published, put back what it set aside
                rolled = True
                for target in published:
                    try:
                        os.unlink(target)                      # a file this restore itself just wrote
                    except OSError:
                        rolled = False
                for aside, target in moved:
                    try:
                        os.replace(aside, target)
                    except OSError:
                        rolled = False
                if not rolled:
                    self.record({"event": "restore-partial", "version": event["id"], "published": published,
                                 "set_aside": [str(a) for a, _ in moved], "error": str(exc)})
                    raise PreservationFailed("restore failed and could not be fully undone ({}); the event log names "
                                             "every file involved".format(exc)) from exc
                raise
            for aside, target in moved:
                try:
                    want = kept_sha.get(str(target))
                    same = want is not None and durable.digest(aside) == want
                except OSError:
                    same = False
                if not same:
                    self.record({"event": "restore-kept-aside", "version": event["id"], "target": str(target),
                                 "aside": str(aside), "note": "its bytes are not the kept version's; left in place"})
                    kept_aside.append(str(aside))
                    continue
                try:
                    os.unlink(aside)                       # byte-identical to the kept version
                except OSError:
                    kept_aside.append(str(aside))
        finally:
            for part, _ in staged:
                part.unlink(missing_ok=True)
        return {"published": published, "kept_aside": kept_aside}

    # ---- verify
    def verify(self, sample=None):
        ids = sorted({e["transaction"] for e in self.events() if e.get("transaction")})
        if sample:
            ids = random.sample(ids, min(sample, len(ids)))
        bad = []
        for tx in ids:
            try:
                self.store.load(tx)
            except (OSError, ValueError, PreservationFailed) as exc:
                bad.append((tx, str(exc)))
        return {"checked": len(ids), "bad": bad}

    # ---- legacy import
    def import_legacy(self, manifest_path):
        """Copy the 3.x guard's backups in, one transaction per record. Never changes the old store.

        Which records are done comes from kept.json (rebuilt from the import events), read and written under the
        capture lock, so a running watcher cannot make a second run import a record again."""
        manifest_path = Path(manifest_path)
        if self.is_busy():
            # each record's original names a live save, and the store resolves it (a handle on its name)
            raise PreservationFailed("import-legacy waits until the game (and SteamVR) is closed")
        if not manifest_path.exists():
            return {"imported": 0, "skipped": 0, "failed": []}
        imported, skipped, failed = 0, 0, []
        for line in manifest_path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            rid = rec.get("id")
            if not rid:
                continue
            members = []
            for index, ext in enumerate(EXTS):
                part = rec.get(ext[1:])
                originals = rec.get("originals") or []
                original = (part or {}).get("original") or (originals[index] if index < len(originals) else None)
                if not original:
                    continue
                if isinstance(part, dict) and part.get("backup") and part.get("sha256"):
                    members.append({"original": original, "source": part["backup"], "missing": False,
                                    "sha256": part["sha256"], "size": part["size"]})
                else:
                    members.append({"original": original, "missing": True,
                                    "source": str(self.root / "staging-links" / (uuid.uuid4().hex + "-absent" + ext))})
            if self.is_busy():
                raise PreservationFailed("the game (or SteamVR) started; import-legacy stopped, run it again later")
            with self._capture_lock():
                kept = self.kept()
                if rid in kept["legacy"]:
                    continue
                if not any(not m["missing"] for m in members):
                    skipped += 1
                    kept["legacy"].append(rid)
                    self._save_kept(kept)
                    continue
                try:
                    manifest = self.store.import_records([members], {"vault": "legacy-import", "legacy_id": rid})
                except (OSError, ValueError, PreservationFailed) as exc:
                    failed.append((rid, str(exc)))
                    continue
                first = members[0]["original"]
                self.record({"event": "import", "legacy_id": rid, "legacy_time": rec.get("time"),
                             "folder": str(Path(first).parent), "stem": Path(first).stem, "transaction": manifest["id"],
                             "ess": next(({"original": m["original"], "sha256": m["sha256"], "size": m["size"]}
                                          for m in members if m["original"].lower().endswith(".ess") and not m["missing"]), None),
                             "skse": next(({"original": m["original"], "sha256": m["sha256"], "size": m["size"]}
                                           for m in members if m["original"].lower().endswith(".skse") and not m["missing"]), None)})
                kept["legacy"].append(rid)
                self._save_kept(kept)
                imported += 1
        return {"imported": imported, "skipped": skipped, "failed": failed}


# --------------------------------------------------------------------------- liveness, watcher, task

TASK = "SkyrimSaveVault"
START_HINT = ("start it: python tools/save_vault.py watch (or, with the Windows logon task installed: "
              "schtasks /Run /TN " + TASK + ")")


def watcher_status(root=None, now=None):
    """(alive, one line) from the heartbeat; what session start, hook-canary and skydoctor print."""
    path = Path(root or default_root()) / "status" / "heartbeat.json"
    now = time.time() if now is None else now
    try:
        beat = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False, "save vault: NOT RUNNING (no heartbeat at {}); {}".format(path, START_HINT)
    age = now - float(beat.get("last_sweep", 0))
    if age > STALE_S:
        return False, "save vault: NOT RUNNING (last sweep {:.0f} s ago, {}); {}".format(
            age, beat.get("time"), START_HINT)
    if not beat.get("watched"):
        return False, "save vault: running but WATCHING NO SAVE FOLDER; check discovery (status --folders)"
    tail = "" if beat.get("ok") else "; PROBLEM: " + str(beat.get("error"))[:300]
    notes = beat.get("notes") or []
    if notes:
        tail += "; note: {}{}".format(notes[0][:200], " (+{} more)".format(len(notes) - 1) if len(notes) > 1 else "")
    return bool(beat.get("ok")), "save vault: running ({} folders, last sweep {:.0f} s ago, {} pending){}".format(
        beat.get("watched"), age, beat.get("pending"), tail)


def watch(vault_factory, stop=None, *more_factories):
    """One watcher for every vault (saves, and the config INIs). Each vault's own watch.lock is held, so a second
    watcher on any of them exits. A vault that fails to build or to refresh never stops the others."""
    import contextlib
    lower_priority()
    factories = (vault_factory,) + tuple(more_factories)
    vaults = []
    for f in factories:
        try:
            vaults.append(f())
        except Exception as exc:  # e.g. Documents briefly unreachable: the other vaults still run
            print("save vault: one vault could not start: {}: {}".format(type(exc).__name__, exc))
            vaults.append(None)
    if all(v is None for v in vaults):
        raise SystemExit("save vault: no vault could start")
    with contextlib.ExitStack() as locks:
        try:
            for vault in vaults:
                if vault is not None:
                    locks.enter_context(exclusive(vault.root / "status" / "watch.lock", 0))
        except TimeoutError:
            print("save vault: a watcher is already running")
            return 0
        for vault in vaults:
            if vault is not None:
                vault.clean_staging()
        refreshed = time.monotonic()
        while not (stop and stop()):
            if time.monotonic() - refreshed > FOLDER_REFRESH_S:
                refreshed = time.monotonic()
                for i, f in enumerate(factories):
                    try:
                        fresh = f()
                    except Exception:
                        continue                                   # keep the last good instance
                    if vaults[i] is None:
                        try:                                       # recovered: it gets its lock like the others
                            locks.enter_context(exclusive(fresh.root / "status" / "watch.lock", 0))
                        except TimeoutError:
                            continue                               # another watcher owns it
                        fresh.clean_staging()
                        vaults[i] = fresh
                    elif fresh.root == vaults[i].root:
                        vaults[i] = fresh
            for vault in vaults:
                if vault is None:
                    continue
                try:
                    vault.sweep()
                except Exception as exc:  # the watcher must outlive one bad pass; the heartbeat says what failed
                    try:
                        vault.heartbeat(False, -1, "{}: {}".format(type(exc).__name__, exc)[:2000])
                    except OSError:
                        pass
            time.sleep(POLL_S)
    return 0


def task_xml(python, script, args, user):
    return """<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo><Description>Keeps every version of every Skyrim save (save_vault.py watch).</Description></RegistrationInfo>
  <Triggers><LogonTrigger><Enabled>true</Enabled><UserId>{user}</UserId></LogonTrigger></Triggers>
  <Principals><Principal id="Author"><UserId>{user}</UserId><LogonType>InteractiveToken</LogonType><RunLevel>LeastPrivilege</RunLevel></Principal></Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <Priority>7</Priority>
    <RestartOnFailure><Interval>PT1M</Interval><Count>999</Count></RestartOnFailure>
    <StartWhenAvailable>true</StartWhenAvailable>
  </Settings>
  <Actions Context="Author"><Exec><Command>{py}</Command><Arguments>-I "{script}" {args}watch</Arguments></Exec></Actions>
</Task>
""".format(user=escape(user), py=escape(python), script=escape(script), args=escape(args))


def install_task(args_text):
    if os.environ.get("PYTEST_CURRENT_TEST") or os.environ.get("HARNESS_FIXTURE_ROOT"):
        raise SystemExit("install-task: refused under a test harness (it would register a real logon task)")
    if os.name != "nt":
        raise SystemExit("install-task: Windows only (on Linux run `save_vault.py watch` from a systemd --user unit)")
    py = Path(sys.executable)
    pyw = py.with_name("pythonw.exe")
    user = (os.environ.get("USERDOMAIN", "") + "\\" if os.environ.get("USERDOMAIN") else "") + getpass.getuser()
    xml = task_xml(str(pyw if pyw.exists() else py), str(Path(__file__).resolve()), args_text, user)
    path = default_root() / "status" / "task.xml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(xml, encoding="utf-16")
    r = subprocess.run(["schtasks", "/Create", "/TN", TASK, "/XML", str(path), "/F"], capture_output=True, text=True)
    print(r.stdout.strip() or r.stderr.strip())
    if r.returncode == 0:
        subprocess.run(["schtasks", "/Run", "/TN", TASK], capture_output=True, text=True)
    return r.returncode


# --------------------------------------------------------------------------- CLI

def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--vault", help="vault folder (default SAVE_VAULT_DIR or %%LOCALAPPDATA%%/modding-agent-harness/...)")
    ap.add_argument("--project", default=str(HERE.parent), help="toolkit or game root (env file, .protect-saves search)")
    ap.add_argument("--folder", action="append", default=[], help="an extra save folder to watch (repeatable)")
    ap.add_argument("--config", action="store_true", help="act on the INI vault (the config folders) instead of saves")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("sweep"); s.add_argument("--now", action="store_true"); s.add_argument("--pair")
    sub.add_parser("watch")
    st = sub.add_parser("status"); st.add_argument("name", nargs="?"); st.add_argument("--vanished", action="store_true")
    st.add_argument("--folders", action="store_true")
    r = sub.add_parser("restore")
    r.add_argument("name"); r.add_argument("--at"); r.add_argument("--id"); r.add_argument("--in-folder")
    g = r.add_mutually_exclusive_group(); g.add_argument("--to"); g.add_argument("--as", dest="as_stem")
    g.add_argument("--replace", action="store_true"); r.add_argument("--apply", action="store_true")
    v = sub.add_parser("verify"); v.add_argument("--sample", type=int)
    sub.add_parser("import-legacy")
    sub.add_parser("install-task"); sub.add_parser("uninstall-task")
    a = ap.parse_args(argv)
    save_root = Path(a.vault) if a.vault else default_root()
    ini_root = save_root.with_name(save_root.name + "-config")
    root = ini_root if a.config else save_root

    def make_saves():
        return Vault(save_root, discover(a.project, a.folder))

    def make_inis():
        return Vault(ini_root, discover_config(a.project), exts=INI_EXTS)

    make = make_inis if a.config else make_saves
    if a.cmd == "watch":
        return watch(make_saves, None, make_inis)
    if a.cmd == "install-task":
        extra = "".join('--folder "{}" '.format(f) for f in a.folder)
        return install_task('--project "{}" {}{}'.format(a.project, '--vault "{}" '.format(save_root) if a.vault else "", extra))
    if a.cmd == "uninstall-task":
        return subprocess.run(["schtasks", "/Delete", "/TN", TASK, "/F"]).returncode
    if a.cmd == "status" and a.folders:
        for f in (discover_config(a.project) if a.config else discover(a.project, a.folder)):
            print(f)
        return 0
    if a.cmd == "status" and not a.name and not a.vanished:
        alive, line = watcher_status(save_root)
        ini_alive, ini_line = watcher_status(ini_root)
        print(line)
        print(ini_line.replace("save vault:", "INI vault:", 1))
        return 0 if alive and ini_alive else 1
    vault = make()
    if a.cmd == "sweep":
        out = vault.sweep(force=a.now, only=a.pair, beat=False)
        print("captured {}  vanished {}  pending {}  deferred {}  errors {}".format(
            len(out["captured"]), len(out["vanished"]), out["pending"], out["deferred"], len(out["errors"])))
        for e in out["errors"][:20]:
            print("  error:", e)
        for p in out["stale_tmp"][:10]:
            print("  stale tmp (ignored):", p)
        return 1 if out["errors"] or out["failing"] else 0
    if a.cmd == "status":
        if a.vanished:
            for e in vault.events():
                if e.get("event") == "vanished":
                    print(e["time"], e["stem"], "(last version", e["last"] + ")")
            return 0
        for e in vault.versions(a.name):
            print(e["time"], e["id"], "legacy" if e.get("legacy") else "",
                  " ".join("{} {}".format(x[1:], (e.get(x[1:]) or {}).get("sha256", "-")[:12]) for x in vault.exts),
                  e.get("note", ""))
        return 0
    if a.cmd == "restore":
        plan = vault.restore(a.name, at=parse_time(a.at) if a.at else None, event_id=a.id, folder=a.in_folder,
                             to=a.to, as_stem=a.as_stem, replace=a.replace, apply=a.apply)
        print(json.dumps(plan, indent=1, default=str))
        if not a.apply:
            print("dry run: nothing written; add --apply")
        return 0
    if a.cmd == "verify":
        out = vault.verify(a.sample)
        print("verified {} stored versions; {} failed".format(out["checked"], len(out["bad"])))
        for tx, why in out["bad"]:
            print("  BAD", tx, why)
        return 1 if out["bad"] else 0
    if a.cmd == "import-legacy":
        out = vault.import_legacy(Path(a.project) / ".claude" / "backups" / "save-backups" / "manifest.jsonl")
        print("imported {}  skipped {} (no file)  failed {}".format(out["imported"], out["skipped"], len(out["failed"])))
        for rid, why in out["failed"][:20]:
            print("  failed", rid, why)
        return 1 if out["failed"] else 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
