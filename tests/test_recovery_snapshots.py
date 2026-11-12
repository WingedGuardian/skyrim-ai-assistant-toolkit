"""Plan 6 Task 15 (spec 5.3): recovery snapshots of the load order, the INIs and every save a command changes.

Before a non-read-only shell command: plugins.txt / loadorder.txt (declared SKYRIM_LOADORDER_DIR) and Skyrim*.ini
(declared SKYRIM_CONFIG_DIR), rate-limited and deduplicated by content hash. Before a call that changes a save (the save
policy, user ruling 2026-10-13: saves.py): that save and its .skse co-save, verified, by the deciding hook -- never the
save folder. Store: .claude/backups/recovery_snapshots/, Task 13's manifest line, content-addressed blobs."""
import hashlib
import json
import os
import subprocess
import sys
import time

import pytest

from conftest import REPO

sys.path.insert(0, str(REPO / "tools"))
from skyguard import audit as audit_mod, mcp_rules, paths, recovery, shell_rules  # noqa: E402
from skyguard import save_store
from modding_harness import workspace
from test_save_policy import live_enforce  # noqa: E402

DAY = 86400


def sha(body):
    return hashlib.sha256(body).hexdigest()


def put(p, body, mtime=None):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(body)
    if mtime is not None:
        os.utime(str(p), (mtime, mtime))
    return p


class Install:
    """A scratch install: the game folder, a config dir (INIs + Saves) and a load-order dir, declared as roots."""

    def __init__(self, tmp_path):
        self.game = tmp_path / "game"
        self.game.mkdir()
        self.cfg = tmp_path / "docs" / "My Games" / "Skyrim VR"
        self.lo = tmp_path / "appdata" / "Skyrim VR"
        self.saves = self.cfg / "Saves"
        put(self.lo / "plugins.txt", b"*Skyrim.esm\n*Mine.esp\n")
        put(self.lo / "loadorder.txt", b"Skyrim.esm\nMine.esp\n")
        put(self.lo / "notes.txt", b"not a load-order file")
        put(self.cfg / "Skyrim.ini", b"[General]\n")
        put(self.cfg / "SkyrimVR.ini", b"[VR]\n")
        put(self.cfg / "skyrimprefs.INI", b"[Display]\n")
        put(self.cfg / "Other.ini", b"not a game ini")
        put(self.saves / "Quick.ess", b"quick save bytes")
        put(self.saves / "Quick.skse", b"quick co-save")
        put(self.saves / "Other.ess", b"another save")
        put(self.saves / "Other.skse", b"another co-save")
        self.roots = paths.roots(str(self.game), str(self.game),
                                 {"SKYRIM_CONFIG_DIR": str(self.cfg), "SKYRIM_LOADORDER_DIR": str(self.lo)})
        self.audit = audit_mod.Audit(str(self.game))

    @property
    def store(self):
        return recovery.store_dir(str(self.game))

    @property
    def save_store(self):
        return save_store.store(self.game)

    def log(self):
        p = self.game / ".claude" / "backups" / "AUDIT_LOG.txt"
        return p.read_bytes().decode("utf-8") if p.is_file() else ""

    def lines(self):
        entries, bad = recovery.read_manifest(self.store)
        assert bad == 0
        for manifest in workspace.summaries(self.save_store)[0]:
            for group in manifest["groups"]:
                absent = [r["path"] for r in group["members"] if r["missing"]]
                entries.extend(dict(src=r["path"], sha256=r["sha256"], size=r["size"], kind="save",
                                    snap="core-" + manifest["id"], missing=absent)
                               for r in group["members"] if not r["missing"])
        return entries

    def run(self, command, now=None, cwd="", shell="bash", deadline=None):
        recovery.run(str(self.game), command, False, self.audit, self.roots, cwd=cwd, shell=shell, now=now,
                     deadline=deadline, env={})

    def keep(self, name):
        """A verified backup of one save pair in the permanent store, as the 4.0 hooks took before a save change.
        No hook takes one since 4.1 (tools/save_vault.py keeps every version); the backups already in a user's store
        must stay listed, kept forever and restorable, which is what the tests using this hold."""
        return save_store.backup_pairs(self.game, self.audit,
                                       [(self.saves / (name + ".ess"), self.saves / (name + ".skse"))], "test backup")

    def change(self, command, cwd="", shell="bash", deadline=None):
        """A save change as the deciding hook makes it: the rules, then the LIVE dispatcher (approval, backup)."""
        v = shell_rules.decide(command, self.roots, self.audit, cwd=cwd, shell=shell, env={})
        return live_enforce(v, self.game, self.roots, self.audit, command=command, deadline=deadline)

    def mcp(self, tool_name, args):
        v = mcp_rules.decide(tool_name, args, self.audit, roots=self.roots, env={})
        return live_enforce(v, self.game, self.roots, self.audit, command=tool_name)

    def stored(self, kind=None):
        """{basename: sha256} of every recorded line (of one kind)."""
        return {os.path.basename(e["src"]): e["sha256"] for e in self.lines() if kind in (None, e["kind"])}


