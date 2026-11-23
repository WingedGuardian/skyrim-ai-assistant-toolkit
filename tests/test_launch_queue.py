"""shared_claims launch queue, vr-headless --queue helpers and the devbench_own guard (fake processes, temp ledger)."""
import json, os, sys, importlib.util
from pathlib import Path
import pytest
REPO = Path(__file__).resolve().parent.parent
TOOLS = REPO / "tools"
sys.path.insert(0, str(TOOLS))
import shared_claims as sc
import devbench_own as dbo

ME = os.getpid()


@pytest.fixture
def L(tmp_path, monkeypatch):
    led = tmp_path / "claims.json"
    monkeypatch.setenv("SHARED_CLAIMS_LEDGER", str(led))
    monkeypatch.setenv("SHARED_CLAIMS_LOCK", str(tmp_path / "claims.lock"))
    return led


live = lambda pid, start: True
none = lambda: []


def test_fifo_and_turn_claims_both_slots_bound_to_pid(L):
    sc.enqueue("a", "r", ME, "background", approved_at="t0", alive=live)
    sc.enqueue("b", "r", ME, "background", alive=live)
    # b is second: must wait even though the install is free (red if FIFO is ignored)
    s = sc.take_turn("b", alive=live, games=none)
    assert s["turn"] is False and s["position"] == 2 and "ahead" in s["waiting_for"]
    t = sc.take_turn("a", alive=live, games=none)
    assert t["turn"] and t["approved_at"] == "t0"
    led = sc.Ledger(L)
    for slot in sc.SLOTS:
        assert led.value[slot] == "a" and led.details(slot)["pid"] == ME and led.details(slot)["reason"].startswith("queued launch")
    # a's ticket is gone; b is now head but slots are held by a (red if held slots are ignored)
    s = sc.take_turn("b", alive=live, games=none)
    assert s["turn"] is False and "slot held" in s["waiting_for"]


def test_game_running_blocks_turn(L):
    sc.enqueue("a", "r", ME, "background", alive=live)
    s = sc.take_turn("a", alive=live, games=lambda: ["skyrimvr.exe"])
    assert s["turn"] is False and "game or VR running" in s["waiting_for"]


def test_dead_ticket_never_blocks(L):
    sc.enqueue("dead", "r", ME, "background", alive=live)
    sc.enqueue("b", "r", ME, "background", alive=live)
    dead_only = lambda pid, start: False if False else True
    # mark 'dead' ticket dead by editing its pid_start so our fake alive says no
    v = json.loads(L.read_text()); v[sc.QUEUE][0]["pid_start"] = "gone"; L.write_text(json.dumps(v))
    alive = lambda pid, start: start != "gone"
    t = sc.take_turn("b", alive=alive, games=none)
    assert t["turn"] is True
    assert sc.queue_status(alive=alive) == []


def test_reentrant_enqueue_and_dequeue(L):
    a1 = sc.enqueue("a", "r", ME, "background", alive=live)
    a2 = sc.enqueue("a", "r", ME, "background", alive=live)
    assert a1["ticket"] == a2["ticket"] and len(sc.queue_status(alive=live)) == 1
    assert sc.dequeue("a", alive=live)["removed"] == 1
    with pytest.raises(sc.Refused):
        sc.take_turn("a", alive=live, games=none)


def test_unreadable_process_list_waits(L):
    sc.enqueue("a", "r", ME, "background", alive=live)
    def boom(): raise sc.Unknown("x")
    assert sc.take_turn("a", alive=live, games=boom)["turn"] is False


def test_guard_refuses_other_game():
    assert dbo.guard(111, read=lambda: {"pid": 111})["pid"] == 111
    with pytest.raises(dbo.NotYours):
        dbo.guard(111, read=lambda: {"pid": 222})       # red if the guard compared nothing


def test_session_pid_reads_manifest_and_refuses_restored_or_foreign(tmp_path):
    m = tmp_path / "s1" / "manifest.json"; m.parent.mkdir()
    man = {"creator_pid": 5, "processes": [{"ProcessName": "SkyrimVR", "Id": 4242}]}
    m.write_text(json.dumps(man)); (tmp_path / "active.json").write_text(json.dumps({"manifest": str(m)}))
    assert dbo.session_pid(sessions=tmp_path) == 4242
    with pytest.raises(dbo.NotYours):
        dbo.session_pid(creator=6, sessions=tmp_path)
    man["restored_at"] = "x"; m.write_text(json.dumps(man))
    with pytest.raises(dbo.NotYours):
        dbo.session_pid(sessions=tmp_path)


def test_launcher_wait_for_turn_and_timeout(L, monkeypatch):
    spec = importlib.util.spec_from_file_location("vrh", TOOLS / "vr-headless.py"); vrh = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(vrh)
    monkeypatch.setattr(sc, "running_games", lambda: [])
    t = vrh.wait_for_turn("a", "background", "r", "t0", 1, sleep=lambda s: None)
    assert t["turn"] and sc.Ledger(L).value["active_live_test_owner"] == "a"
    vrh.release_queued_claims("a")
    assert all(sc.Ledger(L).value.get(s) is None for s in sc.SLOTS)
    # timeout path removes the ticket (red if a timed-out waiter keeps blocking the queue)
    monkeypatch.setattr(sc, "running_games", lambda: ["skyrimvr.exe"])
    ticks = iter(range(0, 10000, 100))
    with pytest.raises((RuntimeError, vrh.Refused)):
        vrh.wait_for_turn("b", "background", "r", None, 1, sleep=lambda s: None, clock=lambda: next(ticks))
    assert sc.queue_status() == []


def test_release_queued_claims_leaves_normal_claims(L):
    sc.claim("active_live_test_owner", "a", "manual", session="s")
    spec = importlib.util.spec_from_file_location("vrh", TOOLS / "vr-headless.py"); vrh = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(vrh)
    vrh.release_queued_claims("a")
    assert sc.Ledger(L).value["active_live_test_owner"] == "a"   # red if it released a non-queued claim


def test_cli_queue_flags_need_owner_and_queue(L, capsys):
    spec = importlib.util.spec_from_file_location("vrh", TOOLS / "vr-headless.py"); vrh = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(vrh)
    assert vrh.main(["run", "--queue", "--desktop", "background"]) == 2
    assert "--queue needs --owner" in capsys.readouterr().err
    assert vrh.main(["run", "--hold", "--owner", "a", "--desktop", "background"]) == 2
    assert "--hold needs --queue" in capsys.readouterr().err
    assert sc.queue_status() == []          # nothing was enqueued by a refused command
