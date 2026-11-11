"""Save vault (tools/save_vault.py): every settled version of every save pair is kept, verified, forever.

Temp folders only: every Vault here is built from explicit folders, so no test reads the real profile's saves.
The game is simulated by writing X.ess.tmp and replacing X.ess, the order the vault must not disturb.
Twins: the `sv-*` block in tests/test_mutations.py, one per guarded clause.
"""
from __future__ import annotations

import ast
import json
import os
import sys
import time
from pathlib import Path

import pytest

from conftest import REPO

sys.path.insert(0, str(REPO / "tools"))
import save_vault  # noqa: E402
from save_vault import EXTS, PAIR_GRACE_S, SETTLE_S, Vault  # noqa: E402

WINDOWS = os.name == "nt"


class Clock:
    def __init__(self):
        self.t = time.time() + 10_000          # well after any file this test writes

    def __call__(self):
        return self.t

    def tick(self, s):
        self.t += s


def write_pair(folder, stem, ess=b"E" * 300, skse=b"S" * 50, age=None):
    paths = []
    for ext, data in ((".ess", ess), (".skse", skse)):
        if data is None:
            continue
        p = folder / (stem + ext)
        p.write_bytes(data)
        if age is not None:
            t = time.time() - age
            os.utime(p, (t, t))
        paths.append(p)
    return paths


def game_save(folder, stem, ess, skse=b"S2" * 25):
    """The game's order: write the temporary file, then replace the save with it."""
    for ext, data in ((".ess", ess), (".skse", skse)):
        tmp = folder / (stem + ext + ".tmp")
        tmp.write_bytes(data)
        os.replace(tmp, folder / (stem + ext))


@pytest.fixture
def bed(tmp_path):
    saves = tmp_path / "My Games" / "Saves"
    saves.mkdir(parents=True)
    clock = Clock()
    state = {"busy": False}
    vault = Vault(tmp_path / "vault", [saves], is_busy=lambda: state["busy"], clock=clock)
    return {"saves": saves, "vault": vault, "clock": clock, "state": state, "root": tmp_path / "vault"}


def versions(vault, stem):
    return vault.versions(stem)


def test_a_settled_pair_is_kept_with_both_members(bed):
    write_pair(bed["saves"], "Save1", age=3600)
    out = bed["vault"].sweep()
    assert len(out["captured"]) == 1
    e = out["captured"][0]
    assert e["ess"]["size"] == 300 and e["skse"]["size"] == 50 and e["note"] == ""
    assert bed["vault"].verify()["bad"] == []


def test_one_version_holds_both_members(bed):
    write_pair(bed["saves"], "Save1", age=3600)
    bed["vault"].sweep()
    [e] = versions(bed["vault"], "Save1")
    tx = bed["vault"].store.inspect(e["transaction"])
    assert sorted(Path(r["path"]).suffix for r in tx["groups"][0]["members"]) == [".ess", ".skse"]


def test_a_pair_still_being_written_is_not_captured(bed):
    write_pair(bed["saves"], "Save1", age=3600)
    v = bed["vault"]
    v.sweep()                                               # first version kept
    p = bed["saves"] / "Save1.ess"
    p.write_bytes(b"F" * 300)
    t = bed["clock"]() - 1                                  # just written, by the vault's clock
    os.utime(p, (t, t))
    assert v.sweep()["captured"] == []
    bed["clock"].tick(SETTLE_S + 1)
    assert len(v.sweep()["captured"]) == 1


def test_tmp_files_are_never_captured(bed):
    write_pair(bed["saves"], "Save1", age=3600)
    (bed["saves"] / "Save1.ess.tmp").write_bytes(b"partial")
    (bed["saves"] / "Other.ess.tmp").write_bytes(b"partial")
    out = bed["vault"].sweep()
    assert out["captured"] == []                            # Save1 is mid-write; Other.ess.tmp is no save
    assert not any(e["stem"].lower().startswith("other") for e in bed["vault"].events())


def test_unchanged_pair_is_not_kept_twice(bed):
    write_pair(bed["saves"], "Save1", age=3600)
    bed["vault"].sweep()
    bed["clock"].tick(100)
    assert bed["vault"].sweep()["captured"] == []
    assert len(versions(bed["vault"], "Save1")) == 1