@pytest.fixture
def inst(tmp_path):
    return Install(tmp_path)


def later(n=1):
    return time.time() + n * (recovery.CONFIG_RATE + 60)


# ---------------------------------------------------------------- load order and INIs


def test_a_command_snapshots_the_load_order_and_the_game_inis_from_the_configured_roots(inst):
    inst.run("make")
    got = inst.stored("config")
    assert sorted(got) == ["Skyrim.ini", "SkyrimVR.ini", "loadorder.txt", "plugins.txt", "skyrimprefs.INI"]
    assert got["plugins.txt"] == sha(b"*Skyrim.esm\n*Mine.esp\n")
    assert (inst.store / "blobs" / sha(b"*Skyrim.esm\n*Mine.esp\n")).read_bytes() == b"*Skyrim.esm\n*Mine.esp\n"


def test_find_file_listing_keeps_config_recovery_before_writing(inst):
    inst.run("find . -fls Skyrim.ini")
    assert inst.stored("config")["Skyrim.ini"] == sha(b"[General]\n")
    assert "RECOVERY-SNAPSHOT" in inst.log()


@pytest.mark.parametrize("cmd", ["ls -la", "git status", "Get-ChildItem Data"])
def test_a_read_only_command_takes_no_recovery_snapshot(inst, cmd):
    inst.run(cmd, shell="powershell" if cmd.startswith("Get-") else "bash")
    assert not inst.store.exists() or inst.lines() == []


@pytest.mark.parametrize("command,shell", [
    ("cat notes.txt; printf x > Skyrim.ini", "bash"),
    ("Get-Content notes.txt; Set-Content Skyrim.ini x", "powershell"),
    ("Get-Content notes.txt | ForEach-Object { Set-Content Skyrim.ini x }", "powershell"),
])
def test_a_read_prefix_keeps_config_recovery_before_later_writes(inst, command, shell):
    inst.run(command, shell=shell)
    assert "Skyrim.ini" in inst.stored("config")


def test_a_manifest_line_has_the_task_13_shape(inst):
    inst.run("make")
    e = next(x for x in inst.lines() if x["src"].endswith("plugins.txt"))
    for k in ("v", "ts", "src", "dest", "size", "sha256", "tool", "agent", "session_id", "kind", "snap", "command"):
        assert k in e, k
    assert e["v"] == 1 and e["dest"] == e["sha256"] and e["size"] == len(b"*Skyrim.esm\n*Mine.esp\n")
    assert e["kind"] == "config" and e["command"] == "make"


def test_unchanged_content_is_recorded_once(inst):
    inst.run("make")
    n = len(inst.lines())
    inst.run("make again", now=later())                # past the rate window: everything is re-read, nothing differs
    assert len(inst.lines()) == n
    assert len(list((inst.store / "blobs").iterdir())) == n


def test_a_change_is_recorded_at_the_next_command_inside_the_rate_window(inst):
    inst.run("make")
    put(inst.lo / "plugins.txt", b"*Skyrim.esm\n")    # new size: the listing says it changed
    inst.run("second command")
    changed = [e for e in inst.lines() if e["src"].endswith("plugins.txt")]
    assert [e["sha256"] for e in changed] == [sha(b"*Skyrim.esm\n*Mine.esp\n"), sha(b"*Skyrim.esm\n")]
    assert len(inst.lines()) == 6, "only the changed file gets a new line"


def test_inside_the_rate_window_an_unchanged_listing_is_not_read(inst, monkeypatch):
    inst.run("make")
    reads = []
    real = recovery.snapshot._store_blob
    monkeypatch.setattr(recovery.snapshot, "_store_blob", lambda b, s, **k: reads.append(s) or real(b, s, **k))
    inst.run("second command")
    assert reads == []


