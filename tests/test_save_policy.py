"""The save policy (user ruling 2026-10-13): saves.py, and the LIVE hook path (portable.check -> dispatcher).

  * 1 or 2 saves changed (a save and its .skse co-save are ONE save): the hook backs up BOTH files of each pair, each
    verified by SHA-256, records them, and only then allows the call.
  * more than 2, or a wildcard: refused until the USER types a SAVE_ACCESS_APPROVAL line (approval.py); then backed
    up the same way and allowed.
  * reading, hashing, listing, copying out: free. Deleting or moving the Saves FOLDER: still denied.
  * any backup failure: denied (fail closed). Save backups are never pruned; a restore puts both files back, verified.

Every test uses fake saves in tmp_path. No real save is read or written.
"""
import json
import sys
from pathlib import Path

import pytest

from conftest import REPO

sys.path.insert(0, str(REPO / "tools"))
from skyguard import (audit as audit_mod, backup, engine, mcp_rules, paths, recovery, rules,  # noqa: E402
                      shell_rules, snapcli, snapshot, write_rules)
from skyguard import save_store
from modding_harness import approval as scoped_approval, workspace
from skyguard.adapters import codex  # noqa: E402

NOTE = "Note: the guard backs up {} before this runs (restore: tools/skyguard.sh snapshot list --kind save)."


class Inst:
    """A scratch install: the game folder, and a config dir whose Saves folder holds three save pairs (Save1..Save3),
    a save with no co-save (Single) and a lone co-save (Orphan)."""

    def __init__(self, tmp_path):
        self.game = tmp_path / "game"
        (self.game / "Data").mkdir(parents=True)
        self.cfg = tmp_path / "docs" / "My Games" / "Skyrim VR"
        self.saves = self.cfg / "Saves"
        self.saves.mkdir(parents=True)
        for stem in ("Save1", "Save2", "Save3"):
            (self.saves / (stem + ".ess")).write_bytes(stem.encode() * 100)
            (self.saves / (stem + ".skse")).write_bytes(stem.encode() * 7)
        (self.saves / "Single.ess").write_bytes(b"single" * 50)
        (self.saves / "Orphan.skse").write_bytes(b"orphan")
        self.roots = paths.roots(str(self.game), str(self.game), {"SKYRIM_CONFIG_DIR": str(self.cfg)})
        self.audit = audit_mod.Audit(str(self.game))
        self.S = self.saves.as_posix()

    def p(self, name):
        return '"{}/{}"'.format(self.S, name)

    def shell(self, command, shell="bash", cwd=None):
        return shell_rules.decide(command, self.roots, self.audit, cwd=self.game.as_posix() if cwd is None else cwd,
                                  shell=shell, env={})

    def enforce(self, v, transcript="", agent="claude", deadline=None):
        """The LIVE deciding path every hook takes (portable.legacy_pre_tool -> the shared dispatcher's approval
        check and verified pair backup), given the rules' verdict `v` (L2 MEDIUM-2: not the retired saves.enforce)."""
        return live_enforce(v, self.game, self.roots, self.audit, transcript=transcript, agent=agent,
                            deadline=deadline)

    def store(self):
        return save_store.store(self.game)

    def saved(self):
        return saved(self.game)

    def grant(self, names=("Save1", "Save2", "Save3"), op="delete", wildcard=False, count=3, command="test"):
        return scoped_approval.example("s1", op,
            [self.saves / (name + ext) for name in names for ext in (".ess", ".skse")],
            command, wildcard, count)

    def transcript(self, *lines, name="t.jsonl", typed=True):
        rows = []
        for line in lines:
            if typed:
                rows.append({"type": "user", "origin": {"kind": "human"}, "promptSource": "typed",
                             "message": {"role": "user", "content": line}})
            else:
                rows.append({"type": "user", "message": {"role": "user", "content": [
                    {"type": "tool_result", "tool_use_id": "x", "content": line}]}})
        f = self.game / name
        f.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
        return str(f)


