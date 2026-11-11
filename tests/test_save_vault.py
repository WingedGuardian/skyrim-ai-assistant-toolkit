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
    vault = Vault(tmp_path / "vault", [saves], is_busy=lambda: state["busy"], clock=clock, reserve_gb=0)
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
    for name in ("Save1.ess.tmp", "Other.ess.tmp"):
        (bed["saves"] / name).write_bytes(b"partial")
        os.utime(bed["saves"] / name, (bed["clock"]() - 1, bed["clock"]() - 1))      # being written now
    pairs, _, _ = bed["vault"].scan()
    key = next(k for k, p in pairs.items() if p["stem"] == "Save1")
    assert not bed["vault"].due(key, pairs[key], bed["clock"](), bed["vault"].kept())   # never queued
    out = bed["vault"].sweep()
    assert out["captured"] == []                            # Save1 is mid-write; Other.ess.tmp is no save
    assert not any(e["stem"].lower().startswith("other") for e in bed["vault"].events())


def test_unchanged_pair_is_not_kept_twice(bed):
    write_pair(bed["saves"], "Save1", age=3600)
    bed["vault"].sweep()
    bed["clock"].tick(100)
    assert bed["vault"].sweep()["captured"] == []
    assert len(versions(bed["vault"], "Save1")) == 1
    pairs, _, _ = bed["vault"].scan()
    key = next(iter(pairs))
    assert not bed["vault"].due(key, pairs[key], bed["clock"](), bed["vault"].kept())   # skipped before any link


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


def test_the_game_can_replace_a_save_while_the_vault_reads_it(bed, monkeypatch):
    """The vault reads only its own staging link: with that link held OPEN, the game's tmp->replace of the live save
    succeeds, and the kept version is the one that had settled, byte for byte."""
    s, v = bed["saves"], bed["vault"]
    write_pair(s, "Quicksave1", ess=b"OLD" * 100, age=3600)
    real = save_vault.durable.digest
    fired = {}

    def digest_while_holding_the_link_open(path, deadline=None):
        if "staging-links" in str(path) and str(path).lower().endswith(".ess") and not fired:
            with open(path, "rb") as held:
                held.read(10)
                game_save(s, "Quicksave1", b"NEW" * 100)    # raises if a handle blocked the replace
                fired["x"] = True
        return real(path, deadline)

    monkeypatch.setattr(save_vault.durable, "digest", digest_while_holding_the_link_open)
    [e] = v.sweep()["captured"]
    assert fired and (s / "Quicksave1.ess").read_bytes() == b"NEW" * 100
    blob = v.store.root / "blobs" / e["ess"]["sha256"]
    assert blob.read_bytes() == b"OLD" * 100


def test_a_save_during_the_stores_own_copy_still_keeps_the_settled_version(bed, monkeypatch):
    """On POSIX the game's replace moves the link's change time mid-copy; the store's recheck fails once and the
    retry, on the SAME links, keeps the settled bytes (re-linking would take the next version and lose this one).
    On Windows st_ctime is the creation time, so the recheck passes and only the outcome is checked."""
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
    again = Vault(bed["root"], [bed["saves"]], is_busy=lambda: False, clock=bed["clock"], reserve_gb=0)
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
        Vault(saves / "vault", [saves], is_busy=lambda: False, reserve_gb=0)


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


# ---- review 2026-11-10 (BLOCK on d4cdc0e): one test per finding

def test_a_changed_pair_waits_one_settle_period(bed):
    s, v, c = bed["saves"], bed["vault"], bed["clock"]
    write_pair(s, "Save50", ess=b"A" * 300, age=3600)
    v.sweep()
    (s / "Save50.ess").write_bytes(b"B" * 300)
    os.utime(s / "Save50.ess", (c() - 3600, c() - 3600))       # an old mtime: only the settle clock can hold it
    assert v.sweep()["captured"] == []
    c.tick(SETTLE_S + 1)
    assert len(v.sweep()["captured"]) == 1


def test_a_young_file_waits_even_on_first_sight(bed):
    s, v, c = bed["saves"], bed["vault"], bed["clock"]
    for p in write_pair(s, "Save51"):
        os.utime(p, (c() - 2, c() - 2))
    assert v.sweep()["captured"] == []