def test_every_version_of_an_overwritten_slot_is_kept(bed):
    s, v, c = bed["saves"], bed["vault"], bed["clock"]
    write_pair(s, "Quicksave0", ess=b"1" * 300, age=3600)
    v.sweep()
    for n in (b"2", b"3"):
        game_save(s, "Quicksave0", n * 300)
        for p in s.glob("Quicksave0.*"):
            os.utime(p, (c() - 100, c() - 100))
        v.sweep()                                           # sees the change; its settle clock starts
        c.tick(SETTLE_S + 1)
        v.sweep()
    kept = versions(v, "Quicksave0")
    assert len(kept) == 3
    assert len({e["ess"]["sha256"] for e in kept}) == 3


def test_a_missing_cosave_is_recorded_not_an_error(bed):
    write_pair(bed["saves"], "Save2", skse=None, age=3600)
    out = bed["vault"].sweep()
    assert out["errors"] == [] and len(out["captured"]) == 1
    assert out["captured"][0]["skse"] is None and out["captured"][0]["note"] == "co-save missing"


def test_a_late_cosave_joins_its_pair(bed):
    s, v, c = bed["saves"], bed["vault"], bed["clock"]
    p = s / "Save3.ess"
    p.write_bytes(b"E" * 300)
    os.utime(p, (c() - (SETTLE_S + 5), c() - (SETTLE_S + 5)))     # settled, but younger than the partner grace
    assert v.sweep()["captured"] == []
    q = s / "Save3.skse"
    q.write_bytes(b"S" * 50)
    os.utime(q, (c() - (SETTLE_S + 5), c() - (SETTLE_S + 5)))
    v.sweep()                                               # sees the partner arrive
    c.tick(SETTLE_S + 1)
    [e] = v.sweep()["captured"]
    assert e["skse"] is not None and e["ess"] is not None


def test_one_failed_listing_is_not_a_vanish(bed):
    s, v, c = bed["saves"], bed["vault"], bed["clock"]
    paths = write_pair(s, "Save4", age=3600)
    v.sweep()
    for p in paths:
        p.unlink()
    c.tick(15)
    assert v.sweep()["vanished"] == []
    c.tick(15)
    [e] = v.sweep()["vanished"]
    assert e["stem"] == "Save4" and e["last"] == versions(v, "Save4")[-1]["id"]


def test_a_removed_pair_is_recorded_vanished_once(bed):
    s, v, c = bed["saves"], bed["vault"], bed["clock"]
    for p in write_pair(s, "Save5", age=3600):
        pass
    v.sweep()
    for p in s.glob("Save5.*"):
        p.unlink()
    for _ in range(5):
        c.tick(15)
        v.sweep()
    assert sum(1 for e in v.events() if e.get("event") == "vanished") == 1


def test_unreadable_folder_is_not_vanished(bed, tmp_path):
    s, c = bed["saves"], bed["clock"]
    write_pair(s, "Save6", age=3600)
    bed["vault"].sweep()
    moved = tmp_path / "elsewhere"
    s.rename(moved)                                         # the folder itself is gone (drive unplugged, renamed)
    for _ in range(4):
        c.tick(15)
        bed["vault"].sweep()
    assert not any(e.get("event") == "vanished" for e in bed["vault"].events())


def test_the_game_can_replace_a_save_while_it_is_being_kept(bed, monkeypatch):
    """The capture holds no handle on the live save: the game's tmp->replace succeeds mid-capture, and the kept
    version is the one that had settled, byte for byte."""
    s, v = bed["saves"], bed["vault"]
    write_pair(s, "Quicksave1", ess=b"OLD" * 100, age=3600)
    real = save_vault.durable.digest
    fired = {}

    def digest_then_game_saves(path, deadline=None):
        if "staging-links" in str(path) and not fired:
            fired["x"] = True
            game_save(s, "Quicksave1", b"NEW" * 100)        # raises if the vault held the live file open
        return real(path, deadline)

    monkeypatch.setattr(save_vault.durable, "digest", digest_then_game_saves)
    [e] = v.sweep()["captured"]
    assert fired and (s / "Quicksave1.ess").read_bytes() == b"NEW" * 100
    blob = v.store.root / "blobs" / e["ess"]["sha256"]
    assert blob.read_bytes() == b"OLD" * 100


