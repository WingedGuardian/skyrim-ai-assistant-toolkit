#!/usr/bin/env python3
"""Save vault: every version of every save, kept forever, without reading a single command.

Replaces the command-text save guard (user ruling 2026-11-10: measured over 1,602 sessions, its 191 refusals changed
no real save). Nothing here decides whether an agent may run something. A watcher copies each save pair once it has
settled, so a later change of ANY kind (a script, a tool, a typo, the game) leaves the earlier version recoverable.

How a version is taken without disturbing the game (MEASURED 2026-11-10, scratch files): a reader holding a save open
makes the game's "write X.ess.tmp, then replace X.ess" fail, with MoveFileEx and with ReplaceFileW alike. So the vault
never holds a live save open. It gives the settled file a second name in its own staging folder (a hard link, ~1 ms,
no handle kept), reads THAT, and removes the link before returning. The game's replace then succeeds and the link
still holds the old bytes. A locked save cannot be hard-linked, and cannot be replaced either, so it is read directly;
so is a save on another volume than the vault (a short whole-file copy into staging first).

Storage is modding_harness.preservation.SaveStore (content-addressed blobs, verified copies, no-clobber publication,
immutable transactions). On top of it: an append-only event log (events/YYYY-MM.jsonl) recording each version, each
pair that vanished and each legacy import, and a heartbeat (status/heartbeat.json) that shows whether the watcher runs.

    save_vault.py sweep [--now] [--pair NAME]   one pass
    save_vault.py watch                          the long-running watcher (logon task: install-task)
    save_vault.py status [NAME] [--vanished]     liveness, counts, one save's history
    save_vault.py restore NAME [--at T | --id ID] [--to DIR | --as STEM | --replace] [--apply]   dry run by default
    save_vault.py verify [--sample N]            re-hash stored versions (all by default)
    save_vault.py import-legacy                  copy the 3.x guard's backups in (never changes them)
    save_vault.py install-task | uninstall-task  Windows logon task for the watcher

The vault removes nothing it stored. Its staging links are scratch it made itself and are removed in `finally`.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import subprocess
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path

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
PAIR_GRACE_S = 60      # a lone .ess (or .skse) waits this long for its partner before it is kept alone
VANISH_MISSES = 2      # passes a pair must be absent (its folder readable) before it is recorded vanished
STALE_S = 120          # a heartbeat older than this means the watcher is not running
FOLDER_REFRESH_S = 300
MARKED_DEPTH = 4
MARKER = ".protect-saves"
BUSY_PROCESSES = ("skyrimvr.exe", "skyrimse.exe", "skyrim.exe", "vrserver.exe", "vrcompositor.exe")
CAPTURE_RETRIES = 3


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
    value = datetime.fromisoformat(text)
    if value.tzinfo is None:
        value = value.astimezone()
    return value.timestamp()


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


# --------------------------------------------------------------------------- the vault

class Vault:
    def __init__(self, root, folders, is_busy=busy, clock=time.time):
        self.root = Path(root).absolute()
        self.folders = [Path(f).absolute() for f in folders]
        self.is_busy = is_busy
        self.clock = clock
        for name in ("events", "status", "staging-links"):
            (self.root / name).mkdir(parents=True, exist_ok=True)
        self.store = SaveStore(self.root, destination=self.root / "store", save_roots=self.folders)
        self.state_path = self.root / "status" / "state.json"
        self.state = self._load_state()

    # ---- state, events, heartbeat
    def _load_state(self):
        try:
            state = json.loads(self.state_path.read_text(encoding="utf-8"))
            if isinstance(state, dict) and state.get("v") == 1:
                return state
        except (OSError, ValueError):
            pass
        return self._rebuild_state()

    def _rebuild_state(self):
        """The event log is the authority: a lost state file costs one re-read, never a re-copy."""
        state = {"v": 1, "seen": {}, "pairs": {}, "legacy": []}
        for event in self.events():
            key = event.get("key")
            if event.get("event") == "version" and key:
                state["pairs"][key] = {"sig": event["sig"], "id": event["id"], "folder": event["folder"],
                                       "stem": event["stem"], "misses": 0, "gone": False}
            elif event.get("event") == "vanished" and key in state["pairs"]:
                state["pairs"][key]["gone"] = True
            elif event.get("event") == "import" and event.get("legacy_id"):
                state["legacy"].append(event["legacy_id"])
        return state

    def _save_state(self):
        part = self.state_path.with_name(".state-" + uuid.uuid4().hex)
        part.write_text(json.dumps(self.state, sort_keys=True), encoding="utf-8")
        os.replace(part, self.state_path)

    def record(self, event):
        """Append one event (append-only: opened "ab", flushed and fsynced before the call returns)."""
        event = dict(event)
        event.setdefault("v", 1)
        event.setdefault("id", datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6])
        event.setdefault("time", now_text(self.clock()))
        path = self.root / "events" / (datetime.fromtimestamp(self.clock()).strftime("%Y-%m") + ".jsonl")
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

    def heartbeat(self, ok, pending, error=""):
        data = {"pid": os.getpid(), "last_sweep": self.clock(), "time": now_text(self.clock()), "ok": ok,
                "pending": pending, "error": error, "folders": [str(f) for f in self.folders]}
        path = self.root / "status" / "heartbeat.json"
        part = path.with_name(".hb-" + uuid.uuid4().hex)
        part.write_text(json.dumps(data, sort_keys=True), encoding="utf-8")
        os.replace(part, path)

    # ---- scanning
    @staticmethod
    def key_of(folder, stem):
        return canonical(folder) + "|" + stem.casefold()

    def scan(self):
        """{key: pair} for every .ess/.skse in the watched folders, and the set of folders that could be listed."""
        pairs, readable = {}, set()
        for folder in self.folders:
            try:
                names = os.listdir(folder)
            except OSError:
                continue
            readable.add(canonical(folder))
            lowered = {n.lower() for n in names}
            for name in names:
                low = name.lower()
                ext = next((e for e in EXTS if low.endswith(e)), None)  # exact suffix: X.ess.tmp never matches
                if ext is None:
                    continue
                path = folder / name
                try:
                    st = path.stat()
                except OSError:
                    continue
                if not os.path.isfile(path) or path.is_symlink():
                    continue
                stem = name[:-len(ext)]
                key = self.key_of(folder, stem)
                pair = pairs.setdefault(key, {"folder": str(folder), "stem": stem, "members": {}, "tmp": False})
                pair["members"][ext] = {"path": str(path), "size": st.st_size, "mtime_ns": st.st_mtime_ns,
                                        "mtime": st.st_mtime}
                if any((stem + e).lower() + ".tmp" in lowered for e in EXTS):   # the game is writing this pair now
                    pair["tmp"] = True
        for pair in pairs.values():
            pair["sig"] = [[pair["members"][e]["size"], pair["members"][e]["mtime_ns"]] if e in pair["members"] else None
                           for e in EXTS]
            pair["newest"] = max(m["mtime"] for m in pair["members"].values())
        return pairs, readable

    def due(self, key, pair, now, force=False):
        """Settled and not yet kept. force skips only the waiting, never the .tmp check."""
        seen = self.state["seen"].get(key)
        if seen is None:
            # first sight: no history, so the age of the files is the evidence
            self.state["seen"][key] = [pair["sig"], now - SETTLE_S]
        elif seen[0] != pair["sig"]:
            self.state["seen"][key] = [pair["sig"], now]          # changed since the last look: restart the clock
        if pair["tmp"]:
            return False
        kept = self.state["pairs"].get(key)
        if kept and kept.get("sig") == pair["sig"] and not kept.get("gone"):
            return False
        if force:
            return True
        if now - self.state["seen"][key][1] < SETTLE_S or now - pair["newest"] < SETTLE_S:
            return False
        if len(pair["members"]) < len(EXTS) and now - pair["newest"] < PAIR_GRACE_S:
            return False                                           # wait for the partner
        return True

    # ---- capture
    def capture(self, key, pair):
        """Keep one settled version of a pair; returns its event, or None if the files moved on before the copy."""
        with exclusive(self.root / "status" / "capture.lock", 60):
            links, members, held = [], [], []
            try:
                # Name every member first (microseconds apart), so the pair cannot straddle one of the game's saves;
                # check and hash only afterwards.
                for ext in EXTS:
                    member = pair["members"].get(ext)
                    if member is None:
                        continue
                    link = self.root / "staging-links" / (uuid.uuid4().hex + "-" + Path(member["path"]).name)
                    links.append(link)
                    try:
                        os.link(member["path"], link)                      # no handle stays on the live save
                    except OSError:
                        # locked (cannot be replaced either) or another volume: one short whole-file read
                        durable.copy_verified(member["path"], link)
                    held.append((ext, member, link))
                for ext, member, link in held:
                    st = os.stat(link)
                    if [st.st_size, st.st_mtime_ns] != [member["size"], member["mtime_ns"]]:
                        return None                                        # a newer write: the next pass takes it
                for ext in EXTS:
                    entry = next(((m, l) for e, m, l in held if e == ext), None)
                    if entry is None:
                        members.append({"original": str(Path(pair["folder"]) / (pair["stem"] + ext)), "missing": True,
                                        "source": str(self.root / "staging-links" / (uuid.uuid4().hex + "-absent" + ext))})
                        continue
                    member, link = entry
                    members.append({"original": member["path"], "source": str(link), "missing": False,
                                    "sha256": durable.digest(link), "size": member["size"]})
                # The links are the version from here on. If the game replaces the live save meanwhile, the link
                # keeps the old bytes but its change time moves (one fewer name), which fails the store's recheck:
                # retry on the SAME links, never re-link (the live file is already the next version).
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
        if manifest is None:
            return None
        recs = {Path(r["path"]).suffix.lower(): r for r in manifest["groups"][0]["members"]}
        info = {}
        for ext in EXTS:
            rec = recs.get(ext)
            info[ext[1:]] = None if rec is None or rec["missing"] else {
                "original": rec["path"], "sha256": rec["sha256"], "size": rec["size"],
                "mtime_ns": pair["members"][ext]["mtime_ns"]}
        missing = [e for e in EXTS if info[e[1:]] is None]
        prev = self.state["pairs"].get(key, {}).get("id")
        pair_sha = hashlib.sha256("|".join((info[e[1:]] or {}).get("sha256", "-") for e in EXTS).encode()).hexdigest()
        event = self.record({"event": "version", "key": key, "folder": pair["folder"], "stem": pair["stem"],
                             "ess": info["ess"], "skse": info["skse"], "sig": pair["sig"], "pair_sha256": pair_sha,
                             "transaction": manifest["id"], "prev": prev, "game_running": bool(self.is_busy()),
                             "note": ("co-save missing" if missing == [".skse"] else ".ess missing" if missing else "")})
        self.state["pairs"][key] = {"sig": pair["sig"], "id": event["id"], "folder": pair["folder"],
                                    "stem": pair["stem"], "misses": 0, "gone": False}
        return event

    # ---- one pass
    def sweep(self, force=False, only=None, limit=None):
        now = self.clock()
        pairs, readable = self.scan()
        queue = []
        for key, pair in pairs.items():
            if only and pair["stem"].casefold() != only.casefold():
                continue
            if self.due(key, pair, now, force):
                queue.append((pair["newest"], key, pair))
        queue.sort(reverse=True)                          # newest first: the slot the game just wrote
        if limit is None:
            limit = 1 if self.is_busy() else len(queue)
        captured, errors = [], []
        for _, key, pair in queue[:limit]:
            try:
                event = self.capture(key, pair)
                if event:
                    captured.append(event)
            except (OSError, PreservationFailed, TimeoutError) as exc:
                errors.append("{}: {}".format(pair["stem"], exc))
        vanished = self._vanished(pairs, readable) if only is None else []
        self._save_state()
        pending = len(queue) - len(captured)
        self.heartbeat(not errors, pending, "; ".join(errors)[:2000])
        return {"captured": captured, "vanished": vanished, "pending": pending, "errors": errors}

    def _vanished(self, pairs, readable):
        found = []
        for key, kept in self.state["pairs"].items():
            if kept.get("gone"):
                if key in pairs:
                    kept["gone"] = False                      # back again; a changed signature gets a new version
                    kept["misses"] = 0
                continue
            if key in pairs:
                kept["misses"] = 0
                continue
            if canonical(kept["folder"]) not in readable:
                continue                                      # an unreadable or missing folder proves nothing
            kept["misses"] = kept.get("misses", 0) + 1
            if kept["misses"] >= VANISH_MISSES:
                kept["gone"] = True
                found.append(self.record({"event": "vanished", "key": key, "folder": kept["folder"],
                                          "stem": kept["stem"], "last": kept["id"], "captured": True}))
        return found

    # ---- queries
    def versions(self, name, folder=None):
        stem = name
        for ext in EXTS:
            if stem.lower().endswith(ext):
                stem = stem[:-len(ext)]
        want = canonical(folder) if folder else None
        return [e for e in self.events() if e.get("event") == "version" and e.get("stem", "").casefold() == stem.casefold()
                and (want is None or canonical(e["folder"]) == want)]

    def choose(self, name, at=None, event_id=None, folder=None):
        versions = self.versions(name, folder)
        if event_id:
            versions = [e for e in versions if e["id"] == event_id]
        elif at is not None:
            versions = [e for e in versions if parse_time(e["time"]) <= at]
        if not versions:
            raise LookupError("no stored version of {} matches".format(name))
        folders = {canonical(e["folder"]) for e in versions}
        if len(folders) > 1 and not event_id:
            raise LookupError("{} exists in {} folders; pass --folder or --id".format(name, len(folders)))
        return versions[-1]

    # ---- restore
    def restore(self, name, at=None, event_id=None, folder=None, to=None, as_stem=None, replace=False, apply=False):
        """Dry run unless apply. A present pair needs replace (which keeps the current one first) or as_stem/to."""
        event = self.choose(name, at, event_id, folder)
        live = [Path(event["folder"]) / (event["stem"] + e) for e in EXTS]
        present = [p for p in live if p.exists()]
        if to:
            plan = {"mode": "copy-to", "destination": str(to)}
        elif as_stem:
            targets = [Path(event["folder"]) / (as_stem + e) for e in EXTS]
            clash = [str(t) for t in targets if t.exists()]
            if clash:
                raise FileExistsError("already exists: " + ", ".join(clash))
            plan = {"mode": "as", "targets": [str(t) for t in targets]}
        elif present:
            if not replace:
                raise FileExistsError("{} is present; pass --replace (keeps the current pair first), --as or --to"
                                      .format(", ".join(map(str, present))))
            plan = {"mode": "replace", "targets": [str(p) for p in live]}
        else:
            plan = {"mode": "put-back", "targets": [str(p) for p in live]}
        plan.update(version=event["id"], time=event["time"], transaction=event["transaction"], apply=apply)
        if not apply:
            return plan
        if plan["mode"] in ("replace", "put-back") and self.is_busy():
            raise PreservationFailed("the game (or SteamVR) is running, or that cannot be told: restore into the "
                                     "live folder refused; use --to or --as, or close the game")
        if plan["mode"] == "replace":
            key = self.key_of(event["folder"], event["stem"])
            pairs, _ = self.scan()
            if key in pairs and self.due(key, pairs[key], self.clock(), force=True):
                self.capture(key, pairs[key])            # the current pair is kept before it is replaced
                self._save_state()
            plan["result"] = self.store.restore(event["transaction"], in_place=True)
        elif plan["mode"] == "put-back":
            plan["result"] = self.store.restore(event["transaction"], in_place=True)
        elif plan["mode"] == "copy-to":
            plan["result"] = self.store.restore(event["transaction"], destination=to)
        else:
            plan["result"] = self._publish_as(event, plan["targets"])
        self.record({"event": "restored", "key": self.key_of(event["folder"], event["stem"]), "version": event["id"],
                     "mode": plan["mode"], "targets": plan.get("targets") or [str(to)]})
        return plan

    def _publish_as(self, event, targets):
        published = []
        for ext, target in zip(EXTS, targets):
            member = event[ext[1:]]
            if member is None:
                continue
            part = Path(target).with_name(".vault-" + uuid.uuid4().hex)
            try:
                info = durable.copy_verified(self.store.root / "blobs" / member["sha256"], part)
                if info["sha256"] != member["sha256"]:
                    raise PreservationFailed("stored version failed verification")
                durable.publish_new(part, target)
                published.append(str(target))
            finally:
                part.unlink(missing_ok=True)
        return {"published": published}

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
        """Copy the 3.x guard's backups in, one transaction per record. Never changes the old store."""
        manifest_path = Path(manifest_path)
        if not manifest_path.exists():
            return {"imported": 0, "skipped": 0, "failed": []}
        done = set(self.state.get("legacy", []))
        imported, skipped, failed = 0, 0, []
        for line in manifest_path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            rid = rec.get("id")
            if not rid or rid in done:
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
            if not any(not m["missing"] for m in members):
                skipped += 1
                done.add(rid)
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
            done.add(rid)
            imported += 1
        self.state["legacy"] = sorted(done)
        self._save_state()
        return {"imported": imported, "skipped": skipped, "failed": failed}