def test_a_locked_save_is_kept_by_copy(bed, monkeypatch):
    """os.link fails on a locked save (deny WA); the copy's own mtime is new, so the live file's stat decides."""
    s, v = bed["saves"], bed["vault"]
    write_pair(s, "Save52", age=3600)

    def no_link(*a, **k):
        raise PermissionError(5, "Access is denied")
    monkeypatch.setattr(save_vault.os, "link", no_link)
    [e] = v.sweep()["captured"]
    assert e["ess"]["size"] == 300 and list((bed["root"] / "staging-links").iterdir()) == []


def test_another_volume_waits_while_the_game_runs(bed, monkeypatch):
    s, v = bed["saves"], bed["vault"]
    write_pair(s, "Save53", age=3600)

    def no_link(*a, **k):
        raise OSError(17, "cross-device")
    monkeypatch.setattr(save_vault.os, "link", no_link)
    monkeypatch.setattr(Vault, "_same_volume", lambda self, path: False)
    bed["state"]["busy"] = True
    out = v.sweep()
    assert out["captured"] == [] and out["deferred"] == 1 and out["errors"] == []
    bed["state"]["busy"] = False
    assert len(v.sweep()["captured"]) == 1


def test_one_pair_that_cannot_be_kept_does_not_starve_the_rest(bed, monkeypatch):
    s, v, c = bed["saves"], bed["vault"], bed["clock"]
    write_pair(s, "Old", ess=b"O" * 300, age=7200)
    write_pair(s, "New", ess=b"N" * 300, age=3600)              # newest first in the queue
    real = Vault.capture

    def capture(self, key, pair):
        return None if pair["stem"] == "New" else real(self, key, pair)
    monkeypatch.setattr(Vault, "capture", capture)
    bed["state"]["busy"] = True
    out = v.sweep()
    assert [e["stem"] for e in out["captured"]] == ["Old"]
    for _ in range(save_vault.FAIL_REPORT):
        c.tick(15)
        v.sweep()
    alive, line = save_vault.watcher_status(bed["root"], now=c())
    assert alive is False and "New" in line                      # a pair that keeps failing is reported


def test_a_leftover_tmp_does_not_block_its_pair_forever(bed):
    s, v, c = bed["saves"], bed["vault"], bed["clock"]
    write_pair(s, "Save54", age=3600)
    tmp = s / "Save54.ess.tmp"
    tmp.write_bytes(b"crashed save")
    os.utime(tmp, (c() - save_vault.TMP_LIVE_S - 60, c() - save_vault.TMP_LIVE_S - 60))
    out = v.sweep()
    assert len(out["captured"]) == 1 and out["stale_tmp"] == [str(tmp)]


def _legacy(tmp_path, saves, stem, data, rid):
    import hashlib
    old = tmp_path / ("legacy-" + stem)
    old.mkdir()
    (old / (stem + ".ess")).write_bytes(data)
    orig = str(saves / (stem + ".ess"))
    rec = {"id": rid, "time": "2026-10-13T00:00:00", "originals": [orig, orig[:-4] + ".skse"],
           "ess": {"original": orig, "backup": str(old / (stem + ".ess")), "sha256": hashlib.sha256(data).hexdigest(),
                   "size": len(data)}, "note": "co-save missing"}
    m = tmp_path / ("manifest-" + stem + ".jsonl")
    m.write_text(json.dumps(rec) + "\n", encoding="utf-8")
    return m


def test_legacy_backups_can_be_listed_and_restored(bed, tmp_path):
    m = _legacy(tmp_path, bed["saves"], "Save55", b"L" * 300, "20261013-000000-cccccc-0")
    bed["vault"].import_legacy(m)
    [e] = bed["vault"].versions("Save55")
    assert e["legacy"] is True
    bed["vault"].restore("Save55", apply=True)
    assert (bed["saves"] / "Save55.ess").read_bytes() == b"L" * 300


def test_legacy_import_stays_idempotent_beside_a_running_watcher(bed, tmp_path):
    m = _legacy(tmp_path, bed["saves"], "Save56", b"W" * 300, "20261013-000000-dddddd-0")
    watcher = bed["vault"]                                       # holds its state in memory
    cli = Vault(bed["root"], [bed["saves"]], is_busy=lambda: False, clock=bed["clock"], reserve_gb=0)
    assert cli.import_legacy(m)["imported"] == 1
    write_pair(bed["saves"], "Save57", age=3600)
    watcher.sweep()                                              # writes its own (older) state
    again = Vault(bed["root"], [bed["saves"]], is_busy=lambda: False, clock=bed["clock"], reserve_gb=0)
    assert again.import_legacy(m)["imported"] == 0
    assert sum(1 for e in again.events() if e.get("event") == "import") == 1


