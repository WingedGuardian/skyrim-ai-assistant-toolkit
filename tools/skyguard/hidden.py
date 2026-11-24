"""Background helpers a session start launches (housekeep sweep, save vault watcher, update check): one way to start
them, so none can open a console window (user ruling 2026-11-14: no visible helper consoles).

Windows: a new process group with a HIDDEN console (CREATE_NO_WINDOW), never DETACHED_PROCESS. Windows ignores
CREATE_NO_WINDOW beside DETACHED_PROCESS, and a detached process has no console for its own console children to
inherit: the `py` launcher or a venv's python.exe redirector starts the real python.exe as such a child, and each
would open a visible window (Skyrim 4.3 release review; the X4 toolkit measured the same in its 4.1.1 review). A
process's lifetime does not depend on its parent's on Windows, so the helper outlives the hook that started it.
Elsewhere: a new session, so the hook's own process group ending does not take it down.
"""
from __future__ import annotations

import os
import subprocess

CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_NO_WINDOW = 0x08000000
FLAGS = CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW if os.name == "nt" else 0


def spawn_background(argv, cwd):
    """Start `argv` in the background with no window and no inherited handles; never waits. Raises what Popen
    raises (callers that must never fail catch it)."""
    return subprocess.Popen(argv, cwd=str(cwd), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, close_fds=True, creationflags=FLAGS,
                            start_new_session=os.name != "nt")
