#!/usr/bin/env python3
"""The toolkit-update canary: after an update's setup, ask the INSTALLED guards to judge two calls.

`update-apply` (modding_harness.guarded_update) runs this after setup; a nonzero exit rolls the update back. It runs
each configured host's real hook entry the way that host does, a payload on stdin through a pipe, and requires:

- a raw text write to a plugin file (`Data/<name>.esp`) is REFUSED: the guard is alive and judging;
- a harmless shell command (`echo`) is ALLOWED: the guard does not refuse everything (Claude Code only, where a
  hook that blocks every call would block the very session that has to repair it, and Codex, whose Windows entry
  reports an engine failure as a deny).

Hosts checked: Claude Code (every PreToolUse hook in .claude/settings.json whose matcher covers the tool), Codex
(.codex/hooks.json, once setup has filled in its paths) and OpenCode (its Python adapter, which every install
ships). At least one must be checked, or the canary fails: an update whose guards nobody could ask is not verified.

Standard library only, and it takes the install as `--root`: the update keeps a copy of the PRE-update canary outside
the install and runs it too when a release changes the canary or setup, so that copy must run on its own.
It never writes the plugin it names: the refusal comes before any write, and if a guard allowed it the hook is only
asked, never the tool. Exit 0 every check passed, 1 a check failed or nothing could be checked.
"""
import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

TIMEOUT = 90                            # seconds per hook run (each hook's own timeout is 60)
GIT_BASH = "C:/Program Files/Git/bin/bash.exe"
NO_WINDOW = 0x08000000 if os.name == "nt" else 0       # CREATE_NO_WINDOW: never a console window, never detached
PROBE = "Data/__toolkit_update_canary__.esp"


def bash():
    """Git Bash, never WSL's launcher (the same rule as update_check._bash)."""
    for candidate in (GIT_BASH, shutil.which("bash")):
        if not candidate or not Path(candidate).is_file():
            continue
        lowered = Path(candidate).as_posix().lower()
        if "/windows/system32/" in lowered or "/windowsapps/" in lowered:
            continue
        return Path(candidate).as_posix()
    return None


def run(argv, payload, root, env=None):
    """(exit code, stdout) of one hook run with PAYLOAD on stdin, or (None, reason) when it could not run."""
    try:
        done = subprocess.run(argv, input=json.dumps(payload).encode("utf-8"), capture_output=True, cwd=str(root),
                              timeout=TIMEOUT, env={**os.environ, **(env or {})}, creationflags=NO_WINDOW)
    except (OSError, subprocess.SubprocessError) as exc:
        return None, "{}: {}".format(type(exc).__name__, exc)
    return done.returncode, done.stdout.decode("utf-8", "replace")


def refused(code, out):
    """A host's refusal: exit 2 (Claude Code's blocking exit) or a JSON deny on stdout."""
    if code == 2:
        return True
    for line in reversed(out.strip().splitlines()):
        if line.startswith("SKYGUARD-OPENCODE "):
            line = line[len("SKYGUARD-OPENCODE "):]          # the OpenCode adapter's reply prefix
        try:
            data = json.loads(line)
        except ValueError:
            continue
        if not isinstance(data, dict):
            continue
        inner = data.get("hookSpecificOutput") if isinstance(data.get("hookSpecificOutput"), dict) else {}
        decision = inner.get("permissionDecision") or data.get("decision") or data.get("permissionDecision")
        return str(decision).lower() in ("deny", "block")
    return False