def live_enforce(v, game, roots, audit, transcript="", agent="claude", deadline=None, command="test"):
    from skyguard import portable
    from skyguard.request import Request
    req = Request(agent=agent, event="pre_tool", kind="shell", command=command, session_id="s1",
                  transcript_path=transcript, cwd=Path(game).as_posix(), tool_name="Bash")
    return portable.legacy_pre_tool(req, str(game), lambda: roots, audit, deadline=deadline, precomputed=v,
                                    side_effects=False)


def keep(inst, *stems):
    """A verified backup of each named save pair in the permanent store, as the 4.0 hooks took before a save change.
    The hooks take none since 4.1 (tools/save_vault.py keeps every version instead); the backups already in a user's
    store must stay listed, kept forever and restorable, which is what the tests below hold."""
    return save_store.backup_pairs(inst.game, inst.audit,
                                   [(inst.saves / (s + ".ess"), inst.saves / (s + ".skse")) for s in stems], "test backup")


@pytest.fixture
def inst(tmp_path):
    return Inst(tmp_path)


def test_workspace_root_save_does_not_make_the_whole_workspace_a_save_folder(tmp_path):
    game = tmp_path / "workspace"
    game.mkdir()
    ess, skse = game / "Copied.ess", game / "Copied.skse"
    ess.write_bytes(b"copied private fixture")
    ids = save_store.backup_pairs(game, audit_mod.Audit(str(game)), [(ess, skse)], "offline edit")
    assert len(ids) == 1
    manifest = save_store.store(game).load(ids[0][len(save_store.PREFIX):])
    assert manifest["groups"][0]["members"][1]["missing"]


def test_worktree_restore_wrappers_use_the_main_configured_save_folder(inst, tmp_path):
    import io
    (inst.game / "skyrim-paths.env").write_text("SKYRIM_CONFIG_DIR='" + inst.cfg.as_posix() + "'\n")
    linked = tmp_path / "linked"
    linked.mkdir()
    gitdir = inst.game / ".git/worktrees/linked"
    gitdir.mkdir(parents=True)
    (linked / ".git").write_text("gitdir: " + gitdir.as_posix() + "\n")
    pair = [inst.saves / ("Save1" + ext) for ext in (".ess", ".skse")]
    sid, = save_store.backup_pairs(linked, inst.audit, [pair], "private worktree fixture")
    original = [p.read_bytes() for p in pair]
    for p in pair:
        p.write_bytes(b"current")
    output = io.StringIO()
    assert save_store.restore(linked, sid, [], None, True, output) == 0, output.getvalue()
    assert [p.read_bytes() for p in pair] == original
    assert snapcli._allowed(linked, "20261015_120000-save", "unused", pair[0]) == ""
    transactions = workspace.summaries(save_store.store(linked))[0]
    assert len(transactions) >= 2


def test_configured_store_cannot_live_in_a_save_root_or_hold_a_source(tmp_path, monkeypatch):
    game = tmp_path / "workspace"
    game.mkdir()
    config = game / ".modding-agent-harness/config.json"
    config.parent.mkdir()
    cfg = tmp_path / "config"
    monkeypatch.setenv("SKYRIM_CONFIG_DIR", str(cfg))
    config.write_text(json.dumps({"save_store": str(cfg / "Saves/history")}))
    with pytest.raises(ValueError, match="separate from save roots"):
        save_store.store(game)
    config.write_text("{}")
    with pytest.raises(ValueError, match="overlaps"):
        save_store.store(game, [game / ".modding-agent-harness/backups/saves/inside.ess"])


def saved(project):
    records = []
    for manifest in workspace.summaries(save_store.store(project))[0]:
        for group in manifest["groups"]:
            missing = [r["path"] for r in group["members"] if r["missing"]]
            for member in group["members"]:
                if not member["missing"]:
                    records.append(dict(src=member["path"], snap="core-" + manifest["id"],
                                        group=group["logical_save"], sha256=member["sha256"], missing=missing))
    return records


# ================================================================ what counts as a save change (judge)


def test_a_quoted_wildcard_over_saves_is_not_a_settings_delete(inst):
    """A quoted `*` is literal to bash, so the delete rules read the word literally (deletes.literal_abs); it is still a
    save word, and no rule decides on a save (4.1): never refused as a delete of the settings folder."""
    v = inst.shell('rm "' + inst.S + '/Save*.ess"')
    assert v.decision == "allow" and v.reason == "", v


