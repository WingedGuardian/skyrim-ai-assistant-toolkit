#!/usr/bin/env python3
"""Adapter template (ADAPTING.md, section 6). tools/adapting/adapter_template.py

Copy to tools/adapters/<name>/adapter.py and edit ONLY the block between SLOTS-BEGIN and SLOTS-END.
Everything else is the fail-closed machinery; tools/adapter_conformance.py checks that it is unchanged.

What it does, per tool call: read the agent's payload from stdin -> map_payload() -> run the toolkit
engine (`run.py neutral pre-tool`) -> check the answer -> render the verdict as THIS agent's hard block.
If anything at all goes wrong it renders the same hard block, because an agent that treats a failed hook
as "allow" would otherwise run the command (that is how Codex behaves: spec E1, E17).
Python >= 3.8, standard library only.
"""
import io
import json
import os
import re
import subprocess
import sys
import time

# SLOTS-BEGIN
# Google Antigravity (antigravity.google/docs/hooks, read 2026-11-21). A PreToolUse hook gets
# {"toolCall": {"name", "args"}, "workspacePaths": [...], ...} on stdin and answers {"decision": ...} on stdout.
# NOT MEASURED: the docs show no deny example and do not say what a crash, bad output or a timeout does. The block
# below is `deny` as the docs' field table defines it; until a live canary (ADAPTING.md section 7) shows Antigravity
# stopping on it, treat this guard as unproven.
AGENT_NAME = "antigravity"
STATIC_BLOCK = ('{"decision": "deny", "reason": "GUARD INERT: the toolkit adapter failed, so it checked nothing."}', 0, "")
ASK_PREFIX = ("NEEDS YOUR CONFIRMATION -- the toolkit cannot ask through Antigravity, so it refuses instead. Stop, "
              "tell the user what you wanted to run and why; if they agree, they run it themselves. ")
# Every Antigravity tool that writes a file or runs a command (docs "Supported tools"), by its own name.
WRITE_TOOLS = ("write_to_file", "replace_file_content", "multi_replace_file_content")
SHELL_TOOLS = ("run_command", "manage_task")


def map_payload(raw):
    d = json.loads(raw.decode("utf-8"))
    call = d["toolCall"]
    tool, args = call["name"], call.get("args") or {}
    workspaces = d.get("workspacePaths") or []
    cwd = args.get("Cwd") or (workspaces[0] if workspaces and isinstance(workspaces[0], str) else "")
    base = {"v": 1, "agent": "antigravity", "event": "pre_tool", "cwd": cwd, "tool_name": tool,
            "session_id": d.get("conversationId") or ""}
    if tool in WRITE_TOOLS:
        # Creating a new file takes no backup; replacing or editing one does.
        op = "add" if tool == "write_to_file" and args.get("Overwrite") is False else "update"
        return dict(base, kind="write", paths=[args["TargetFile"]], ops=[op])
    if tool == "manage_task":
        # Action send_input types Input into a running task (possibly a shell): judged as a command. Its other
        # actions (list, status, kill) run nothing new.
        if args.get("Action") != "send_input":
            return dict(base, kind="other")
        return dict(base, kind="shell", shell=None, command=args["Input"])
    if tool in SHELL_TOOLS:
        # The docs call it "a Bash command"; on Windows the shell is not documented, so both dialects are tried.
        return dict(base, kind="shell", shell=None, command=args["CommandLine"])
    # UNMAPPED TOOLS ARE NOT GUARDED: anything that reaches this line is sent as kind other, which the engine
    # allows unjudged. Only a tool that neither writes nor runs anything may end up here.
    return dict(base, kind="other")


def render_block(reason):
    return (json.dumps({"decision": "deny", "reason": reason}), 0, "")


def render_allow(context):
    out = {"decision": "allow"}
    if context:
        out["reason"] = context
    return (json.dumps(out), 0, "")


render_ask = None
# SLOTS-END

ENGINE_BUDGET_S = 20      # the engine's own watchdog (SKYGUARD_ENGINE_TIMEOUT)
ADAPTER_TIMEOUT_S = 25    # this adapter's deadline. Your agent's hook timeout MUST be longer than this.
MARKER = "SKYGUARD-OK\n"
_UNSAFE = re.compile(r"[^A-Za-z0-9 ._:/()=,+-]")


def _root():
    d = os.path.dirname(os.path.abspath(__file__))
    for _ in range(6):
        if os.path.isfile(os.path.join(d, "tools", "skyguard", "run.py")):
            return d
        parent = os.path.dirname(d)
        if parent == d:
            break
        d = parent
    return ""


