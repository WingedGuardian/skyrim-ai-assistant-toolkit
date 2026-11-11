"""Session start says whether the save vault's watcher runs, and starts it when it does not (tools/skyguard/run.py).

Both agents' session-start roles go through run.py. The line is read from a heartbeat file; the watcher is a detached
process. No test here may reach the real vault: each one points SAVE_VAULT_DIR at a folder of its own.
"""
from __future__ import annotations

import faulthandler
import io
import json
import os
import subprocess
import sys
import time

import pytest

from conftest import REPO

sys.path.insert(0, str(REPO / "tools"))
from skyguard import run  # noqa: E402

TOOL = REPO / "tools" / "save_vault.py"
SWITCHES = ("PYTEST_CURRENT_TEST", "HARNESS_FIXTURE_ROOT", "HOUSEKEEP_REAL_BIN_IN_TESTS", "SKYGUARD_HOUSEKEEP_SWEEP",
            "SKYGUARD_SAVE_VAULT", "SAVE_VAULT_DIR")


def beat(root, age=1.0, ok=True, watched=2, **more):
    (root / "status").mkdir(parents=True, exist_ok=True)
    data = dict({"last_sweep": time.time() - age, "time": "t", "ok": ok, "pending": 0, "error": "", "notes": [],
                 "watched": watched}, **more)
    (root / "status" / "heartbeat.json").write_text(json.dumps(data), encoding="utf-8")


def _real_session(tmp_path, monkeypatch):
    """A real session's environment (no test marker, no switch) on a vault of this test's own; Popen is recorded.
    Called inside the test: pytest sets PYTEST_CURRENT_TEST again after the fixtures ran."""
    for k in SWITCHES:
        monkeypatch.delenv(k, raising=False)
    root = tmp_path / "vault" / "skyrim"
    monkeypatch.setenv("SAVE_VAULT_DIR", str(root))
    launched = []
    monkeypatch.setattr(subprocess, "Popen", lambda argv, **kw: launched.append((argv, kw)))
    return root, root.with_name("skyrim-config"), launched


def test_a_running_vault_is_one_line_and_starts_nothing(tmp_path, monkeypatch):
    saves, inis, launched = _real_session(tmp_path, monkeypatch)
    beat(saves)
    beat(inis)
    out = run._vault_note(str(REPO))
    assert out.startswith("save vault: running (2 folders") and out.count("\n") == 1 and launched == []


def test_a_stopped_vault_is_said_and_its_watcher_is_started_detached(tmp_path, monkeypatch):
    saves, inis, launched = _real_session(tmp_path, monkeypatch)
    out = run._vault_note(str(REPO))                                 # no heartbeat at all
    assert "save vault: NOT RUNNING" in out and "started the watcher now" in out
    (argv, kw), = launched
    assert argv[-4:] == [str(TOOL), "--project", str(REPO), "watch"]
    assert kw["stdout"] == subprocess.DEVNULL and (kw["creationflags"] if os.name == "nt" else kw["start_new_session"])
    del launched[:]
    beat(saves, age=10_000)                                         # a heartbeat that stopped long ago
    beat(inis, age=10_000)
    assert "started the watcher now" in run._vault_note(str(REPO)) and len(launched) == 1


def test_a_vault_with_a_problem_is_reported_and_not_restarted(tmp_path, monkeypatch):
    saves, inis, launched = _real_session(tmp_path, monkeypatch)
    beat(saves, ok=False, error="disk full")
    beat(inis)
    out = run._vault_note(str(REPO))
    assert "PROBLEM: disk full" in out and "INI vault: running" in out and launched == []


def test_an_ini_vault_down_alone_is_shown_and_left_to_the_running_watcher(tmp_path, monkeypatch):
    saves, inis, launched = _real_session(tmp_path, monkeypatch)
    beat(saves)
    out = run._vault_note(str(REPO))
    assert out.startswith("save vault: running") and "INI vault: NOT RUNNING" in out and launched == []


