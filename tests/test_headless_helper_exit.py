"""Headless cleanup when a SteamVR helper exits by itself (ported from the Souls lifecycle repair, 2026-11-09).

Stopping a parent vrwebhelper makes its Chromium children exit on their own. Before this fix the toolkit's
`terminate_verified` raised on such a child (ERROR_INVALID_PARAMETER once it is gone, ERROR_ACCESS_DENIED while it
runs down) and `stop` aborted with the remaining helpers still running. An exit is excused only when it is proved.
"""
import ctypes
import ctypes.wintypes as w
import json
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

from conftest import REPO
from test_vr_headless import launch_session, tree, world  # noqa: F401  (fixtures)

if str(REPO / "tools") not in sys.path:
    sys.path.insert(0, str(REPO / "tools"))

EPOCH = "1601-01-01T00:00:00Z"          # what a zeroed FILETIME reads as: the fakes' recorded creation time


def _fake(monkeypatch, *, open_ok=True, open_error=87, listed=None, terminate_ok=True, wait=0, created=EPOCH):
    import vr_background_desktop as desktop
    closed, sent = [], []
    error = {"value": 0}

    def open_process(*a):
        error["value"] = open_error
        return 77 if open_ok else 0

    def terminate(*a):
        sent.append(a)
        error["value"] = 5
        return 1 if terminate_ok else 0

    kernel = SimpleNamespace(OpenProcess=open_process, GetProcessTimes=lambda *a: 1, CloseHandle=closed.append,
                             TerminateProcess=terminate, WaitForSingleObject=lambda *a: wait)
    monkeypatch.setattr(desktop, "_api", lambda: (None, kernel, w))
    monkeypatch.setattr(ctypes, "get_last_error", lambda: error["value"], raising=False)
    monkeypatch.setattr(ctypes, "WinError", lambda code=None: OSError(code, "synthetic native failure"), raising=False)
    monkeypatch.setattr(desktop, "process_generation_limited", lambda pid: listed)
    return desktop, closed, sent


def test_a_live_helper_is_terminated(monkeypatch):
    desktop, closed, sent = _fake(monkeypatch)
    assert desktop.terminate_verified(55, EPOCH) == "terminated"
    assert len(sent) == 1 and closed == [77]


def test_a_helper_already_gone_is_recorded_as_exited(monkeypatch):
    desktop, closed, sent = _fake(monkeypatch, open_ok=False, open_error=87, listed=None)
    assert desktop.terminate_verified(55, EPOCH) == "exited"
    assert sent == []


def test_an_unopenable_pid_still_listed_keeps_its_error(monkeypatch):
    import datetime
    desktop, _, _ = _fake(monkeypatch, open_ok=False, open_error=87,
                          listed=(datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc), True))
    with pytest.raises(OSError):
        desktop.terminate_verified(55, EPOCH)


def test_open_access_denied_is_never_an_exit(monkeypatch):
    desktop, _, _ = _fake(monkeypatch, open_ok=False, open_error=5, listed=None)
    with pytest.raises(OSError):
        desktop.terminate_verified(55, EPOCH)


def test_a_helper_running_down_is_recorded_as_exited_once_its_handle_signals(monkeypatch):
    desktop, closed, _ = _fake(monkeypatch, terminate_ok=False, wait=0)
    assert desktop.terminate_verified(55, EPOCH) == "exited"
    assert closed == [77]


def test_a_live_helper_that_refuses_termination_keeps_its_error(monkeypatch):
    desktop, closed, _ = _fake(monkeypatch, terminate_ok=False, wait=0x102)     # WAIT_TIMEOUT: still running
    with pytest.raises(OSError):
        desktop.terminate_verified(55, EPOCH)
    assert closed == [77]


def test_a_reused_pid_is_still_refused(monkeypatch):
    desktop, _, sent = _fake(monkeypatch)
    with pytest.raises(RuntimeError, match="reused"):
        desktop.terminate_verified(55, "2026-10-25T10:00:00Z")
    assert sent == []


# --- real owned processes (Windows): the same outcomes through the real kernel32 bindings

windows = pytest.mark.skipif(sys.platform != "win32", reason="real process handles exist only on Windows")


def _start(p):
    import vr_background_desktop as desktop
    created, _running = desktop.process_generation(p.pid)
    return created.isoformat()


@windows
def test_real_live_process_is_terminated():
    import vr_background_desktop as desktop
    p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        assert desktop.terminate_verified(p.pid, _start(p)) == "terminated"
        assert p.wait(10) == 0       # TerminateProcess(handle, 0)
    finally:
        if p.poll() is None:
            p.kill()