def claude_checks(root, shell):
    """[(label, ok, detail)] for Claude Code's PreToolUse hooks; [] when Claude Code is not set up here."""
    try:
        settings = json.loads((root / ".claude" / "settings.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    groups = (settings.get("hooks") or {}).get("PreToolUse") or []
    def commands(tool):
        out = []
        for group in groups:
            matcher = group.get("matcher") or ""
            if matcher and re.fullmatch(matcher, tool):
                out += [h.get("command") for h in group.get("hooks") or [] if isinstance(h.get("command"), str)]
        return out
    write, shell_cmds = commands("Write"), commands("Bash")
    if not write and not shell_cmds:
        return []
    if shell is None:
        return [("Claude Code hooks", False, "Git Bash was not found, so the hooks cannot run")]
    env = {"CLAUDE_PROJECT_DIR": root.as_posix()}
    checks = []
    deny = {"hook_event_name": "PreToolUse", "tool_name": "Write", "cwd": root.as_posix(), "session_id": "update-canary",
            "tool_input": {"file_path": (root / PROBE).as_posix(), "content": "update canary\n"}}
    results = [(c, *run([shell, "-c", c], deny, root, env)) for c in write]
    ok = any(code is not None and refused(code, out) for _c, code, out in results)
    checks.append(("Claude Code refuses a raw plugin write", ok,
                   "; ".join("{} -> {}".format(c, "could not run: " + out if code is None else "exit %d" % code)
                             for c, code, out in results)))
    allow = {"hook_event_name": "PreToolUse", "tool_name": "Bash", "cwd": root.as_posix(), "session_id": "update-canary",
             "tool_input": {"command": "echo toolkit-update-canary"}}
    for c in shell_cmds:
        code, out = run([shell, "-c", c], allow, root, env)
        checks.append(("Claude Code allows a harmless command ({})".format(c), code == 0 and not refused(code, out),
                       "could not run: " + out if code is None else "exit %d" % code))
    return checks


def codex_verdict(code, out):
    """Codex needs exit 0 and a PreToolUse decision inside hookSpecificOutput."""
    if code != 0:
        return "invalid"
    if not out.strip():
        return "allow"
    try:
        body = json.loads(out)["hookSpecificOutput"]
        if body["hookEventName"] == "PreToolUse":
            decision = body.get("permissionDecision", "allow")
            if decision in ("allow", "deny"):
                return decision
    except (KeyError, TypeError, ValueError):
        pass
    return "invalid"


def codex_checks(root, shell):
    path = root / ".codex" / "hooks.json"
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    except (OSError, ValueError) as exc:
        return [("Codex hook", False, str(exc))]
    if "{{" in text:
        return []                                     # setup has not configured Codex on this install
    windows = os.name == "nt"
    try:
        groups = json.loads(text)["hooks"]["PreToolUse"]
        commands = [(re.compile(group.get("matcher") or ".*"),
                     hook["commandWindows"] if windows and "commandWindows" in hook
                     else hook["command"]) for group in groups for hook in group["hooks"] if hook["type"] == "command"]
    except (KeyError, TypeError, ValueError, re.error) as exc:
        return [("Codex hook", False, "invalid rendered hook definition: {}".format(exc))]
    if not windows and shell is None:
        return [("Codex hook", False, "Bash was not found")]
    patch = "*** Begin Patch\n*** Add File: {}\n+update canary\n*** End Patch".format(PROBE)
    checks = []
    for tool, sample, expected, label in (
        ("apply_patch", patch, "deny", "Codex refuses a raw plugin write"),
        ("Bash", "echo toolkit-update-canary", "allow", "Codex allows a harmless command"),
    ):
        payload = {"hook_event_name": "PreToolUse", "tool_name": tool, "cwd": root.as_posix(),
                   "session_id": "update-canary", "tool_input": {"command": sample}}
        results = []
        for matcher, command in commands:
            if not matcher.fullmatch(tool):
                continue
            if windows:
                # Keep the rendered command's quoting and its PowerShell fallback intact under cmd.exe.
                cmd = subprocess.list2cmdline([os.environ.get("COMSPEC") or "cmd.exe"])
                argv = '{} /D /S /C "{}"'.format(cmd, command)
            else:
                argv = [shell, "-c", command]
            code, out = run(argv, payload, root)
            results.append(codex_verdict(code, out))
        ok = bool(results) and "invalid" not in results and ("deny" in results) == (expected == "deny")
        checks.append((label, ok, ", ".join(results) if results else "no matching command hook"))
    return checks


def opencode_checks(root):
    adapter = root / ".opencode" / "hooks" / "skyguard_adapter.py"
    if not adapter.is_file():
        return []
    payload = {"v": 1, "tool": "write", "directory": str(root),
               "args": {"filePath": str(root / PROBE), "content": "update canary\n"}}
    code, out = run([sys.executable, "-B", str(adapter), "pre"], payload, root)
    return [("OpenCode refuses a raw plugin write", code is not None and refused(code, out),
             "could not run: " + out if code is None else "exit %d" % code)]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", default=".", help="the installed toolkit folder")
    root = Path(parser.parse_args(argv).root).resolve()
    shell = bash()
    checks = claude_checks(root, shell) + codex_checks(root, shell) + opencode_checks(root)
    for label, ok, detail in checks:
        print("{}  {}{}".format("ok  " if ok else "FAIL", label, "  ({})".format(detail) if detail else ""))
    if not checks:
        print("FAIL  no guard hook of any agent is configured here, so nothing could be checked")
        return 1
    return 0 if all(ok for _label, ok, _detail in checks) else 1


if __name__ == "__main__":
    raise SystemExit(main())