def test_after_the_rate_window_a_rewrite_that_keeps_size_and_mtime_is_seen(inst):
    p = inst.cfg / "Skyrim.ini"
    st = p.stat()
    inst.run("make")
    put(p, b"[Genera1]\n", mtime=st.st_mtime)          # same size, same mtime: only a re-read can tell
    os.utime(str(p), ns=(st.st_atime_ns, st.st_mtime_ns))
    inst.run("inside the window")
    assert inst.stored("config")["Skyrim.ini"] == sha(b"[General]\n")
    inst.run("after the window", now=later())
    assert inst.stored("config")["Skyrim.ini"] == sha(b"[Genera1]\n")


def test_nothing_outside_the_configured_roots_is_read(inst, tmp_path):
    put(tmp_path / "elsewhere" / "Skyrim.ini", b"not configured")
    inst.run("make")
    assert all("elsewhere" not in e["src"] for e in inst.lines())
    assert all("Saves" not in e["src"] for e in inst.lines()), "an ordinary command never copies a save"


def test_an_install_that_declares_no_roots_says_so_once(tmp_path):
    game = tmp_path / "game"
    game.mkdir()
    a = audit_mod.Audit(str(game))
    roots = paths.roots(str(game), str(game), {})
    for i in range(3):
        recovery.run(str(game), "make", False, a, roots, now=time.time() + i * 1000, env={})
    log = (game / ".claude" / "backups" / "AUDIT_LOG.txt").read_bytes().decode("utf-8")
    assert log.count("NOT CONFIGURED") == 1


def test_unreadable_roots_are_logged_not_silent(inst):
    recovery.run(str(inst.game), "make", False, inst.audit, None, env={})
    assert "the paths file could not be read" in inst.log()


# ---------------------------------------------------------------- the save a command changes (the save policy)
# User ruling 2026-10-13: the DECIDING hook backs up every save a call changes (portable.check -> dispatcher), both
# files of each pair, verified, BEFORE it allows the call; any failure denies. These are the Task 15 cases, ported:
# the save is found exactly as before, and now its backup decides the call.


def test_a_failed_manifest_append_is_retried_by_the_next_call(inst, monkeypatch):
    """The index is updated only once the lines are on disk; otherwise the unchanged file would never be recorded."""
    real = recovery.backup._append_line

    def fail(*a, **k):
        raise OSError("disk full")
    monkeypatch.setattr(recovery.backup, "_append_line", fail)
    inst.run("make")
    assert "could not append" in inst.log()
    monkeypatch.setattr(recovery.backup, "_append_line", real)
    inst.run("make again")
    assert "plugins.txt" in inst.stored("config")


def test_resaver_writing_a_new_out_keeps_nothing_and_says_so(inst):
    """A NEW out changes no save: nothing to back up, and no note claims a backup."""
    command = "bash tools/resaver-cli.sh set-global Quick.ess 0x14 1 Fresh.ess --apply"
    v = inst.change(command, cwd=str(inst.game))
    assert (v.decision, v.context) == ("allow", "")
    assert inst.stored("save") == {}
    # The rules' own verdict claims no backup either (the live dispatcher re-checks existence before preserving, so
    # only this reading can show a pair wrongly counted as existing).
    ruled = shell_rules.decide(command, inst.roots, inst.audit, cwd=str(inst.game), shell="bash", env={})
    assert ruled.context == ""


def test_a_devbench_save_under_a_new_name_copies_nothing(inst):
    assert inst.mcp("mcp__devbench-vr__game", {"action": "save", "name": "Brand New"})[:3] == ("allow", "", "")
    assert inst.stored("save") == {}


@pytest.mark.parametrize("tool,args", [
    ("game", {"action": "save", "name": "Quick"}), ("game", {"action": " SAVE ", "name": "quick.ess"}),
    ("game", {"action": "save", "name": "Nope"}), ("game", {"action": "load", "name": "Quick"}),
    ("console", {"command": "save Quick"}), ("console", {"command": "coc riverwood; SAVE Other"}),
    ("console", {"command": "player.additem f 1"}), ("console", {"command": 5}), ("game", {"action": "save"}),
], ids=["game", "game-case", "game-new", "game-load", "console", "console-second", "console-other",
        "console-number", "game-no-name"])
def test_the_save_snapshot_reads_a_call_exactly_as_the_rule_does(inst, tool, args):
    """judge() reports every save a call writes (`found`), and the backup takes exactly those: an existing save is
    kept and allowed, an unknowable one refused, and a call the rule lets through copies nothing."""
    hit = mcp_rules.judge(tool, args, inst.roots, None, {})
    v = inst.mcp("mcp__devbench-vr__" + tool, args)
    kept = inst.stored("save") != {}
    refused = v.decision == "deny"
    assert (kept or refused) == (hit is not None and hit[0] == "S-DEVBENCH-SAVE")