@pytest.mark.parametrize("cmd,rid", [('rm -rf "{dir}"', "S-DEL-CONFIG"), ('mv "{dir}" /tmp/old', "S-MOVE-ROOT"),
                                     ('Remove-Item -Recurse "{dir}"', "S-DEL-CONFIG")],
                         ids=["rm-folder", "mv-folder", "ps-remove-folder"])
def test_the_whole_saves_folder_stays_a_hard_deny(inst, cmd, rid):
    v = inst.shell(cmd.format(dir=inst.S), shell="powershell" if cmd[0].isupper() else "bash")
    assert v.decision == "deny" and v.reason == rules.message(rid, file="Saves")


@pytest.mark.parametrize("tail", ["", r"\0\uD800", r"\0\Uffffffff", r"\Uffffffff", r"\U80000000"])
@pytest.mark.parametrize("escape", [r"\x65", r"\145", r"\u0065", r"\U00000065"])
def test_ansi_c_plugin_suffix_remains_a_raw_binary_write_refusal(inst, escape, tail):
    verdict = inst.shell("printf x > $'" + inst.game.as_posix() + "/Data/plugin." + escape + "sp" + tail + "'")
    assert verdict.decision == "deny"
    assert "raw" in verdict.reason.lower() or "text" in verdict.reason.lower()


# ---------------------------------------------------------------- ReSaver and DevBench


def mcp(inst, args):
    return mcp_rules.decide("mcp__devbench-vr__game", args, inst.audit, roots=inst.roots, env={})


def test_a_devbench_save_under_a_new_name_is_free(inst):
    assert mcp(inst, {"action": "save", "name": "Brand New"})[:3] == ("allow", "", "")


# ---------------------------------------------------------------- edit tools and patches


def codex_pre(inst, tool_name, tool_input, transcript="", cwd=None):
    d = {"hook_event_name": "PreToolUse", "tool_name": tool_name, "cwd": cwd or inst.game.as_posix(),
         "tool_input": tool_input, "session_id": "s1"}
    if transcript:
        d["transcript_path"] = transcript
    req = codex.to_request(json.dumps(d), "win32")
    return engine.pre_tool(req, str(inst.game), lambda: inst.roots, inst.audit)


def _patch(*lines):
    return {"command": "\n".join(("*** Begin Patch",) + lines + ("*** End Patch",))}


# ================================================================ the backup (the live dispatcher)


def test_a_new_save_takes_no_backup(inst):
    v = inst.enforce(inst.shell("cp /tmp/x " + inst.p("Brand New.ess")))
    assert v.decision == "allow" and inst.saved() == []


def failed(v):
    return v.decision == "deny" and v.code == "shell.S-SAVE-BACKUP-FAILED"


# ---------------------------------------------------------------- approvals


APPROVE3 = 'SAVE_ACCESS_APPROVAL {"op": "delete", "files": ["Save1", "Save2", "Save3"]}'


def three(inst):
    return inst.shell("rm " + " ".join(inst.p(s + ".ess") for s in ("Save1", "Save2", "Save3")))


# ================================================================ kept forever, and restored as a pair


def test_save_snapshots_are_never_pruned(inst):
    keep(inst, "Save1")
    store = inst.store().root
    later = recovery.time.time() + 3650 * recovery.DAY
    recovery.prune(store, later, inst.audit)
    assert len(inst.saved()) == 2
    assert all((store / "blobs" / e["sha256"]).is_file() for e in inst.saved())


def test_an_old_config_snapshot_is_still_pruned(inst):
    """Only saves are kept forever: the config pruning still runs on the same store."""
    store = recovery.store_dir(str(inst.game))
    (store / "blobs").mkdir(parents=True)
    old = recovery._line("20200101_000000", 1, "c:/x/Skyrim.ini", "a" * 64, 1, "config", "20200101_000000-config",
                         "x", "", "", 1, ["c:/x/Skyrim.ini"])
    new = dict(old, snap="20200102_000000-config", ts="20200102_000000", t=2, dest="b" * 64, sha256="b" * 64)
    (store / recovery.MANIFEST).write_text(json.dumps(old) + "\n" + json.dumps(new) + "\n", encoding="ascii")
    recovery.prune(store, 10 ** 10, inst.audit)
    assert [e["snap"] for e in recovery.read_manifest(store)[0]] == ["20200102_000000-config"]


