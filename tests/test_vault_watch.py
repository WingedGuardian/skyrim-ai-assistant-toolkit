"""Every host's pre-tool path keeps the save vault's watcher running (tools/skyguard/vault_watch.py).

A host with no session-start hook (OpenCode, a generic adapter) has no other way to get the vault started, and since
4.1 no rule decides on a save. No test here may reach the real vault: each uses a vault folder of its own, and the
process launch is recorded, never made.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time

import pytest

from conftest import REPO

sys.path.insert(0, str(REPO / "tools"))
import save_vault  # noqa: E402
from modding_harness import contracts  # noqa: E402
from skyguard import portable, vault_watch  # noqa: E402

MARKERS = ("PYTEST_CURRENT_TEST", "HARNESS_FIXTURE_ROOT")
SWITCHES = ("SKYGUARD_HOUSEKEEP_SWEEP", "SKYGUARD_SAVE_VAULT")


def beat(root, age=1.0):
    (root / "status").mkdir(parents=True, exist_ok=True)
    (root / "status" / "heartbeat.json").write_text(json.dumps({"last_sweep": time.time() - age}), encoding="utf-8")


def real_session(tmp_path, monkeypatch):
    """As a real session: no test marker and no switch in the process, a vault of this test's own, Popen recorded.
    Called inside the test body: pytest sets PYTEST_CURRENT_TEST again after the fixtures ran."""
    for k in MARKERS + SWITCHES:
        monkeypatch.delenv(k, raising=False)
    root = tmp_path / "vault" / "skyrim"
    monkeypatch.setenv("SAVE_VAULT_DIR", str(root))
    launched = []
    monkeypatch.setattr(subprocess, "Popen", lambda argv, **kw: launched.append((argv, kw)))
    return root, launched


@pytest.mark.parametrize("env", [{"SAVE_VAULT_DIR": "C:/somewhere/vault"}, {"LOCALAPPDATA": "C:/Users/x/AppData/Local"},
                                 {"XDG_DATA_HOME": "/home/x/.local/share"}, {}])
def test_the_vault_is_looked_for_where_the_vault_itself_lives(env):
    assert vault_watch.vault_root(env) == save_vault.default_root(env)
    assert vault_watch.STALE_S == save_vault.STALE_S


def test_a_running_watcher_costs_nothing_and_starts_nothing(tmp_path, monkeypatch):
    root, launched = real_session(tmp_path, monkeypatch)
    beat(root)
    assert vault_watch.ensure(str(REPO)) == "" and launched == []
    assert not (root / "status" / "start-attempt").exists()


def test_a_stopped_watcher_is_started_once_and_said(tmp_path, monkeypatch):
    root, launched = real_session(tmp_path, monkeypatch)
    assert vault_watch.ensure(str(REPO)) == vault_watch.STARTED            # no heartbeat at all
    (argv, kw), = launched
    assert argv[-4:] == [str(REPO / "tools" / "save_vault.py"), "--project", str(REPO), "watch"]
    assert kw["stdout"] == subprocess.DEVNULL and (kw["creationflags"] if os.name == "nt" else kw["start_new_session"])
    beat(root, age=10_000)                                                  # a heartbeat that stopped long ago
    del launched[:]
    assert vault_watch.ensure(str(REPO), now=time.time() + vault_watch.THROTTLE_S + 1) == vault_watch.STARTED
    assert len(launched) == 1


def test_a_start_is_tried_at_most_once_per_throttle_period(tmp_path, monkeypatch):
    root, launched = real_session(tmp_path, monkeypatch)
    now = time.time()
    assert vault_watch.ensure(str(REPO), now=now) == vault_watch.STARTED
    assert vault_watch.ensure(str(REPO), now=now + vault_watch.THROTTLE_S - 5) == "" and len(launched) == 1
    assert vault_watch.ensure(str(REPO), now=now + vault_watch.THROTTLE_S + 5) == vault_watch.STARTED
    assert len(launched) == 2


@pytest.mark.parametrize("marker", MARKERS)
@pytest.mark.parametrize("where", ["process", "mapping"])
def test_under_a_test_harness_nothing_is_started(tmp_path, monkeypatch, marker, where):
    root, launched = real_session(tmp_path, monkeypatch)
    env = dict(os.environ)
    if where == "process":
        monkeypatch.setenv(marker, "1")
    else:
        env[marker] = "1"
    assert vault_watch.ensure(str(REPO), env=env) == "" and launched == []
    assert not (root / "status" / "start-attempt").exists()


@pytest.mark.parametrize("switch", SWITCHES)
def test_each_switch_stops_the_start(tmp_path, monkeypatch, switch):
    root, launched = real_session(tmp_path, monkeypatch)
    monkeypatch.setenv(switch, "off")
    assert vault_watch.ensure(str(REPO)) == "" and launched == []
    monkeypatch.setenv(switch, "on")                                        # twin: any other value starts it
    assert vault_watch.ensure(str(REPO)) == vault_watch.STARTED


def test_nothing_for_a_project_that_is_not_this_toolkit(tmp_path, monkeypatch):
    root, launched = real_session(tmp_path, monkeypatch)
    assert vault_watch.ensure(str(tmp_path)) == "" and launched == []
    assert vault_watch.ensure(str(REPO)) == vault_watch.STARTED            # twin: the toolkit itself


def test_a_launch_that_fails_is_said_and_never_raises(tmp_path, monkeypatch):
    root, launched = real_session(tmp_path, monkeypatch)
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: (_ for _ in ()).throw(OSError("no")))
    assert vault_watch.ensure(str(REPO)) == vault_watch.FAILED


def _check(tmp_path, **kw):
    root = tmp_path / "toolkit"
    root.mkdir(exist_ok=True)
    action = contracts.Action("opencode", "edit", "files", cwd=str(root), session_id="s", tool_call_id="c",
                              paths=(str(root / "notes.txt"),), operations=("write",))
    return portable.check(action, root, **kw)[0]


def test_every_pre_tool_check_looks_at_the_watcher_and_passes_its_note_on(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(vault_watch, "ensure", lambda project: seen.append(project) or "VAULT NOTE")
    outcome = _check(tmp_path)
    assert seen == [str(tmp_path / "toolkit")] and "VAULT NOTE" in outcome.result.warnings
    assert outcome.result.decision == "allow"


def test_a_preview_never_starts_a_watcher(tmp_path, monkeypatch):
    monkeypatch.setattr(vault_watch, "ensure", lambda project: pytest.fail("a preview must change nothing"))
    _check(tmp_path, preview=True)
