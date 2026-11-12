"""Keep the save vault's watcher running from every agent's pre-tool path (portable.check).

Since 4.1 no rule decides on a save: tools/save_vault.py keeps every version instead, and only while its watcher
runs. Claude Code and Codex start it at session start (run._vault_note). A host with no session-start hook (OpenCode,
a generic adapter) would have neither rule nor vault, so the one path every host goes through before a tool call
looks at the watcher's heartbeat and starts the watcher when it is not running.

    ensure(project) -> ""  or one line saying the watcher was started (or could not be)

Cost per call: one small file read. A start is tried at most once per THROTTLE_S (a marker file beside the
heartbeat), so a watcher that cannot start does not launch a process per tool call. Only for the installed toolkit's
own save_vault.py, and never under a test harness: a test must never start a watcher on the real vault. Never raises.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

TOOL = Path(__file__).resolve().parents[1] / "save_vault.py"
STALE_S = 120          # save_vault.STALE_S: a heartbeat older than this means the watcher is not running
THROTTLE_S = 300       # seconds between two attempts to start it
STARTED = ("save vault: its watcher was not running, so it was started now. Until it runs, a changed save has no "
           "kept earlier version; check with python tools/save_vault.py status.")
FAILED = "save vault: its watcher is not running and could not be started; run python tools/save_vault.py watch"


def vault_root(env=None) -> Path:
    """Where the save vault lives: the same answer as save_vault.default_root (tests/test_vault_watch.py holds the two
    together), spelled here so a pre-tool call does not import the vault to read one file."""
    env = os.environ if env is None else env
    if env.get("SAVE_VAULT_DIR"):
        return Path(env["SAVE_VAULT_DIR"])
    if os.name == "nt":
        base = env.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    else:
        base = env.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "modding-agent-harness" / "save-vault" / "skyrim"


def running(root, now=None) -> bool:
    """True when the heartbeat says a watcher swept within STALE_S."""
    now = time.time() if now is None else now
    try:
        beat = json.loads((Path(root) / "status" / "heartbeat.json").read_text(encoding="utf-8"))
        return now - float(beat.get("last_sweep", 0)) <= STALE_S
    except (OSError, ValueError, TypeError, AttributeError):
        return False


def start(project) -> None:
    """Launch the watcher detached (a second watcher exits by itself: watch.lock)."""
    flags = 0x00000008 | 0x00000200 | 0x08000000 if os.name == "nt" else 0       # detached, new group, no window
    subprocess.Popen([sys.executable, "-B", str(TOOL), "--project", str(project), "watch"], cwd=str(project),
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     close_fds=True, creationflags=flags, start_new_session=os.name != "nt")


def ensure(project, env=None, now=None) -> str:
    env = os.environ if env is None else env
    try:
        own = (Path(project).resolve() / "tools" / "save_vault.py") == TOOL
    except OSError:
        own = False
    if not (own and TOOL.is_file()):
        return ""
    if env.get("SKYGUARD_SAVE_VAULT", "").strip().lower() == "off":
        return ""
    if env.get("SKYGUARD_HOUSEKEEP_SWEEP", "").strip().lower() == "off":
        return ""                           # the switch every test helper and smoke run sets: no background job
    # The process environment is read as well as `env`: the watcher inherits the process's, so a caller's own
    # mapping must not be able to start one from inside a test run.
    if any(e.get(k) for e in (env, os.environ) for k in ("PYTEST_CURRENT_TEST", "HARNESS_FIXTURE_ROOT")):
        return ""
    now = time.time() if now is None else now
    try:
        root = vault_root(env)
        if running(root, now):
            return ""
        marker = root / "status" / "start-attempt"
        try:
            if now - marker.stat().st_mtime < THROTTLE_S:
                return ""
        except OSError:
            pass                            # no attempt yet
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_bytes(b"")
        os.utime(marker, (now, now))
    except OSError:
        return ""
    try:
        start(project)
    except Exception:  # noqa: BLE001 -- the vault must never cost a tool call its verdict
        return FAILED
    return STARTED