def _snap(inst):
    return inst.saved()[0]["snap"]


def test_a_restore_puts_both_files_of_the_pair_back_verified(inst, tmp_path):
    keep(inst, "Save1")
    sid = _snap(inst)
    (inst.saves / "Save1.ess").unlink()
    (inst.saves / "Save1.skse").write_bytes(b"changed")
    out = tmp_path / "restored"
    import io
    buf = io.StringIO()
    assert snapcli.cmd_restore(str(inst.game), sid, [], str(out), False, buf) == 0
    got = sorted(p.name for p in out.rglob("*") if p.is_file())
    assert got == ["Save1.ess", "Save1.skse"]
    assert next(out.rglob("Save1.ess")).read_bytes() == b"Save1" * 100
    assert next(out.rglob("Save1.skse")).read_bytes() == b"Save1" * 7


def test_a_restore_refuses_a_damaged_backup_and_writes_nothing(inst, tmp_path):
    keep(inst, "Save1")
    sid, sha = _snap(inst), inst.saved()[0]["sha256"]
    (inst.store().root / "blobs" / sha).write_bytes(b"rot")
    import io
    out = tmp_path / "restored"
    assert snapcli.cmd_restore(str(inst.game), sid, [], str(out), False, io.StringIO()) == 2
    assert not out.exists()


def test_a_restore_that_does_not_verify_at_its_target_writes_nothing(inst, tmp_path, monkeypatch):
    keep(inst, "Save1")
    store, sha = inst.store().root, inst.saved()[0]["sha256"]
    real = backup._sha256
    monkeypatch.setattr(backup, "_sha256", lambda p: "0" * 64 if ".restore-part-" in str(p) else real(p))
    target = tmp_path / "t" / "Save1.ess"
    with pytest.raises(OSError):
        snapshot.write_blobs(store, [(target, sha)])
    assert not target.exists()


def test_a_restore_of_a_pair_without_its_cosave_says_so(inst, tmp_path):
    keep(inst, "Single")
    import io
    buf = io.StringIO()
    assert snapcli.cmd_restore(str(inst.game), _snap(inst), [], str(tmp_path / "r"), False, buf) == 0
    assert "Single.skse did not exist when this save was backed up" in buf.getvalue()


def test_the_listing_names_each_pair_and_a_missing_cosave(inst):
    keep(inst, "Single", "Save2")
    import io
    buf = io.StringIO()
    snapcli.cmd_list(str(inst.game), "save", buf)
    text = buf.getvalue()
    assert "Save2.ess + Save2.skse" in text and "Single.ess, no Single.skse" in text and "kept forever" in text


# ================================================================ the hooks take no save backup (4.1: the save vault)


def test_the_snapshot_hook_no_longer_copies_saves(inst):
    """No hook backs a save up; the snapshot hook copies the load order and INIs only."""
    recovery.run(str(inst.game), "rm " + inst.p("Save1.ess"), False, inst.audit, inst.roots, env={})
    assert inst.saved() == []


# ================================================================ the corpus rows, in process (mutations name them)