# --------------------------------------------------------------------------- liveness, watcher, task

def watcher_status(root=None, now=None):
    """(alive, one line) from the heartbeat; what session start, hook-canary and skydoctor print."""
    path = Path(root or default_root()) / "status" / "heartbeat.json"
    now = time.time() if now is None else now
    try:
        beat = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False, "save vault: NOT RUNNING (no heartbeat at {}); start it: save_vault.py watch".format(path)
    age = now - float(beat.get("last_sweep", 0))
    if age > STALE_S:
        return False, "save vault: NOT RUNNING (last sweep {:.0f} s ago, {}); restart: save_vault.py watch".format(
            age, beat.get("time"))
    tail = "" if beat.get("ok") else "; last pass had errors: " + str(beat.get("error"))[:200]
    return True, "save vault: running (last sweep {:.0f} s ago, {} pending){}".format(age, beat.get("pending"), tail)


def watch(vault_factory, stop=None):
    lower_priority()
    vault = vault_factory()
    try:
        with exclusive(vault.root / "status" / "watch.lock", 0):
            refreshed = time.monotonic()
            while not (stop and stop()):
                try:
                    if time.monotonic() - refreshed > FOLDER_REFRESH_S:
                        vault = vault_factory()
                        refreshed = time.monotonic()
                    vault.sweep()
                except Exception as exc:  # the watcher must outlive one bad pass; the heartbeat says what failed
                    try:
                        vault.heartbeat(False, -1, "{}: {}".format(type(exc).__name__, exc)[:2000])
                    except OSError:
                        pass
                time.sleep(POLL_S)
    except TimeoutError:
        print("save vault: a watcher is already running")
        return 0
    return 0


