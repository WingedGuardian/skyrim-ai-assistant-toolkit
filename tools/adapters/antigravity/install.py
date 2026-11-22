#!/usr/bin/env python3
"""Register the toolkit guard as a Google Antigravity PreToolUse hook: writes `.agents/hooks.json`.

    python tools/adapters/antigravity/install.py            # dry run: prints what it would write
    python tools/adapters/antigravity/install.py --apply    # writes it

Antigravity's docs do not say which shell runs a hook command on Windows, and a Skyrim folder usually has spaces
in its path. So the command is written with nothing to quote: this interpreter and the adapter by their absolute
paths, each shortened to its 8.3 name on Windows when it contains a space. An existing hooks.json is merged: only the
toolkit's own entry is added or replaced, and a file this script cannot read is never overwritten.
NOT MEASURED: no live Antigravity session has run this hook yet (ADAPTING.md section 7 lists the canaries).
"""
import argparse
import json
import os
import sys
from pathlib import Path

ENTRY = "skyrim-toolkit-guard"
TOOLS = ("run_command", "manage_task", "write_to_file", "replace_file_content", "multi_replace_file_content")
ADAPTER = "tools/adapters/antigravity/adapter.py"
TIMEOUT_S = 40          # must exceed the adapter's own 25 s deadline, or a hung engine lets the call run


def interpreter(path=sys.executable):
    """An absolute path with no space in it (8.3 short name on Windows), forward slashes."""
    path = os.path.abspath(path)
    if os.name == "nt" and " " in path:
        import ctypes
        buf = ctypes.create_unicode_buffer(32768)
        if ctypes.windll.kernel32.GetShortPathNameW(path, buf, len(buf)) and " " not in buf.value:
            path = buf.value
    if " " in path:
        raise ValueError("this path has a space and no short name: " + path)
    return path.replace("\\", "/")


def entry(python, adapter):
    return {"PreToolUse": [{"matcher": "^(" + "|".join(TOOLS) + ")$",
                            "hooks": [{"type": "command", "command": python + " -B " + adapter,
                                       "timeout": TIMEOUT_S}]}]}


def plan(root, python, adapter=None):
    target = Path(root) / ".agents" / "hooks.json"
    current = {}
    if target.exists():
        current = json.loads(target.read_text(encoding="utf-8"))      # unreadable: raise, never overwrite
        if not isinstance(current, dict):
            raise ValueError("%s is not a JSON object; not touching it" % target)
    merged = dict(current)
    # Absolute: the docs do not say which folder a hook starts in, and a relative path that does not resolve
    # makes Python exit before the adapter's fail-closed code can run.
    merged[ENTRY] = entry(python, adapter or interpreter(str(Path(root) / ADAPTER)))
    return target, merged


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--root", default=str(Path(__file__).resolve().parents[3]))
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args(argv)
    try:
        if not (Path(a.root) / ADAPTER).is_file():
            raise ValueError("no %s under %s" % (ADAPTER, a.root))
        target, merged = plan(a.root, interpreter())
    except (OSError, ValueError) as exc:
        print("refused: " + str(exc), file=sys.stderr)
        return 2
    text = json.dumps(merged, indent=2) + "\n"
    if not a.apply:
        print("dry run; would write " + str(target) + ":\n" + text)
        return 0
    target.parent.mkdir(exist_ok=True)
    tmp = target.with_suffix(".json.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, target)
    print("wrote " + str(target))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
