"""setup.sh and the save vault: it is named every run; its sign-in task is registered only when asked for.

No test registers a real scheduled task: tools/save_vault.py refuses `install-task` under a test harness
(tests/test_save_vault.py), so the flag's FAILURE path is what runs here, and that is the path worth asserting.
"""
from __future__ import annotations

import sys

from conftest import make_game, run_setup

WIN = sys.platform == "win32"


def test_setup_names_the_vault_and_registers_no_task_unasked(tmp_path):
    r = run_setup(make_game(tmp_path), args=("--agent", "claude"))
    out = r.stdout + r.stderr
    assert r.returncode == 0, out[-3000:]
    assert "Save vault: every version of every save and settings INI is kept" in out
    assert ("Sign-in task: not registered" if WIN else "Its watcher is started by each Claude Code or Codex session and by any agent's first guarded tool call") in out
    assert "SIGN-IN TASK NOT REGISTERED" not in out and "Sign-in task registered" not in out
    assert "refused under a test harness" not in out, "setup must not even try without the flag"


def test_the_flag_tries_to_register_and_a_failure_is_exit_1(tmp_path):
    r = run_setup(make_game(tmp_path), args=("--agent", "claude", "--save-vault-task"))
    out = r.stdout + r.stderr
    assert r.returncode == 1, out[-3000:]
    if WIN:
        assert "refused under a test harness" in out and "SIGN-IN TASK NOT REGISTERED (exit 1, above)" in out
    else:
        assert "--save-vault-task: the sign-in task needs Windows. Not registered." in out


def test_the_no_flag_is_accepted_and_changes_nothing(tmp_path):
    r = run_setup(make_game(tmp_path), args=("--agent", "claude", "--no-save-vault-task"))
    out = r.stdout + r.stderr
    assert r.returncode == 0, out[-3000:]
    assert "refused under a test harness" not in out and "SIGN-IN TASK NOT REGISTERED" not in out
