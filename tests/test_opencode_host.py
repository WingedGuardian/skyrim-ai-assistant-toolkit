"""The shipped Skyrim OpenCode plugin inside a REAL OpenCode host, driven by a scripted loopback model.

tests/test_opencode_portable.py drives the plugin's hooks from node; it proves the transport, never that OpenCode calls
them and honours a refusal. This starts the actual `opencode` binary on a throwaway toolkit kit. A loopback "model"
asks for three tool calls in turn: a write over a plugin file, a bash `rm -rf` of the Saves folder, and a write of an
ordinary notes file. The guard must refuse the first two, and the host must run the third, which proves the host ran
tools at all (without it, a model call that silently failed would pass the refusals).

No credentials, no network model: the provider is an OpenAI-compatible server on 127.0.0.1 in this process. OpenCode
gets a fresh HOME/XDG folder, so it reads no user configuration. It still installs its provider package from npm at
startup, so the host needs the network once, which is why CI runs this file in its own step, outside the contained
launcher.

Needs TOOLKIT_OPENCODE_BIN (the opencode executable). CI sets TOOLKIT_REQUIRE_OPENCODE_HOST=1 in that step, so a
missing binary fails there instead of skipping.
"""
import http.server
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading

import pytest

from conftest import REPO

KEEP_ENV = ("PATH", "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "TEMP", "TMP", "SYSTEMDRIVE",
            "PROGRAMFILES", "PROGRAMDATA", "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE")


def host_binary():
    binary = os.environ.get("TOOLKIT_OPENCODE_BIN", "")
    if binary and Path(binary).is_file():
        # Absolute: the host runs with the throwaway kit as its working folder (CI passes a repo-relative path).
        return str(Path(binary).resolve())
    if os.environ.get("TOOLKIT_REQUIRE_OPENCODE_HOST") == "1":
        pytest.fail("TOOLKIT_OPENCODE_BIN must name the opencode executable for host qualification")
    pytest.skip("no OpenCode host (set TOOLKIT_OPENCODE_BIN); real-host enforcement was not tested")


def scripted_model(calls):
    """An OpenAI-compatible chat endpoint: one scripted tool call per request until the script runs out, then text."""
    record = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
            names = {t.get("function", {}).get("name") for t in body.get("tools") or []}
            tool_rows = [str(m.get("content")) for m in body.get("messages", []) if m.get("role") == "tool"]
            record.append(tool_rows)
            base = {"id": "c", "object": "chat.completion.chunk", "created": 0, "model": "scripted"}
            done = len(tool_rows)
            if {"bash", "write"} <= names and done < len(calls):
                name, args = calls[done]
                chunks = [{**base, "choices": [{"index": 0, "finish_reason": None, "delta": {"role": "assistant", "tool_calls": [
                              {"index": 0, "id": "call_%d" % done, "type": "function",
                               "function": {"name": name, "arguments": json.dumps(args)}}]}}]},
                          {**base, "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]}]
            else:
                chunks = [{**base, "choices": [{"index": 0, "delta": {"role": "assistant", "content": "done"}, "finish_reason": None}]},
                          {**base, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}]
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            for chunk in chunks:
                self.wfile.write(b"data: " + json.dumps(chunk).encode() + b"\n\n")
            self.wfile.write(b"data: [DONE]\n\n")

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, record


def test_the_shipped_guard_refuses_in_a_real_opencode_host(tmp_path):
    binary = host_binary()
    root, home = tmp_path / "toolkit", tmp_path / "home"
    for name in ("skyguard", "modding_harness"):
        shutil.copytree(REPO / "tools" / name, root / "tools" / name, ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copytree(REPO / ".opencode", root / ".opencode")
    home.mkdir()
    saves = tmp_path / "config" / "Saves"
    saves.mkdir(parents=True)
    (root / "skyrim-paths.env").write_text("SKYRIM_CONFIG_DIR='" + saves.parent.as_posix() + "'\n")
    for name in ("save.ess", "save.skse"):
        (saves / name).write_bytes(name.encode())
    plugin = root / "Guarded.esp"
    plugin.write_bytes(b"TES4 original")
    notes = root / "notes.txt"
    calls = [("write", {"filePath": str(plugin), "content": "overwritten"}),
             ("bash", {"command": 'rm -rf "%s"' % saves.as_posix(), "description": "t"}),
             ("write", {"filePath": str(notes), "content": "control"})]
    server, record = scripted_model(calls)
    try:
        (root / ".opencode" / "opencode.json").write_text(json.dumps({
            "$schema": "https://opencode.ai/config.json",
            "provider": {"scripted": {"npm": "@ai-sdk/openai-compatible", "name": "Scripted",
                                      "options": {"baseURL": "http://127.0.0.1:%d/v1" % server.server_address[1],
                                                  "apiKey": "not-a-key"},
                                      "models": {"m": {"name": "m", "tool_call": True}}}},
            "model": "scripted/m", "small_model": "scripted/m",
            "permission": {"bash": "allow", "edit": "allow", "external_directory": "allow"}}))
        subprocess.run(["git", "init", "-q"], cwd=root, check=True)
        env = {k: v for k, v in os.environ.items() if k.upper() in KEEP_ENV}
        env.update(HOME=str(home), USERPROFILE=str(home), APPDATA=str(home / "AppData" / "Roaming"),
                   LOCALAPPDATA=str(home / "AppData" / "Local"), XDG_CONFIG_HOME=str(home / ".config"),
                   XDG_DATA_HOME=str(home / ".local" / "share"), XDG_CACHE_HOME=str(home / ".cache"),
                   XDG_STATE_HOME=str(home / ".local" / "state"), SKYRIM_PYTHON=sys.executable,
                   SKYRIM_NO_PYTHON_FALLBACK="1")
        result = subprocess.run([binary, "run", "--model", "scripted/m", "go"], cwd=root, env=env,
                                capture_output=True, text=True, encoding="utf-8", errors="replace",
                                timeout=300, stdin=subprocess.DEVNULL)
    finally:
        server.shutdown()
        server.server_close()
    tail = result.stdout[-3000:] + result.stderr[-3000:]
    assert record, "the scripted model was never called: " + tail
    # The control: the host ran the third tool, so it did reach and execute tool calls.
    assert notes.is_file() and notes.read_text() == "control", (record, tail)
    # The refusals: the plugin and both save files are untouched.
    assert plugin.read_bytes() == b"TES4 original", (record, tail)
    assert sorted(p.name for p in saves.iterdir()) == ["save.ess", "save.skse"], (record, tail)
    # The model was told why: every tool row after the run holds the three results, and the first two carry the guard.
    final = max(record, key=len)
    assert len(final) == 3, record
    shown = json.dumps(final, indent=1)
    assert "Blocked: plugin and archive files" in final[0], shown
    assert final[1].startswith("Blocked:") and "Saves" in final[1], shown
    assert not final[2].startswith("Blocked:"), shown