def test_a_save_during_the_stores_own_copy_still_keeps_the_settled_version(bed, monkeypatch):
    """The game's replace changes the link's change time mid-copy; the store's recheck fails once and the retry, on
    the SAME links, keeps the settled bytes (re-linking would take the next version and lose this one)."""
    s, v = bed["saves"], bed["vault"]
    write_pair(s, "Quicksave2", ess=b"OLD" * 100, age=3600)
    real = save_vault.durable.copy_verified
    fired = {}

    def copy_then_game_saves(source, target, deadline=None):
        info = real(source, target, deadline)
        if "staging-links" in str(source) and "store" in str(target) and not fired:
            fired["x"] = True
            game_save(s, "Quicksave2", b"NEW" * 100)
        return info

    monkeypatch.setattr(save_vault.durable, "copy_verified", copy_then_game_saves)
    [e] = v.sweep()["captured"]
    assert fired
    assert (v.store.root / "blobs" / e["ess"]["sha256"]).read_bytes() == b"OLD" * 100


def test_the_capture_never_reads_a_live_save(bed, monkeypatch):
    """Every read the capture makes is of its own staging link, never of the file in Saves (an open live save makes
    the game's replace fail, measured 2026-11-10)."""
    s, v = bed["saves"], bed["vault"]
    write_pair(s, "Save40", age=3600)
    read = []
    for name in ("digest", "copy_verified"):
        real = getattr(save_vault.durable, name)

        def spy(path, *a, _real=real, **k):
            read.append(str(path))
            return _real(path, *a, **k)
        monkeypatch.setattr(save_vault.durable, name, spy)
    assert len(v.sweep()["captured"]) == 1
    live = os.path.normcase(str(s))
    assert read and not [p for p in read if os.path.normcase(p).startswith(live)]


def test_staging_links_are_removed(bed):
    write_pair(bed["saves"], "Save7", age=3600)
    bed["vault"].sweep()
    assert list((bed["root"] / "staging-links").iterdir()) == []


def test_events_survive_a_second_capture(bed):
    s, v, c = bed["saves"], bed["vault"], bed["clock"]
    write_pair(s, "Save8", age=3600)
    v.sweep()
    first = [e["id"] for e in v.events()]
    game_save(s, "Save8", b"Z" * 300)
    for p in s.glob("Save8.*"):
        os.utime(p, (c() - 100, c() - 100))
    v.sweep()
    c.tick(SETTLE_S + 1)
    v.sweep()
    ids = [e["id"] for e in v.events()]
    assert ids[:len(first)] == first and len(ids) == len(first) + 1


def test_a_lost_state_file_costs_no_recapture(bed):
    write_pair(bed["saves"], "Save9", age=3600)
    bed["vault"].sweep()
    (bed["root"] / "status" / "state.json").unlink()
    again = Vault(bed["root"], [bed["saves"]], is_busy=lambda: False, clock=bed["clock"])
    assert again.sweep()["captured"] == []


def test_only_one_pair_per_pass_while_the_game_runs(bed):
    for n in range(4):
        write_pair(bed["saves"], "Save%d" % (20 + n), ess=bytes([65 + n]) * 300, age=3600)
    bed["state"]["busy"] = True
    out = bed["vault"].sweep()
    assert len(out["captured"]) == 1 and out["pending"] == 3
    bed["state"]["busy"] = False
    assert len(bed["vault"].sweep()["captured"]) == 3


def test_busy_counts_unknown_as_running(monkeypatch):
    def boom(*a, **k):
        raise OSError("no tasklist")
    monkeypatch.setattr(save_vault.subprocess, "run", boom)
    assert save_vault.busy() is True


def test_restore_without_apply_writes_nothing(bed):
    s, v = bed["saves"], bed["vault"]
    write_pair(s, "Save10", age=3600)
    v.sweep()
    for p in s.glob("Save10.*"):
        p.unlink()
    before = sorted(os.listdir(s))
    plan = v.restore("Save10")
    assert plan["mode"] == "put-back" and plan["apply"] is False and sorted(os.listdir(s)) == before