TASK = "SkyrimSaveVault"


def task_xml(python, script, args):
    return """<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo><Description>Keeps every version of every Skyrim save (save_vault.py watch).</Description></RegistrationInfo>
  <Triggers><LogonTrigger><Enabled>true</Enabled></LogonTrigger></Triggers>
  <Principals><Principal id="Author"><LogonType>InteractiveToken</LogonType><RunLevel>LeastPrivilege</RunLevel></Principal></Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <Priority>7</Priority>
    <RestartOnFailure><Interval>PT1M</Interval><Count>999</Count></RestartOnFailure>
    <StartWhenAvailable>true</StartWhenAvailable>
  </Settings>
  <Actions Context="Author"><Exec><Command>{}</Command><Arguments>-I "{}" {}watch</Arguments></Exec></Actions>
</Task>
""".format(python, script, args)


def install_task(args_text):
    if os.name != "nt":
        raise SystemExit("install-task: Windows only (on Linux run `save_vault.py watch` from a systemd --user unit)")
    py = Path(sys.executable)
    pyw = py.with_name("pythonw.exe")
    xml = task_xml(str(pyw if pyw.exists() else py), str(Path(__file__).resolve()), args_text)
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
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("sweep"); s.add_argument("--now", action="store_true"); s.add_argument("--pair")
    sub.add_parser("watch")
    st = sub.add_parser("status"); st.add_argument("name", nargs="?"); st.add_argument("--vanished", action="store_true")
    r = sub.add_parser("restore")
    r.add_argument("name"); r.add_argument("--at"); r.add_argument("--id"); r.add_argument("--in-folder")
    g = r.add_mutually_exclusive_group(); g.add_argument("--to"); g.add_argument("--as", dest="as_stem")
    g.add_argument("--replace", action="store_true"); r.add_argument("--apply", action="store_true")
    v = sub.add_parser("verify"); v.add_argument("--sample", type=int)
    sub.add_parser("import-legacy")
    sub.add_parser("install-task"); sub.add_parser("uninstall-task")
    a = ap.parse_args(argv)
    root = Path(a.vault) if a.vault else default_root()

    def make():
        return Vault(root, discover(a.project, a.folder))

    if a.cmd == "watch":
        return watch(make)
    if a.cmd == "install-task":
        extra = "".join('--folder "{}" '.format(f) for f in a.folder)
        return install_task('--project "{}" {}{}'.format(a.project, '--vault "{}" '.format(root) if a.vault else "", extra))
    if a.cmd == "uninstall-task":
        return subprocess.run(["schtasks", "/Delete", "/TN", TASK, "/F"]).returncode
    if a.cmd == "status" and not a.name and not a.vanished:
        alive, line = watcher_status(root)
        print(line)
        return 0 if alive else 1
    vault = make()
    if a.cmd == "sweep":
        out = vault.sweep(force=a.now, only=a.pair, limit=None if not a.now else 10 ** 6)
        print("captured {}  vanished {}  pending {}  errors {}".format(len(out["captured"]), len(out["vanished"]),
                                                                     out["pending"], len(out["errors"])))
        for e in out["errors"]:
            print("  error:", e)
        return 1 if out["errors"] else 0
    if a.cmd == "status":
        if a.vanished:
            for e in vault.events():
                if e.get("event") == "vanished":
                    print(e["time"], e["stem"], "(last version", e["last"] + ")")
            return 0
        for e in vault.versions(a.name):
            print(e["time"], e["id"], "ess", (e["ess"] or {}).get("sha256", "-")[:12], "skse",
                  (e["skse"] or {}).get("sha256", "-")[:12], e.get("note", ""))
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