def test_two_processes_keep_one_version_once(bed):
    write_pair(bed["saves"], "Save58", age=3600)
    other = Vault(bed["root"], [bed["saves"]], is_busy=lambda: False, clock=bed["clock"], reserve_gb=0)
    pairs, _, _ = other.scan()
    stale_view = other.kept()                                    # taken before the first process keeps it
    bed["vault"].sweep()
    key = next(iter(pairs))
    assert other.due(key, pairs[key], bed["clock"](), stale_view)
    assert other.capture(key, pairs[key]) is None               # the capture re-checks under the lock
    assert len(bed["vault"].versions("Save58")) == 1


def test_replace_keeps_the_current_pair_even_with_a_tmp_beside_it(bed):
    s, v, c = bed["saves"], bed["vault"], bed["clock"]
    write_pair(s, "Save59", ess=b"A" * 300, age=3600)
    v.sweep()
    old = v.versions("Save59")[-1]["id"]
    (s / "Save59.ess").write_bytes(b"C" * 300)
    os.utime(s / "Save59.ess", (c() - 3600, c() - 3600))
    (s / "Save59.ess.tmp").write_bytes(b"x")
    os.utime(s / "Save59.ess.tmp", (c() - 1, c() - 1))
    v.restore("Save59", event_id=old, replace=True, apply=True)
    import hashlib
    assert hashlib.sha256(b"C" * 300).hexdigest() in {e["ess"]["sha256"] for e in v.versions("Save59")}


def test_now_does_not_shorten_the_wait_while_the_game_runs(bed):
    s, v, c = bed["saves"], bed["vault"], bed["clock"]
    for p in write_pair(s, "Save60"):
        os.utime(p, (c() - 5, c() - 5))                          # 5 s old: --now alone would take it
    bed["state"]["busy"] = True
    assert v.sweep(force=True)["captured"] == []
    bed["state"]["busy"] = False
    assert len(v.sweep(force=True)["captured"]) == 1


def test_scanning_never_opens_a_live_save_by_name(bed, monkeypatch):
    s, v = bed["saves"], bed["vault"]
    write_pair(s, "Save61", age=3600)
    opened = []
    for name in ("stat", "lstat"):
        real = getattr(save_vault.os, name)

        def spy(path, *a, _real=real, **k):
            opened.append(str(path))
            return _real(path, *a, **k)
        monkeypatch.setattr(save_vault.os, name, spy)
    real_isfile = save_vault.os.path.isfile
    monkeypatch.setattr(save_vault.os.path, "isfile", lambda p: opened.append(str(p)) or real_isfile(p))
    assert len(v.sweep()["captured"]) == 1
    live = os.path.normcase(str(s))
    assert not [p for p in opened if os.path.normcase(p).startswith(live) and p.lower().endswith(EXTS)]


def test_a_vault_watching_nothing_is_not_reported_running(tmp_path):
    v = Vault(tmp_path / "vault", [], is_busy=lambda: False, reserve_gb=0)
    v.sweep()
    alive, line = save_vault.watcher_status(tmp_path / "vault")
    assert alive is False and "NO SAVE FOLDER" in line


def test_a_version_without_a_cosave_never_orphans_the_present_one(bed):
    s, v = bed["saves"], bed["vault"]
    write_pair(s, "Save62", skse=None, age=3600)
    v.sweep()
    (s / "Save62.skse").write_bytes(b"S" * 50)
    with pytest.raises(FileExistsError):
        v.restore("Save62", replace=True)                        # the dry run already says no


def test_restore_as_refuses_paths_and_a_running_game(bed):
    s, v = bed["saves"], bed["vault"]
    write_pair(s, "Save63", age=3600)
    v.sweep()
    with pytest.raises(ValueError):
        v.restore("Save63", as_stem="../escape", apply=True)
    bed["state"]["busy"] = True
    with pytest.raises(save_vault.PreservationFailed):
        v.restore("Save63", as_stem="Save63-b", apply=True)


def test_links_left_by_a_crash_are_cleared_at_watcher_start(bed):
    stray = bed["root"] / "staging-links" / "dead-Save64.ess"
    stray.write_bytes(b"x")
    bed["vault"].clean_staging()
    assert not stray.exists()