@windows
def test_real_process_that_exited_while_a_handle_holds_it_is_exited():
    """The object stays (our Popen handle keeps it): TerminateProcess fails with 5 and the handle is signalled."""
    import vr_background_desktop as desktop
    p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(1.5)"])
    start = _start(p)
    p.wait(20)
    assert desktop.terminate_verified(p.pid, start) == "exited"


@windows
def test_real_process_that_is_gone_is_exited():
    """Our handle is closed. Windows may still keep the object (another holder), so this reaches either proved-exit
    branch; the ERROR_INVALID_PARAMETER branch itself is pinned by the fake-kernel test above (MEASURED 2026-11-09:
    here it went through the signalled-handle branch)."""
    import vr_background_desktop as desktop
    p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(1.5)"])
    start, pid = _start(p), p.pid
    p.wait(20)
    p._handle.Close()
    time.sleep(0.2)
    assert desktop.terminate_verified(pid, start) == "exited"


# --- stop: a helper that ended by itself does not abort cleanup

def test_stop_completes_when_a_helper_exits_by_itself(tree, world, capsys):
    launch_session(tree, world)
    mod = tree["mod"]

    def exits_first(pid, start):
        assert world.process_alive(pid, start)
        world.procs = [p for p in world.procs if p["Id"] != pid]       # it ended by itself; nothing was sent
        return "exited"
    mod.terminate_verified = exits_first
    assert mod.main(["stop"]) == 0
    line = next(x for x in capsys.readouterr().out.splitlines() if x.startswith("stopped: "))
    cleanup = json.loads(line[len("stopped: "):])
    assert cleanup["helpers_exited"] == [11] and cleanup["helpers_stopped"] == []
    assert mod.load_state() is None


# --- stop: the cleanup budget is not spent proving late helpers before any of them is terminated

def test_many_late_helpers_are_all_terminated_within_the_budget(tree, world, monkeypatch):
    """Souls control06 shape: each late helper's ownership proof used to run its own process census (one PowerShell
    CIM query, ~2 s here), and the 15 s deadline was checked before the first termination, so with ~10 late helpers
    nothing was ever terminated. One census per pass, and every pass terminates what it proved first."""
    from test_vr_headless import proc
    mod = tree["mod"]
    launch_session(tree, world)
    world.game_exits()
    late = [proc(30 + i, "vrmonitor", str(tree["vr"] / "bin" / "vrmonitor.exe")) for i in range(10)]
    world.procs += late
    tree_read = mod.read_process_tree
    calls = []

    def slow_census():
        calls.append(world.clock.now)
        world.clock.now += 2                      # one CIM query
        return [dict(p, ParentId=11) if p["Id"] >= 30 else p for p in tree_read()]

    monkeypatch.setattr(mod, "read_process_tree", slow_census)
    mod.stop_owned(mod.load_state())
    assert sorted(x for x in world.terminated if x >= 30) == [p["Id"] for p in late]
    assert len(calls) <= 2, "one census per cleanup pass, not one per helper"


def test_a_census_slower_than_the_whole_budget_still_terminates_what_it_proved(tree, world, monkeypatch):
    from test_vr_headless import proc
    mod = tree["mod"]
    launch_session(tree, world)
    world.game_exits()
    world.procs.append(proc(40, "vrmonitor", str(tree["vr"] / "bin" / "vrmonitor.exe")))
    tree_read = mod.read_process_tree

    def stalled_census():
        world.clock.now += 16                     # the machine is loaded: one census outlasts the 15 s budget
        return [dict(p, ParentId=11) if p["Id"] == 40 else p for p in tree_read()]

    monkeypatch.setattr(mod, "read_process_tree", stalled_census)
    mod.stop_owned(mod.load_state())
    assert 40 in world.terminated


def test_a_helper_that_refuses_only_the_wait_right_is_still_terminated(monkeypatch):
    """4.1 review: SYNCHRONIZE was added to the open; a process granting TERMINATE without it must still be stopped."""
    import vr_background_desktop as desktop
    desktop_, closed, sent = _fake(monkeypatch)
    error = {"value": 0}
    asked = []

    def open_process(access, inherit, pid):
        asked.append(access)
        if access & desktop.SYNCHRONIZE:
            error["value"] = 5
            return 0
        return 77
    kernel = desktop._api()[1]
    monkeypatch.setattr(kernel, "OpenProcess", open_process)
    monkeypatch.setattr(ctypes, "get_last_error", lambda: error["value"], raising=False)
    assert desktop.terminate_verified(55, EPOCH) == "terminated"
    assert len(asked) == 2 and not asked[1] & desktop.SYNCHRONIZE and len(sent) == 1