# ---------------------------------------------------------------- the real entry points, and the ordering


def _engine(inst, argv, payload):
    env = dict(os.environ)
    env.pop("CLAUDE_ENV_FILE", None)
    tmp = str(inst.game.parent)
    env.update({"SKYRIM_CONFIG_DIR": str(inst.cfg), "SKYRIM_LOADORDER_DIR": str(inst.lo), "USERPROFILE": tmp,
                "HOME": tmp, "OneDrive": "", "OneDriveConsumer": "", "OneDriveCommercial": ""})
    cmd = [sys.executable, "-B", str(REPO / "tools" / "skyguard" / "run.py"), *argv, "--project", str(inst.game),
           "--root", str(inst.game), "--root-win", str(inst.game).replace("\\", "/")]
    return subprocess.run(cmd, input=json.dumps(payload).encode("utf-8"), capture_output=True, timeout=120, env=env)


def _resaver_payload(inst):
    return {"session_id": "s-1", "tool_name": "Bash", "hook_event_name": "PreToolUse", "cwd": str(inst.game),
            "tool_input": {"command": "bash tools/resaver-cli.sh clean Quick.ess --apply"}}


def test_the_snapshot_hook_takes_the_load_order_but_never_the_save(inst):
    """Claude's snapshot-before-tool runs BESIDE protect-bash: it keeps the load order and INIs, the save is the
    deciding hook's (two writers of one backup would race for the store lock and could deny each other)."""
    r = _engine(inst, ["claude", "snapshot-before-tool"], _resaver_payload(inst))
    assert r.returncode == 0, r.stderr
    assert inst.stored("save") == {} and "plugins.txt" in inst.stored("config")


# ---------------------------------------------------------------- resolving and pruning


def test_resolve_gives_the_full_state_at_a_snapshot(inst):
    inst.run("first")
    first = inst.lines()[0]["snap"]
    put(inst.lo / "plugins.txt", b"*Skyrim.esm\n")
    inst.run("second")
    put(inst.lo / "plugins.txt", b"*Skyrim.esm\n*Third.esp\n*More.esp\n")
    inst.run("third")
    second = inst.lines()[5]["snap"]
    state = recovery.resolve(inst.store, second)
    assert len(state) == 5
    plugins = next(s for s in state if s.endswith("plugins.txt"))
    assert state[plugins]["sha256"] == sha(b"*Skyrim.esm\n")
    assert recovery.resolve(inst.store, first)[plugins]["sha256"] == sha(b"*Skyrim.esm\n*Mine.esp\n")


def test_two_snapshots_in_one_second_get_different_ids(inst, monkeypatch):
    monkeypatch.setattr(inst.audit, "stamp", lambda: "20261005_120000")
    inst.run("first")
    put(inst.lo / "plugins.txt", b"*Skyrim.esm\n")
    inst.run("second")
    assert len({e["snap"] for e in inst.lines()}) == 2


def test_pruning_drops_old_lines_but_keeps_each_paths_newest(inst):
    """An old snapshot that holds no path's newest content goes as a whole; the newest snapshot of each path stays
    (fix round 1, I4: a snapshot that still holds a newest line is kept WHOLE, so it stays restorable)."""
    old = time.time() - 30 * DAY
    inst.run("old", now=old)
    put(inst.lo / "plugins.txt", b"*Skyrim.esm\n")
    for p in (inst.cfg / "Skyrim.ini", inst.lo / "loadorder.txt", inst.cfg / "SkyrimVR.ini",
              inst.cfg / "skyrimprefs.INI"):
        put(p, b"newer")
    inst.run("newer", now=old + 1)
    newer = inst.lines()[-1]["snap"]
    recovery.prune(inst.store, time.time(), inst.audit)
    after = inst.lines()
    assert [e["snap"] for e in after] == [newer] * 5, "the old snapshot, superseded on every path, goes whole"
    assert not (inst.store / "blobs" / sha(b"*Skyrim.esm\n*Mine.esp\n")).exists()
    assert (inst.store / "blobs" / sha(b"*Skyrim.esm\n")).exists()
    # Review L2 LOW-7: the pruned blob is retired (then recycled by housekeep), never deleted.
    from skyguard import retire
    moved = [p for p in retire.retired_dir(inst.store).glob("*/*")
             if p.name.split("-", 1)[1] == sha(b"*Skyrim.esm\n*Mine.esp\n")]
    assert len(moved) == 1 and moved[0].read_bytes() == b"*Skyrim.esm\n*Mine.esp\n"