def test_a_lost_index_is_rebuilt_from_the_events(bed):
    write_pair(bed["saves"], "Save65", age=3600)
    bed["vault"].sweep()
    (bed["root"] / "status" / "kept.json").unlink()
    again = Vault(bed["root"], [bed["saves"]], is_busy=lambda: False, clock=bed["clock"], reserve_gb=0)
    assert again.sweep()["captured"] == [] and len(again.versions("Save65")) == 1


def test_now_still_waits_out_a_write_in_progress(bed):
    s, v, c = bed["saves"], bed["vault"], bed["clock"]
    for p in write_pair(s, "Save66"):
        os.utime(p, (c() - 0.5, c() - 0.5))
    assert v.sweep(force=True)["captured"] == []


def test_the_watcher_clears_stray_links_when_it_starts(bed):
    stray = bed["root"] / "staging-links" / "dead-Save67.ess"
    stray.write_bytes(b"x")
    save_vault.watch(lambda: bed["vault"], stop=lambda: True)
    assert not stray.exists()


def test_an_old_tmp_file_is_never_kept_as_a_save(bed):
    s, c = bed["saves"], bed["clock"]
    junk = s / "Other.ess.tmp"
    junk.write_bytes(b"left over")
    os.utime(junk, (c() - 3600, c() - 3600))
    bed["vault"].sweep()
    assert not [e for e in bed["vault"].events() if e.get("stem", "").lower().startswith("other")]


# ---- re-review 2026-11-10 (BLOCK on 0c7a8dd) and the INI vault

def test_the_capture_resolves_no_live_save_name(bed, monkeypatch):
    """The store is handed the staging link, so its path resolution (a handle on the name) never touches the save;
    the one remaining open of a live name is os.link, once per member."""
    import pathlib
    s, v = bed["saves"], bed["vault"]
    write_pair(s, "Save70", age=3600)
    resolved, linked = [], []
    real_resolve = pathlib.Path.resolve
    monkeypatch.setattr(pathlib.Path, "resolve", lambda self, *a, **k: resolved.append(str(self)) or real_resolve(self, *a, **k))
    real_link = save_vault.os.link
    monkeypatch.setattr(save_vault.os, "link", lambda src, dst: linked.append(str(src)) or real_link(src, dst))
    assert len(v.sweep()["captured"]) == 1
    live = [str(s / "Save70.ess"), str(s / "Save70.skse")]
    assert not [r for r in resolved if os.path.normcase(r) in map(os.path.normcase, live)]
    assert sorted(map(os.path.normcase, linked)) == sorted(map(os.path.normcase, live))


def test_a_pair_the_game_starts_saving_is_skipped_before_linking(bed, monkeypatch):
    s, v, c = bed["saves"], bed["vault"], bed["clock"]
    write_pair(s, "Save71", age=3600)
    pairs, _, _ = v.scan()
    tmp = s / "Save71.ess.tmp"
    tmp.write_bytes(b"writing")
    os.utime(tmp, (c() - 1, c() - 1))
    linked = []
    monkeypatch.setattr(save_vault.os, "link", lambda *a: linked.append(a))
    key = next(iter(pairs))
    assert v.capture(key, pairs[key]) is None and linked == []


def test_another_volume_is_copied_never_linked(bed, monkeypatch):
    s, v = bed["saves"], bed["vault"]
    write_pair(s, "Save72", age=3600)
    monkeypatch.setattr(Vault, "_same_volume", lambda self, folder: False)
    linked = []
    monkeypatch.setattr(save_vault.os, "link", lambda *a: linked.append(a))
    assert len(v.sweep()["captured"]) == 1 and linked == []


def test_deferred_pairs_do_not_starve_a_same_volume_folder(tmp_path):
    far, near = tmp_path / "Far" / "Saves", tmp_path / "Near" / "Saves"
    far.mkdir(parents=True)
    near.mkdir(parents=True)
    for n in range(save_vault.BUSY_TRIES + 1):
        write_pair(far, "Far%d" % n, ess=bytes([70 + n]) * 300, age=100 + n)      # newest: first in the queue
    write_pair(near, "Near", ess=b"N" * 300, age=7200)
    v = Vault(tmp_path / "vault", [far, near], is_busy=lambda: True, clock=Clock(), reserve_gb=0)
    v._same_volume = lambda folder: os.path.normcase(str(near)) == os.path.normcase(str(folder))
    out = v.sweep()
    assert [e["stem"] for e in out["captured"]] == ["Near"] and out["deferred"] == save_vault.BUSY_TRIES + 1


