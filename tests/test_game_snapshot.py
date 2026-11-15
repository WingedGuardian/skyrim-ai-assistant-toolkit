"""game_snapshot: capture through DevBench's read-only inspect, and a diff that matches list entries by id."""
import json
import subprocess
import sys

import pytest

from conftest import REPO
sys.path.insert(0, str(REPO / "tools"))
import game_snapshot as gs  # noqa: E402

MOCK = REPO / "tests" / "mock_devbench.py"


def snap(**kinds):
    return {"kinds": kinds}


def test_identical_snapshots_have_no_differences():
    a = snap(player={"level": 41, "perks": [{"formId": "0x1", "name": "Armsman"}]})
    assert gs.diff(a, json.loads(json.dumps(a))) == []


def test_reordered_list_entries_are_matched_by_id_not_position():
    a = snap(inventory=[{"formId": "0x1", "count": 1}, {"formId": "0x2", "count": 5}])
    b = snap(inventory=[{"formId": "0x2", "count": 5}, {"formId": "0x1", "count": 1}])
    assert gs.diff(a, b) == []


def test_added_removed_and_changed_entries_are_reported():
    a = snap(inventory=[{"formId": "0x1", "count": 1}, {"formId": "0x2", "count": 5}])
    b = snap(inventory=[{"formId": "0x2", "count": 4}, {"formId": "0x3", "count": 1}])
    kinds = {(k, p) for k, p, _, _ in gs.diff(a, b)}
    assert ("removed", "inventory[formId=0x1].count") in kinds
    assert ("added", "inventory[formId=0x3].count") in kinds
    assert ("changed", "inventory[formId=0x2].count") in kinds


def test_volatile_fields_are_ignored():
    assert gs.diff(snap(state={"frame": 1, "level": 3}), snap(state={"frame": 99, "level": 3})) == []


def test_capture_uses_only_inspect_and_refuses_without_a_loaded_save(tmp_path, monkeypatch):
    log = tmp_path / "requests.log"
    monkeypatch.setenv("MOCK_LOG", str(log))
    proc = subprocess.Popen([sys.executable, str(MOCK), "0", "running"], stdout=subprocess.PIPE, text=True)
    try:
        port = int(proc.stdout.readline().strip().split("=", 1)[1])
        out = tmp_path / "before.json"
        taken = gs.capture(out, "http://127.0.0.1:{}".format(port))
        assert set(taken["kinds"]) == set(gs.KINDS) and out.is_file()
    finally:
        proc.terminate(); proc.wait(timeout=10)
    paths = {line.split(" ", 1)[0] for line in log.read_text(encoding="utf-8").splitlines()}
    assert paths == {"/api/tool/inspect"}


def test_cli_exit_codes(tmp_path, capsys):
    a, b = tmp_path / "a.json", tmp_path / "b.json"
    a.write_text(json.dumps(snap(player={"level": 41})))
    b.write_text(json.dumps(snap(player={"level": 42})))
    assert gs.main(["diff", str(a), str(a)]) == 0
    assert gs.main(["diff", str(a), str(b)]) == 1
    assert "player.level" in capsys.readouterr().out
    assert gs.main(["diff", str(a)]) == 2