def _hb_dir(root):
    return os.path.join(root, ".claude", "backups", ".hook-heartbeat", "adapted",
                        re.sub(r"[^A-Za-z0-9._-]", "_", AGENT_NAME))


def _hb(root, stage, inv, detail=""):
    """Best-effort heartbeat; never decides anything."""
    if not root:
        return
    try:
        d = _hb_dir(root)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "pre-tool." + stage), "w") as f:
            f.write("{:.6f} {} {}\n".format(time.time(), inv, detail))
    except Exception:
        pass


def _engine(req_json, root, inv):
    run_py = os.path.join(root, "tools", "skyguard", "run.py")
    env = dict(os.environ)
    env.setdefault("SKYGUARD_ENGINE_TIMEOUT", str(ENGINE_BUDGET_S))
    cp = subprocess.run(
        [sys.executable, run_py, "neutral", "pre-tool", "--root", root, "--root-win", root,
         "--project", root, "--invocation", inv, "--telemetry", _hb_dir(root)],
        input=req_json.encode("utf-8"), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        timeout=float(os.environ.get("SKYGUARD_ADAPTER_TIMEOUT_S", ADAPTER_TIMEOUT_S)), env=env)
    if cp.returncode != 0:
        raise RuntimeError("engine exited {}".format(cp.returncode))
    text = cp.stdout.decode("utf-8")
    if not text.startswith(MARKER):
        raise RuntimeError("engine answered without the marker")
    v = json.loads(text[len(MARKER):])
    if (not isinstance(v, dict) or type(v.get("v")) is not int or v.get("v") != 1
            or v.get("decision") not in ("allow", "ask", "deny")
            or not all(isinstance(v.get(key), str) for key in ("reason", "context", "status", "code"))
            or v.get("status") not in ("evaluated", "partial", "failed")):
        raise RuntimeError("engine verdict malformed")
    return v


def _checked(t):
    ok = (isinstance(t, tuple) and len(t) == 3 and isinstance(t[0], str)
          and isinstance(t[1], int) and not isinstance(t[1], bool) and isinstance(t[2], str))
    if not ok:
        raise RuntimeError("a render slot did not return (stdout text, exit code, stderr text)")
    return t


def _render(v):
    decision = v["decision"]
    reason = str(v.get("reason", ""))
    context = str(v.get("context", ""))
    if decision == "deny":
        return _checked(render_block(reason))
    if decision == "ask":
        if render_ask is not None:
            return _checked(render_ask(reason))
        return _checked(render_block(ASK_PREFIX + reason))
    return _checked(render_allow(context))


def _block_for_failure(why):
    reason = ("GUARD INERT: the adapter could not get a verdict from the toolkit engine ({}). NOTHING was "
              "checked: this is a refusal, not a decision. Tell the user.").format(_UNSAFE.sub("", why)[:120])
    try:
        return _checked(render_block(reason))
    except BaseException:
        return STATIC_BLOCK


def _emit(out, code, err):
    """Write UTF-8 BYTES, never text: a piped Windows stdout is cp1252 and raises on a reason it cannot encode,
    and this runs outside main()'s try. Any failure falls back to STATIC_BLOCK on raw file descriptors."""
    try:
        o, e = out.encode("utf-8", "replace"), err.encode("utf-8", "replace")
        sys.stdout.buffer.write(o)
        sys.stdout.buffer.flush()
        if e:
            sys.stderr.buffer.write(e)
            sys.stderr.buffer.flush()
    except BaseException:
        try:
            so, code, se = STATIC_BLOCK
            os.write(1, so.encode("utf-8", "replace"))
            if se:
                os.write(2, se.encode("utf-8", "replace"))
        except BaseException:
            code = 2
    sys.exit(code)


def main():
    real_stdout = sys.stdout
    sys.stdout = io.StringIO()      # a slot's print() must never reach the agent: only _emit writes stdout
    root = _root()
    inv = "1.{}-{}".format(os.getpid(), int(time.time() * 1000) % 100000)
    try:
        _hb(root, "invoked", inv)
        raw = sys.stdin.buffer.read()
        if not root:
            raise RuntimeError("cannot locate the toolkit root")
        if not raw.strip():
            raise RuntimeError("empty payload")
        req = map_payload(raw)
        if not isinstance(req, dict):
            raise RuntimeError("map_payload did not return a dict")
        v = _engine(json.dumps(req), root, inv)
        out, code, err = _render(v)
        _hb(root, "emitted", inv, v["decision"])
    except BaseException as e:
        out, code, err = _block_for_failure(e.__class__.__name__ + " " + str(e))
        _hb(root, "failed", inv, e.__class__.__name__)
    sys.stdout = real_stdout
    _emit(out, code, err)


if __name__ == "__main__":
    main()