def test_a_failure_count_clears_once_the_pair_is_kept_elsewhere_or_gone(bed, monkeypatch):
    s, v, c = bed["saves"], bed["vault"], bed["clock"]
    write_pair(s, "Bad", ess=b"B" * 300, age=7200)
    write_pair(s, "Gone", ess=b"G" * 300, age=7200)
    real = Vault.capture
    monkeypatch.setattr(Vault, "capture", lambda self, key, pair, ignore_tmp=False: None)
    for _ in range(save_vault.FAIL_REPORT):
        c.tick(15)
        v.sweep()
    assert save_vault.watcher_status(bed["root"], now=c())[0] is False
    monkeypatch.setattr(Vault, "capture", real)
    other = Vault(bed["root"], [s], is_busy=lambda: False, clock=c, reserve_gb=0)
    other.sweep(only="Bad")                                      # kept by another process
    for p in s.glob("Gone.*"):
        p.unlink()                                               # and the other one is deleted
    monkeypatch.setattr(Vault, "capture", lambda self, key, pair, ignore_tmp=False: None)
    c.tick(15)
    v.sweep()
    assert save_vault.watcher_status(bed["root"], now=c())[0] is True


def test_an_unreadable_folder_is_reported(bed, tmp_path):
    other = tmp_path / "Other" / "Saves"
    other.mkdir(parents=True)
    v = Vault(tmp_path / "vault2", [bed["saves"], other], is_busy=lambda: False, clock=bed["clock"], reserve_gb=0)
    other.rmdir()
    v.sweep()
    alive, line = save_vault.watcher_status(tmp_path / "vault2", now=bed["clock"]())
    assert alive is False and "folder unreadable" in line


def test_restore_checks_the_game_before_looking_at_the_saves(bed, monkeypatch):
    s, v = bed["saves"], bed["vault"]
    write_pair(s, "Save73", age=3600)
    v.sweep()
    bed["state"]["busy"] = True

    def looked(*a, **k):
        raise AssertionError("listed the save folder before the game check")
    monkeypatch.setattr(Vault, "_listed", staticmethod(looked))
    with pytest.raises(save_vault.PreservationFailed):
        v.restore("Save73", replace=True, apply=True)


def test_replace_refuses_when_the_current_pair_could_not_be_kept(bed, monkeypatch):
    s, v, c = bed["saves"], bed["vault"], bed["clock"]
    write_pair(s, "Save74", ess=b"A" * 300, age=3600)
    v.sweep()
    old = v.versions("Save74")[-1]["id"]
    (s / "Save74.ess").write_bytes(b"D" * 300)
    os.utime(s / "Save74.ess", (c() - 3600, c() - 3600))
    monkeypatch.setattr(Vault, "capture", lambda self, key, pair, ignore_tmp=False: None)
    with pytest.raises(save_vault.PreservationFailed):
        v.restore("Save74", event_id=old, replace=True, apply=True)
    assert (s / "Save74.ess").read_bytes() == b"D" * 300           # nothing replaced


def test_copy_to_writes_plain_names(bed, tmp_path):
    s, v = bed["saves"], bed["vault"]
    write_pair(s, "Save75", ess=b"T" * 300, age=3600)
    v.sweep()
    out = tmp_path / "out"
    v.restore("Save75", to=str(out), apply=True)
    assert (out / "Save75.ess").read_bytes() == b"T" * 300 and (out / "Save75.skse").exists()


def test_legacy_import_waits_for_the_game(bed, tmp_path):
    m = tmp_path / "manifest.jsonl"
    m.write_text("", encoding="utf-8")
    bed["state"]["busy"] = True
    with pytest.raises(save_vault.PreservationFailed):
        bed["vault"].import_legacy(m)


def test_a_save_replaced_between_scan_and_link_is_not_kept_under_the_old_signature(bed):
    s, v, c = bed["saves"], bed["vault"], bed["clock"]
    write_pair(s, "Save76", ess=b"1" * 300, age=3600)
    pairs, _, _ = v.scan()
    key = next(iter(pairs))
    game_save(s, "Save76", b"2" * 300)                          # replaced after the listing; its .tmp is gone again
    assert v.capture(key, pairs[key]) is None
    assert v.versions("Save76") == []