def _rec():
    import importlib.util
    spec = importlib.util.spec_from_file_location("record", REPO / "scripts" / "guard-corpus" / "record.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _sp_rows():
    text = (REPO / "tests" / "guard_corpus" / "intended.jsonl").read_text(encoding="utf-8")
    return [r for r in (json.loads(x) for x in text.split("\n") if x.strip()) if r["task"] == "SAVE-POLICY"]


SP_ROWS = _sp_rows()


@pytest.fixture(scope="module")
def sp_installs(tmp_path_factory):
    rec = _rec()
    base = tmp_path_factory.mktemp("sp")
    return rec, {n: rec.make_install(base, REPO / ".claude" / "hooks", n) for n in {r["layout"] for r in SP_ROWS}}


def test_there_are_save_policy_rows():
    assert len(SP_ROWS) >= 25
    assert {r["decision"] for r in SP_ROWS} == {"allow", "deny"}


@pytest.mark.parametrize("row", SP_ROWS, ids=[r["id"] for r in SP_ROWS])
def test_sp_row(sp_installs, row):
    rec, inst = sp_installs
    install = inst[row["layout"]]
    if rec.windows_only(row["id"]):
        pytest.skip(rec.windows_only(row["id"]))
    got = rec.engine_verdict(install, row["hook"], rec.row_payload(row, install))
    assert rec.intended_problems(row, got) == []


# ================================================================ a save change beside another rule's ASK (L2 HIGH-1)
# Through run.py in process (the real Claude hook entry): another rule's ask must never skip the save policy. More than
# 2 saves with no typed approval: the S-SAVE-APPROVAL refusal wins over the ask. 1 or 2 saves: the pairs are backed up
# and verified BEFORE the ask reaches the user, and a failed backup refuses.


def _ask_beside(tmp_path, command_of, transcript=""):
    rec = _rec()
    install = rec.make_install(tmp_path, REPO / ".claude" / "hooks", "saves-bulk")
    S = (install / "cfg" / "Saves").as_posix()
    payload = {"tool_name": "Bash", "session_id": "corpus-save", "cwd": install.as_posix(),
               "tool_input": {"command": command_of(S, install.as_posix())}}
    if transcript:
        payload["transcript_path"] = str(install / "transcripts" / transcript)
    return install, rec.fire_inprocess(install, "protect-bash.sh", payload)


def _rm(S, *stems):
    return "rm " + " ".join('"{}/{}.ess"'.format(S, s) for s in stems)


ASKERS = {
    "opaque": lambda S, I: '$R "{}/Save1.ess"; '.format(S),
    "git-discard": lambda S, I: "git checkout -- Data/Plugin.esp ; ",
}


# ---- restored in 4.1 (kept behaviour; the reads of the retired save plan are taken out)
@pytest.mark.parametrize("cmd", ["cp {S1} /tmp/out/", "sha256sum {S1} {S2} {S3}", "cat {S1}", "ls -la {dir}",
                                 "Get-FileHash {S1}", "Copy-Item {S1} C:/Temp/out.ess"],
                         ids=["copy-out", "hash", "read", "list", "ps-hash", "ps-copy-out"])
def test_reading_hashing_listing_and_copying_out_are_free(inst, cmd):
    text = cmd.format(S1=inst.p("Save1.ess"), S2=inst.p("Save2.ess"), S3=inst.p("Save3.ess"), dir='"' + inst.S + '"')
    v = inst.shell(text, shell="powershell" if cmd[0].isupper() else "bash")
    assert (v.decision, v.reason, v.context) == ("allow", "", "")


def test_an_existing_save_outside_the_settings_folder_is_free(inst, tmp_path):
    """Only saves in the settings folder are judged (writes.is_save): a copy elsewhere is the user's own scratch."""
    (tmp_path / "work").mkdir()
    (tmp_path / "work" / "Out.ess").write_bytes(b"a scratch copy")
    v = inst.shell('cp /tmp/x "{}"'.format((tmp_path / "work" / "Out.ess").as_posix()))
    assert (v.decision, v.context) == ("allow", "")
    w = write_rules.decide((tmp_path / "work" / "Out.ess").as_posix(), inst.audit, roots=inst.roots)
    assert (w.decision, w.context) == ("allow", "")


@pytest.mark.parametrize("name,want", [("Save1.skse", "allow"), ("Save1.ess", "allow"), ("notes.txt", "deny")],
                         ids=["cosave", "save", "other-file"])
def test_sed_on_a_save_is_no_crlf_refusal(inst, name, want):
    """A save (either member of the pair) is no rule's business since 4.1; another file in Saves still gets the
    S-SED-CRLF refusal. So SAVE_EXTS still decides something: dropping `.skse` from it refuses the co-save."""
    v = inst.shell("sed -i 's/a/b/' " + inst.p(name))
    assert v.decision == want, v