def test_a_vanished_pair_is_put_back_byte_for_byte(bed):
    s, v = bed["saves"], bed["vault"]
    write_pair(s, "Save11", ess=b"Q" * 300, skse=b"R" * 50, age=3600)
    v.sweep()
    for p in s.glob("Save11.*"):
        p.unlink()
    v.restore("Save11", apply=True)
    assert (s / "Save11.ess").read_bytes() == b"Q" * 300 and (s / "Save11.skse").read_bytes() == b"R" * 50


def test_a_present_pair_is_never_overwritten_without_replace(bed):
    write_pair(bed["saves"], "Save12", age=3600)
    bed["vault"].sweep()
    with pytest.raises(FileExistsError):
        bed["vault"].restore("Save12", apply=True)


def test_replace_vaults_the_current_pair_first(bed):
    s, v, c = bed["saves"], bed["vault"], bed["clock"]
    write_pair(s, "Save13", ess=b"A" * 300, age=3600)
    v.sweep()
    old = versions(v, "Save13")[-1]["id"]
    (s / "Save13.ess").write_bytes(b"B" * 300)               # changed, not yet seen by any sweep
    v.restore("Save13", event_id=old, replace=True, apply=True)
    assert (s / "Save13.ess").read_bytes() == b"A" * 300
    assert any(e["ess"]["size"] == 300 and e["id"] != old for e in versions(v, "Save13"))
    blobs = {e["ess"]["sha256"] for e in versions(v, "Save13")}
    import hashlib
    assert hashlib.sha256(b"B" * 300).hexdigest() in blobs   # the overwritten version was kept first


def test_restore_into_the_live_folder_is_refused_while_the_game_runs(bed):
    s, v = bed["saves"], bed["vault"]
    write_pair(s, "Save14", age=3600)
    v.sweep()
    for p in s.glob("Save14.*"):
        p.unlink()
    bed["state"]["busy"] = True
    with pytest.raises(save_vault.PreservationFailed):
        v.restore("Save14", apply=True)


def test_restore_as_a_new_name_never_clobbers(bed):
    s, v = bed["saves"], bed["vault"]
    write_pair(s, "Save15", age=3600)
    v.sweep()
    v.restore("Save15", as_stem="Save15-restored", apply=True)
    assert (s / "Save15-restored.ess").exists()
    with pytest.raises(FileExistsError):
        v.restore("Save15", as_stem="Save15-restored", apply=True)


def test_a_blob_that_does_not_verify_is_named(bed):
    write_pair(bed["saves"], "Save16", age=3600)
    v = bed["vault"]
    v.sweep()
    [e] = versions(v, "Save16")
    blob = v.store.root / "blobs" / e["ess"]["sha256"]
    os.chmod(blob, 0o666)
    blob.write_bytes(b"corrupt")
    out = v.verify()
    assert [tx for tx, _ in out["bad"]] == [e["transaction"]]


def test_an_existing_blob_is_never_replaced(bed):
    s, v, c = bed["saves"], bed["vault"], bed["clock"]
    write_pair(s, "Save17", ess=b"K" * 300, age=3600)
    write_pair(s, "Save18", ess=b"K" * 300, age=3600)        # identical bytes: one blob
    v.sweep()
    blobs = list((v.store.root / "blobs").iterdir())
    stamp = {b.name: b.stat().st_mtime_ns for b in blobs}
    assert len(blobs) == 2                                   # one shared .ess blob + one shared .skse blob
    c.tick(1)
    write_pair(s, "Save19", ess=b"K" * 300, age=3600)
    v.sweep()
    assert {b.name: b.stat().st_mtime_ns for b in (v.store.root / "blobs").iterdir()} == stamp