def test_a_heartbeat_with_no_folders_is_not_ok(tmp_path):
    v = Vault(tmp_path / "vault", [], is_busy=lambda: False, reserve_gb=0)
    v.heartbeat(True, 0)
    assert json.loads((tmp_path / "vault" / "status" / "heartbeat.json").read_text())["ok"] is False


def test_busy_counts_an_empty_process_list_as_running(monkeypatch):
    class R:
        stdout = ""
    monkeypatch.setattr(save_vault.subprocess, "run", lambda *a, **k: R())
    assert save_vault.busy() is True


def test_the_logon_task_names_its_user_and_escapes_paths():
    xml = save_vault.task_xml("C:/P&Q/pythonw.exe", "C:/a<b>/save_vault.py", '--project "C:/x&y" ', "DOM\\me")
    assert xml.count("<UserId>DOM\\me</UserId>") == 2
    assert "P&amp;Q" in xml and "a&lt;b&gt;" in xml and "x&amp;y" in xml and "P&Q" not in xml


def test_a_rebuilt_index_remembers_a_vanished_pair(bed):
    s, v, c = bed["saves"], bed["vault"], bed["clock"]
    write_pair(s, "Save77", age=3600)
    v.sweep()
    for p in s.glob("Save77.*"):
        p.unlink()
    for _ in range(save_vault.VANISH_MISSES):
        c.tick(15)
        v.sweep()
    (bed["root"] / "status" / "kept.json").unlink()
    again = Vault(bed["root"], [s], is_busy=lambda: False, clock=c, reserve_gb=0)
    assert any(p.get("gone") for p in again.kept()["pairs"].values())


def test_a_returning_pair_is_no_longer_gone(bed):
    s, v, c = bed["saves"], bed["vault"], bed["clock"]
    paths = write_pair(s, "Save78", age=3600)
    data = [p.read_bytes() for p in paths]
    v.sweep()
    for p in paths:
        p.unlink()
    for _ in range(save_vault.VANISH_MISSES):
        c.tick(15)
        v.sweep()
    for p, d in zip(paths, data):
        p.write_bytes(d)
        os.utime(p, (c() - 3600, c() - 3600))
    c.tick(15)
    v.sweep()
    assert not any(p.get("gone") for p in v.kept()["pairs"].values())


def test_every_version_of_an_ini_is_kept_and_can_be_put_back(tmp_path):
    cfg = tmp_path / "My Games" / "Skyrim VR"
    cfg.mkdir(parents=True)
    c = Clock()
    v = Vault(tmp_path / "ini-vault", [cfg], is_busy=lambda: False, clock=c, reserve_gb=0, exts=save_vault.INI_EXTS)
    ini = cfg / "SkyrimPrefs.ini"
    ini.write_text("[Display]\nfGamma=1.0\n")
    os.utime(ini, (c() - 3600, c() - 3600))
    v.sweep()
    ini.write_text("[Display]\nfGamma=1.5\n")
    os.utime(ini, (c() - 1800, c() - 1800))                      # a later write: a later mtime
    v.sweep()
    c.tick(SETTLE_S + 1)
    v.sweep()
    kept = v.versions("SkyrimPrefs")
    assert len(kept) == 2 and kept[0]["note"] == ""
    ini.unlink()
    v.restore("SkyrimPrefs", event_id=kept[0]["id"], apply=True)
    assert "fGamma=1.0" in ini.read_text()


def test_one_watcher_sweeps_both_vaults(bed, tmp_path, monkeypatch):
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    (cfg / "Skyrim.ini").write_text("x")
    os.utime(cfg / "Skyrim.ini", (bed["clock"]() - 3600, bed["clock"]() - 3600))
    write_pair(bed["saves"], "Save79", age=3600)
    ini = Vault(tmp_path / "ini-vault", [cfg], is_busy=lambda: False, clock=bed["clock"], reserve_gb=0,
                exts=save_vault.INI_EXTS)
    monkeypatch.setattr(save_vault.time, "sleep", lambda s: None)
    calls = iter([False, True])
    save_vault.watch(lambda: bed["vault"], lambda: next(calls), lambda: ini)
    assert bed["vault"].versions("Save79") and ini.versions("Skyrim")
