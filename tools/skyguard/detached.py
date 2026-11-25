"""S-NO-DETACHED-LAUNCH / W-NO-DETACHED-LAUNCH: refuse launching a helper DETACHED (Windows DETACHED_PROCESS, 0x8).

A detached process has no console, so every console program it starts without CREATE_NO_WINDOW gets a NEW VISIBLE
window. MEASURED 2026-11-24: an agent's driver started a headless-VR helper detached; the helper read processes
through powershell.exe once a second, and the user got a console pop-up every second for a whole run.
CREATE_NO_WINDOW (optionally | CREATE_NEW_PROCESS_GROUP) gives a hidden console the child's own children inherit,
and the child still outlives the caller, so detaching is never needed for that (tools/skyguard/hidden.py).

Scope: only DETACHED_PROCESS. A new visible console (CREATE_NEW_CONSOLE, Start-Process, a terminal the user opens) is
deliberate, visible work and is NOT refused.

The detector is the shared core's (tools/modding_harness/detached.py, vendored; shipped here first, in v4.3.0). Its
docstring lists what is read and the known limits. This module is the toolkit's wiring: finding() turns one engine
request (Claude Code, Codex or OpenCode; a shell command, a file write or edit, or an apply_patch) into the
offending statement or "".

Fail OPEN, never silently: this rule prevents pop-up spam and preserves nothing, so an error in the detector allows
the call rather than refusing every edit. finding() raises it; engine._detached catches it and writes an audit line
naming the error, so a broken check shows in .claude/backups/AUDIT_LOG.txt. The engine's own rules keep their
fail-closed handling.
"""
from __future__ import annotations

import json
import os

from modding_harness.detached import (  # noqa: F401 -- re-exported: the tests and the engine read them from here
    EXEMPT as CORE_EXEMPT, _option, _words, first_bad, judge, judge_patch, judge_shell, judge_text, judge_words,
)

# never judged: the vendored detector (the core's own list) and this toolkit's wiring and tests
EXEMPT = CORE_EXEMPT + ("tools/skyguard/detached.py", "tests/test_detached_launch.py")

OPENCODE_KEYS = {"filePath": "file_path", "content": "content", "oldString": "old_string",
                 "newString": "new_string", "replaceAll": "replace_all"}
TOOLS = {"Write": "Write", "Edit": "Edit", "MultiEdit": "MultiEdit", "NotebookEdit": "NotebookEdit",
         "write": "Write", "edit": "Edit"}


def _resolve(path, cwd):
    if not isinstance(path, str) or not path or not cwd or os.path.isabs(path):
        return path
    return os.path.join(cwd, path)


def finding(req):
    """The offending statement in this request, or "". A detector error RAISES: engine._detached allows the call and
    writes the error to the audit log (fail open, never silently)."""
    if req.kind == "shell" and isinstance(req.command, str) and req.command:
        hit = judge_shell(req.command) or judge_words(req.command, req.shell or "")
        if not hit:
            from . import patch_paths
            try:
                found = patch_paths.shell_patch(req.command)      # the adapter's own reading of a shell-run patch
            except patch_paths.PatchParseError:
                found = None                                    # refused by the adapter already (unreadable patch)
            if found:
                hit = judge_patch(found[0], _resolve(found[1], req.cwd) if found[1] else req.cwd, exempt=EXEMPT)
        return hit or ""
    if req.kind != "write":
        return ""
    if isinstance(req.command, str) and "*** Begin Patch" in req.command:   # a write request's command is its patch
        return judge_patch(req.command, req.cwd, exempt=EXEMPT) or ""
    tool = TOOLS.get(req.tool_name)
    if not tool or not req.tool_input:
        return ""
    inp = json.loads(req.tool_input)
    if not isinstance(inp, dict):
        return ""
    if req.tool_name in ("write", "edit"):                     # OpenCode spells the keys in camelCase
        inp = {OPENCODE_KEYS.get(k, k): v for k, v in inp.items()}
    for key in ("file_path", "notebook_path"):
        if key in inp:
            # the path the adapter resolved (OpenCode's transport normalises an MSYS /c/x spelling), else the input's
            inp[key] = _resolve(req.paths[0] if req.paths else inp[key], req.cwd)
    return judge(tool, inp, exempt=EXEMPT) or ""