def test_legacy_import_is_idempotent_and_skips_empty_records(bed, tmp_path):
    old = tmp_path / "legacy"
    old.mkdir()
    (old / "Save1.ess").write_bytes(b"L" * 300)
    import hashlib
    sha = hashlib.sha256(b"L" * 300).hexdigest()
    orig = str(bed["saves"] / "Save1.ess")
    recs = [{"id": "20261013-000000-aaaaaa-0", "time": "2026-10-13T00:00:00", "originals": [orig, orig[:-4] + ".skse"],
             "ess": {"original": orig, "backup": str(old / "Save1.ess"), "sha256": sha, "size": 300},
             "note": "co-save missing"},
            {"id": "20261013-000001-bbbbbb-0", "originals": [orig], "note": "no file existed (nothing to back up)"}]
    m = tmp_path / "manifest.jsonl"
    m.write_text("\n".join(json.dumps(r) for r in recs) + "\n", encoding="utf-8")
    first = bed["vault"].import_legacy(m)
    assert (first["imported"], first["skipped"], first["failed"]) == (1, 1, [])
    assert bed["vault"].import_legacy(m) == {"imported": 0, "skipped": 0, "failed": []}
    assert (old / "Save1.ess").read_bytes() == b"L" * 300


def test_legacy_hash_mismatch_is_refused(bed, tmp_path):
    old = tmp_path / "legacy"
    old.mkdir()
    (old / "Save1.ess").write_bytes(b"L" * 300)
    orig = str(bed["saves"] / "Save1.ess")
    rec = {"id": "x-0", "originals": [orig], "ess": {"original": orig, "backup": str(old / "Save1.ess"),
                                                       "sha256": "0" * 64, "size": 300}}
    m = tmp_path / "manifest.jsonl"
    m.write_text(json.dumps(rec) + "\n", encoding="utf-8")
    out = bed["vault"].import_legacy(m)
    assert out["imported"] == 0 and len(out["failed"]) == 1


def test_stale_heartbeat_reports_watcher_down(bed):
    write_pair(bed["saves"], "Save30", age=3600)
    bed["vault"].sweep()
    beat = json.loads((bed["root"] / "status" / "heartbeat.json").read_text())
    alive, _ = save_vault.watcher_status(bed["root"], now=beat["last_sweep"] + 10)
    dead, line = save_vault.watcher_status(bed["root"], now=beat["last_sweep"] + save_vault.STALE_S + 1)
    assert alive is True and dead is False and "NOT RUNNING" in line
    missing, _ = save_vault.watcher_status(bed["root"].parent / "nowhere")
    assert missing is False


def test_a_second_watcher_exits(bed):
    root = bed["root"]
    from modding_harness.locking import exclusive
    with exclusive(root / "status" / "watch.lock", 0):
        assert save_vault.watch(lambda: bed["vault"], stop=lambda: True) == 0


def test_the_vault_never_deletes_what_it_stored():
    """Outside its own staging links, save_vault.py has no unlink/rmtree/remove: nothing it stored can go."""
    tree = ast.parse((REPO / "tools" / "save_vault.py").read_text(encoding="utf-8"))
    offenders = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in (
                "unlink", "rmtree", "remove", "rmdir", "removedirs"):
            offenders.append(ast.unparse(node))
    allowed = {"link.unlink()", "part.unlink(missing_ok=True)"}
    assert set(offenders) <= allowed, offenders


def test_the_vault_store_must_be_outside_the_save_folders(tmp_path):
    saves = tmp_path / "Saves"
    saves.mkdir()
    with pytest.raises(ValueError):
        Vault(saves / "vault", [saves], is_busy=lambda: False)


def test_discovery_reads_save_dirs_and_the_override(tmp_path):
    a, b = tmp_path / "A", tmp_path / "B"
    a.mkdir()
    b.mkdir()
    assert save_vault.discover(tmp_path, env={"SAVE_VAULT_FOLDERS": str(a) + os.pathsep + str(b)}) == [a, b]
    found = save_vault.discover(tmp_path, env={"SKYRIM_SAVE_DIRS": str(a), "SKYGUARD_NO_KNOWN_FOLDERS": "1",
                                               "USERPROFILE": str(tmp_path / "nohome"), "HOME": str(tmp_path / "nohome")})
    assert a in found and b not in found


def test_a_marked_folder_is_watched(tmp_path):
    kept = tmp_path / "_CLAUDE_OUTPUT" / "baseline"
    kept.mkdir(parents=True)
    (kept / ".protect-saves").write_text("")
    found = save_vault.discover(tmp_path, env={"SKYGUARD_NO_KNOWN_FOLDERS": "1", "USERPROFILE": str(tmp_path / "x"),
                                               "HOME": str(tmp_path / "x")})
    assert kept in found