@pytest.mark.parametrize("marker", ["PYTEST_CURRENT_TEST", "HARNESS_FIXTURE_ROOT"])
def test_under_the_test_harness_no_watcher_starts_and_no_real_heartbeat_is_read(monkeypatch, marker, tmp_path):
    saves, inis, launched = _real_session(tmp_path, monkeypatch)
    monkeypatch.setenv(marker, "1")
    out = run._vault_note(str(REPO))
    assert "NOT RUNNING" in out and launched == [], "a test must never start a watcher"
    monkeypatch.delenv("SAVE_VAULT_DIR")                             # no vault of the test's own: nothing is read
    monkeypatch.setattr(run.Path, "read_text", lambda *a, **k: pytest.fail("read a file under the test harness"))
    assert run._vault_note(str(REPO)) == ""


def test_the_sweep_switch_also_stops_the_watcher_start(monkeypatch, tmp_path):
    """Helpers that clean the environment lose the test markers and rely on SKYGUARD_HOUSEKEEP_SWEEP=off."""
    saves, inis, launched = _real_session(tmp_path, monkeypatch)
    monkeypatch.setenv("SKYGUARD_HOUSEKEEP_SWEEP", "off")
    assert "NOT RUNNING" in run._vault_note(str(REPO)) and launched == []
    monkeypatch.delenv("SKYGUARD_HOUSEKEEP_SWEEP")                   # twin: without the switch it starts
    run._vault_note(str(REPO))
    assert len(launched) == 1


def test_the_vault_switch_silences_the_line_and_the_start(monkeypatch, tmp_path):
    saves, inis, launched = _real_session(tmp_path, monkeypatch)
    monkeypatch.setenv("SKYGUARD_SAVE_VAULT", "off")
    assert run._vault_note(str(REPO)) == "" and launched == []
    monkeypatch.setenv("SKYGUARD_SAVE_VAULT", "on")                  # twin
    assert run._vault_note(str(REPO)) != ""


def test_nothing_for_a_project_that_is_not_this_toolkit(tmp_path, monkeypatch):
    saves, inis, launched = _real_session(tmp_path, monkeypatch)
    assert run._vault_note(str(tmp_path)) == "" and launched == []
    assert run._vault_note(str(REPO)) != ""                          # twin: the toolkit itself


def test_a_vault_that_cannot_be_read_costs_one_line_never_the_session(monkeypatch, tmp_path):
    saves, inis, launched = _real_session(tmp_path, monkeypatch)
    import importlib.util
    monkeypatch.setattr(importlib.util, "spec_from_file_location", lambda *a, **k: (_ for _ in ()).throw(OSError("x")))
    out = run._vault_note(str(REPO))
    assert out.startswith("save vault: its state could not be read (OSError)") and launched == []


def test_a_launch_that_fails_is_said(monkeypatch, tmp_path):
    saves, inis, launched = _real_session(tmp_path, monkeypatch)
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: (_ for _ in ()).throw(OSError("no")))
    assert "could not be started" in run._vault_note(str(REPO))


class _Std:
    def __init__(self, buf):
        self.buffer = buf


@pytest.mark.parametrize("agent,role,payload", [
    ("claude", "session-kb-guard", {"session_id": "s1", "source": "startup"}),
    ("codex", "session-start", {"hook_event_name": "SessionStart", "source": "startup"}),
])
def test_both_session_starts_show_the_vault_line(tmp_path, monkeypatch, agent, role, payload):
    monkeypatch.setattr(run, "_vault_note", lambda project: "VAULT-LINE for {}\n".format(project))
    out = io.BytesIO()
    monkeypatch.setattr(sys, "stdin", _Std(io.BytesIO(json.dumps(payload).encode("utf-8"))))
    monkeypatch.setattr(sys, "stdout", _Std(out))
    try:
        code = run.main([agent, role, "--project", str(tmp_path), "--root", str(tmp_path), "--bash", "bash",
                         "--telemetry", str(tmp_path / "hb")])
    finally:
        faulthandler.cancel_dump_traceback_later()
    assert code == 0 and "VAULT-LINE for {}".format(tmp_path) in out.getvalue().decode("utf-8")
