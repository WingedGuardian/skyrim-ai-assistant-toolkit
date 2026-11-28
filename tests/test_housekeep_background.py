import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
from modding_harness.housekeep_budget import worker_slot  # noqa: E402


def test_foreground_and_session_cli_share_the_worker_lock(tmp_path):
    config = tmp_path / "housekeep.json"
    config.write_text(json.dumps({"root": str(tmp_path)}), encoding="utf-8")
    plan = tmp_path / "plan.json"
    plan.write_text("previous plan", encoding="utf-8")
    note = tmp_path / "note.txt"
    cmd = [sys.executable, "-B", str(ROOT / "tools/housekeep.py"), "--config", str(config)]
    flags = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
    with worker_slot(tmp_path) as acquired:
        assert acquired
        busy = subprocess.run(cmd + ["plan", "-o", str(plan)], **flags,
                              capture_output=True, timeout=15)
        assert busy.returncode == 1
        assert b"another worker is active" in busy.stderr
        session = subprocess.run(cmd + ["session", str(note)], **flags,
                                 capture_output=True, timeout=15)
        assert session.returncode == 0
    assert plan.read_text(encoding="utf-8") == "previous plan"
    assert not note.exists()