def test_pruning_never_cuts_into_the_state_a_kept_snapshot_needs(inst):
    """Fix round 1, I4 (MEASURED by the reviewer): S1 day 0 (5 files), S2 day 10 (plugins.txt), S3 day 13 (the other
    four); pruned on day 15, S2 lost Skyrim.ini and still 'restored' -- a state that never existed. Now S2 resolves
    whole, and S1 (whose plugins.txt line went) is refused, never returned partial."""
    day0 = time.time() - 15 * DAY
    inst.run("s1", now=day0)
    s1 = inst.lines()[0]["snap"]
    put(inst.lo / "plugins.txt", b"*Skyrim.esm\n")
    inst.run("s2", now=day0 + 10 * DAY)
    s2 = inst.lines()[-1]["snap"]
    for p in (inst.cfg / "Skyrim.ini", inst.lo / "loadorder.txt", inst.cfg / "SkyrimVR.ini",
              inst.cfg / "skyrimprefs.INI"):
        put(p, b"changed on day 13")
    inst.run("s3", now=day0 + 13 * DAY)
    before = {s: e["sha256"] for s, e in recovery.resolve(inst.store, s2).items()}
    assert len(before) == 5
    recovery.prune(inst.store, time.time(), inst.audit)
    after = {s: e["sha256"] for s, e in recovery.resolve(inst.store, s2).items()}
    assert after == before
    with pytest.raises(ValueError, match="incomplete"):
        recovery.resolve(inst.store, s1)


def _drop_line(inst, pred):
    raw = [r for r in (inst.store / recovery.MANIFEST).read_bytes().split(b"\n") if r.strip()]
    keep = [r for r in raw if not pred(json.loads(r))]
    assert len(keep) == len(raw) - 1
    (inst.store / recovery.MANIFEST).write_bytes(b"\n".join(keep) + b"\n")


def test_a_snapshot_missing_one_of_its_own_lines_is_refused(inst):
    """Its state still resolves (plugins.txt falls back to the older line) -- only the line count tells."""
    inst.run("s1")
    put(inst.lo / "plugins.txt", b"*Skyrim.esm\n")
    put(inst.cfg / "Skyrim.ini", b"[changed]\n")
    inst.run("s2")
    s2 = inst.lines()[-1]["snap"]
    _drop_line(inst, lambda e: e["snap"] == s2 and e["src"].endswith("plugins.txt"))
    with pytest.raises(ValueError, match="1 of its 2 file"):
        recovery.resolve(inst.store, s2)


def test_a_snapshot_whose_earlier_state_line_is_gone_is_refused(inst):
    """Its own lines are all there; a path it holds from an EARLIER snapshot is not -- only the state check tells."""
    inst.run("s1")
    s1 = inst.lines()[0]["snap"]
    put(inst.lo / "plugins.txt", b"*Skyrim.esm\n")
    inst.run("s2")
    s2 = inst.lines()[-1]["snap"]
    _drop_line(inst, lambda e: e["snap"] == s1 and e["src"].endswith("Skyrim.ini"))
    with pytest.raises(ValueError, match="no stored copy of .*Skyrim.ini"):
        recovery.resolve(inst.store, s2)


def test_old_save_snapshots_are_kept_forever(inst):
    """The save policy: save backups are never pruned, however old (disk space is the user's call)."""
    inst.keep("Quick")
    put(inst.saves / "Quick.ess", b"rewritten")
    inst.keep("Quick")
    recovery.prune(inst.store, time.time() + 3650 * DAY, inst.audit)
    assert [e["sha256"] for e in inst.lines() if e["src"].endswith("Quick.ess")] == \
        [sha(b"quick save bytes"), sha(b"rewritten")]
    assert all(((inst.save_store.root if e["kind"] == "save" else inst.store) / "blobs" / e["sha256"]).is_file()
               for e in inst.lines())


def test_an_unreadable_manifest_line_stops_blob_pruning_and_is_kept(inst):
    old = time.time() - 30 * DAY
    inst.run("old", now=old)
    put(inst.lo / "plugins.txt", b"*Skyrim.esm\n")
    inst.run("newer", now=old + 1)
    with open(str(inst.store / recovery.MANIFEST), "ab") as fh:
        fh.write(b"{not json\n")
    recovery.prune(inst.store, time.time(), inst.audit)
    assert (inst.store / "blobs" / sha(b"*Skyrim.esm\n*Mine.esp\n")).exists()
    assert b"{not json" in (inst.store / recovery.MANIFEST).read_bytes()
