"""S-/W-NO-DETACHED-LAUNCH (4.3): a helper launched DETACHED gives each console child a visible window.

The DENY / ALLOW tables and the edit-in-context tests are the reviewed packet's own (skyrimvr-save-guard-ci 23918cc,
no_detached_launch/test_no_detached_launch.py), run against tools/skyguard/detached.py in process instead of through
the packet's hook process. The rest checks the wiring: each agent's real entry point (Claude Code's hook wrapper,
Codex's adapter, OpenCode's adapter) refuses, a refused call names the fix, and a detector failure allows (fail open).
"""
import json
import os
import subprocess
import sys

import pytest

from conftest import REPO, install_hooks
sys.path.insert(0, str(REPO / "tools"))
from skyguard import detached, engine, paths  # noqa: E402
from skyguard.adapters import codex, opencode  # noqa: E402
from skyguard.audit import Audit  # noqa: E402


def decide(tool, tool_input):
    return "deny" if detached.judge(tool, tool_input) else "allow"


INCIDENT = ("    p = subprocess.Popen(cmd, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT,\n"
            "                         creationflags=NO_WINDOW | 0x00000200 | 0x00000008)  # no window, new group, detached\n")


def W(content, path="x.py"):
    return ("Write", {"file_path": path, "content": content})


DENY = {
    "incident-write": W(INCIDENT),
    "named-constant": W("subprocess.Popen(c, creationflags=subprocess.DETACHED_PROCESS)\n"),
    "named-or": ("Edit", {"file_path": "missing.py", "old_string": "a", "new_string": "flags = CREATE_NO_WINDOW | DETACHED_PROCESS\n"}),
    "named-or-spaced": W("base = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP\n"),
    "or-assign": W("flags |= subprocess.DETACHED_PROCESS\n"),
    "plain-assign": W("flags = subprocess.DETACHED_PROCESS\nPopen(c, creationflags=flags)\n"),
    "create-process": W("win32process.CreateProcess(None, cmd, None, None, 0, win32process.DETACHED_PROCESS, None, None, si)\n"),
    "c-multiline": W("ok = CreateProcessW(NULL, cmd, NULL, NULL, FALSE,\n    DETACHED_PROCESS,\n    NULL, NULL, &si, &pi);\n", "x.cpp"),
    "cpp-scope": W("BOOL ok = ::CreateProcessW(NULL, cmd, NULL, NULL, FALSE, DETACHED_PROCESS, NULL, NULL, &si, &pi);\n", "x.cpp"),
    "multiline-paren": W("p = Popen(c, creationflags=(\n    NO_WINDOW |\n    0x8))\n"),
    "trailing-or": W("p = Popen(c, creationflags=NO_WINDOW |\n    subprocess.DETACHED_PROCESS,\n    cwd=x)\n"),
    "csharp-trailing-or": W("uint flags = CREATE_NO_WINDOW |\n    DETACHED_PROCESS;\n", "x.cs"),
    "dict-key":W('kw = {"creationflags": 0x8}\n'),
    "kw-index": W('kw["creationflags"] = 8\n'),
    "rust": W("Command::new(x).creation_flags(0x8).spawn()?;\n", "x.rs"),
    "detached-with-startupinfo":W("Popen(c, creationflags=0x8 | 0x10, startupinfo=si)\n"),
    "node-detached": W("spawn(cmd, args, { detached: true, stdio: 'ignore' })\n", "x.js"),
    "short-hex": ("Edit", {"file_path": "missing.py", "old_string": "a", "new_string": "Popen(c, creationflags=0x8)"}),
    "decimal": W("Popen(c, creationflags=8)"),
    "leading-zero": W("Popen(c, creationflags=0008)"),
    "combined-literal": W("Popen(c, creationflags=0x08000208)"),
    "hash-in-string": W('Popen(["echo", " #1"], creationflags=8)\n'),
    "uppercase-ext": W("Popen(c, creationflags=0x8)\n", "X.PY"),
    "backslash-path": W("Popen(c, creationflags=0x8)\n", "C:\\work\\x.py"),
    "multiedit": ("MultiEdit", {"file_path": "missing.py", "edits": [{"old_string": "a", "new_string": "ok = 1"},
                                                                     {"old_string": "b", "new_string": "Popen(c, creationflags=0x00000008)"}]}),
    "bash-inline": ("Bash", {"command": "python -c \"import subprocess;subprocess.Popen(['x'],creationflags=0x00000008)\""}),
    "bash-heredoc-python": ("Bash", {"command": "python - <<'EOF'\nimport subprocess\nsubprocess.Popen(['x'], creationflags=0x8)\nEOF"}),
    "bash-heredoc-to-file": ("Bash", {"command": "cat > drive.py <<'EOF'\np = Popen(c, creationflags=NO_WINDOW | 0x8)\nEOF"}),
    "bash-list-command": ("Bash", {"command": ["python", "-c", "Popen(c, creationflags=0x8)"]}),
    "powershell-inline": ("PowerShell", {"command": "python -c \"import subprocess as s; s.Popen(['x'], creationflags=s.DETACHED_PROCESS)\""}),
    "monitor": ("Monitor", {"command": "python -c \"from subprocess import *; Popen(['x'], creationflags=8)\""}),
    "notebook": ("NotebookEdit", {"notebook_path": "n.ipynb", "new_source": "Popen(c, creationflags=DETACHED_PROCESS)"}),
    "same-name-elsewhere": W(INCIDENT, "C:/x/other/test_no_detached_launch.py"),
    # re-review (R2-R11)
    "usage-constant-then-code": W('USAGE = """run it\n"""\np = Popen(c, creationflags=0x8)\ndef main():\n    """Entry."""\n'),
    "echo-to-code-file": ("Bash", {"command": 'echo "p = Popen(c, creationflags=0x8)" > drive.py'}),
    "printf-append-code-file": ("Bash", {"command": "printf '%s\\n' 'flags |= subprocess.DETACHED_PROCESS' >> x.py"}),
    "ps-herestring-set-content": ("PowerShell", {"command": "@'\nPopen(c, creationflags=0x8)\n'@ | Set-Content drive.py"}),
    "ps-herestring-python": ("PowerShell", {"command": "@'\nimport subprocess\nsubprocess.Popen(['x'], creationflags=subprocess.DETACHED_PROCESS)\n'@ | python -"}),
    "bash-c-nested": ("Bash", {"command": "bash -c \"python -c 'import subprocess as s; s.Popen([1], creationflags=s.DETACHED_PROCESS)'\""}),
    "cmd-c-nested": ("PowerShell", {"command": "cmd /c \"python -c \\\"Popen(c, creationflags=0x8)\\\"\""}),
    "inline-single-quote": ("Bash", {"command": "python -c 'Popen(c, creationflags=0x8)'"}),
    "node-dot-detached": W("opts.detached = true;\nspawn(cmd, args, opts);\n", "x.js"),
    "node-json-detached": W('spawn(cmd, args, {"detached": true});\n', "x.js"),
    "c-define": W("#define FLAGS (NO_WINDOW | DETACHED_PROCESS)\n", "x.c"),
    "js-glob-string":W('globby("src/**/*.js");\nspawn(cmd, args, {detached: true});\n/** doc */\n', "x.js"),
    "sh-glob": W('cp build/* out/\nnode -e "spawn(c, {detached: true})"\nls */\n', "x.sh"),
    "python-floor-div-then-flag": W("half = n // 2; p = Popen(c, creationflags=0x8)\n"),
    "printf-multiline-to-code": ("Bash", {"command": "printf '%s' 'import subprocess\nsubprocess.Popen(c, creationflags=0x8)' > x.py"}),
    "pwsh-option-values": ("PowerShell", {"command": "pwsh -NoProfile -ExecutionPolicy Bypass -Command \"python -c 'import subprocess as s; s.Popen([1], creationflags=s.DETACHED_PROCESS)'\""}),
    "python-x-utf8": ("Bash", {"command": 'python -X utf8 -c "Popen(c, creationflags=8)"'}),
    "bash-lc": ("Bash", {"command": "bash -lc \"python -c 'import subprocess as s; s.Popen([1], creationflags=s.DETACHED_PROCESS)'\""}),
    # X4 port review (2026-11-24): quoted / variable interpreters and code on stdin
    # review of a256f41: positional fallback, keyword before a group, Go
    "var-python-heredoc": ("Bash", {"command": "\"$PY\" - <<'EOF'\nimport subprocess\nsubprocess.Popen(['x'], creationflags=8)\nEOF"}),
    "getattr-fallback":W('Popen(c, creationflags=getattr(subprocess, "DETACHED_PROCESS", 0x00000008))\n'),
    "getattr-fallback-or": W('Popen(c, creationflags=NO_WINDOW | getattr(subprocess, "DETACHED_PROCESS", 8))\n'),
    "keyword-else-group": W("Popen(c, creationflags=NO_WINDOW if quiet else (0x200 | 0x8))\n"),
    "go-sysprocattr": W("cmd.SysProcAttr = &syscall.SysProcAttr{CreationFlags: windows.DETACHED_PROCESS}\n", "x.go"),
    "or-in-parens":W("flags = (DETACHED_PROCESS | GROUP)\n"),
    "quoted-path-interp":("Bash", {"command": '"C:/Program Files/Python313/python.exe" -c "Popen(c, creationflags=8)"'}),
    "dq-var-interp": ("Bash", {"command": '"$PY" -c "Popen(c, creationflags=8)"'}),
    "bare-var-interp": ("Bash", {"command": '$PY -c "Popen(c, creationflags=8)"'}),
    "braced-var-interp": ("Bash", {"command": '${X4_PY} -B -c "Popen(c, creationflags=8)"'}),
    "variable-interp-in-loop": ("Bash", {"command": 'for f in x; do $cmd -c "creationflags=0x8"; done'}),
    "ps-amp-path": ("PowerShell", {"command": '& "C:\\Python313\\python.exe" -c "Popen(c, creationflags=8)"'}),
    "ps-amp-var": ("PowerShell", {"command": '& $py -c "Popen(c, creationflags=8)"'}),
    "echo-pipe-python": ("Bash", {"command": 'echo "Popen(c, creationflags=8)" | python -'}),
    "here3-python": ("Bash", {"command": 'python <<< "Popen(c, creationflags=8)"'}),
    "ps-string-pipe-python": ("PowerShell", {"command": "'Popen(c, creationflags=8)' | python -"}),
    "node-p": ("Bash", {"command": 'node -p "spawn(c, {detached: true})"'}),
    "bash-lc-writes-code":("Bash", {"command": "bash -lc \"echo 'Popen(c, creationflags=8)' > x.py\""}),
    "js-slashes-in-string": W('spawn(" //x", {detached: true});\n', "x.js"),
    "spawn-only-use":W("spawn(cmd, DETACHED_PROCESS)\n", "x.js"),
    "leading-or-join": W("var creationflags = NO_WINDOW\n    | 0x8;\n", "x.cs"),
    "backslash-join": W("x = dict(a=1)\ncreationflags = NO_WINDOW \\\n    + 0x8\n"),
}

ALLOW = {
    "prose-docstring": W('"""Windows: a hidden console, never CREATE_NO_WINDOW | DETACHED_PROCESS."""\nx = 1\n'),
    "string-mention": W('msg = "never use DETACHED_PROCESS"\n'),
    # user 2026-11-24: deliberately visible terminal work is allowed; only detaching (the spam) is refused
    "visible-new-console": W("Popen(c, creationflags=subprocess.CREATE_NEW_CONSOLE)\n"),
    "visible-new-console-literal": W("Popen(c, creationflags=0x10)\n"),
    "visible-console-startupinfo-none": W("Popen(c, creationflags=CREATE_NEW_CONSOLE, startupinfo=None)\n"),
    "start-process-visible": ("PowerShell", {"command": "Start-Process pwsh -ArgumentList '-NoExit','-Command','Get-Content log.txt -Wait'"}),
    "hidden-new-console": W("si.wShowWindow = SW_HIDE\np = subprocess.run(c, creationflags=subprocess.CREATE_NEW_CONSOLE, startupinfo=si)\n"),
    "hidden-new-console-literal": W("si.wShowWindow = win32con.SW_HIDE\np = subprocess.run(c, creationflags=0x10, startupinfo=si)\n"),
    "multiline-prose-string": W('X = """\nNO_WINDOW | DETACHED_PROCESS\n"""\n'),
    "string-holds-flags": W('raise SystemExit("never use creationflags=0x8")\n'),
    "inline-print-flags": ("Bash", {"command": "python -c \"print('was creationflags=8')\""}),
    "bracket-string-in-value": W('p = Popen(c, creationflags=pick("(", 0x200), timeout=8)\n'),
    "grep-c-after-python": ("Bash", {"command": 'python -m pytest -q; grep -c "DETACHED_PROCESS|creationflags=8" tools/x.py'}),
    "sed-e-after-py": ("Bash", {"command": "py -3 a.py | sed -e 's/creationflags=0x8 | DETACHED_PROCESS//'"}),
    "js-string-holds-use": W('const msg = "flags = DETACHED_PROCESS was the bug";\n', "x.js"),
    "python-heredoc-prose":("Bash", {"command": "python - <<'EOF'\nrep('''Scope: never creationflags=NO_WINDOW | 0x8,\nflags = DETACHED_PROCESS is the bug''')\nEOF"}),
    "echo-pipe-python-prose": ("Bash", {"command": "echo \"print('''flags = DETACHED_PROCESS''')\" | python -"}),
    # real false positive of the deployed ac71e24 hook (2026-11-24): nested "$(git -C "..." ...)" quoting before a
    # commit message that quotes the flag's spelling with \" escapes and mentions <<EOF
    "commit-msg-nested-quotes": ("Bash", {"command": r'''cd .. && git add -A x && git -c user.email="$(git -C "/c/G G/S" config user.email)" commit -q -m "fix

A1 getattr(subprocess, \"DETACHED_PROCESS\", 0x8) fallback was blanked; A2 else (A | 0x8) follows; A3 any \$VAR
as the command word; \"\$PY\" - <<EOF still counts, cat > \"\$NOTES\" <<EOF does not)." && git push -q'''}),
    "heredoc-to-var-notes":("Bash", {"command": "cat > \"$NOTES\" <<'EOF'\ndrop creationflags=NO_WINDOW | DETACHED_PROCESS\nEOF"}),
    "ps-herestring-to-var": ("PowerShell", {"command": "@'\ndrop creationflags=NO_WINDOW | DETACHED_PROCESS\n'@ | Set-Content $notesPath"}),
    "rg-var-pattern": ("Bash", {"command": 'rg "$PAT" -e "creationflags=8" tools'}),
    "git-var-c": ("Bash",{"command": '$GIT -c "user.name=x" commit -m "y"'}),
    # X4 port review (2026-11-24): false positives that are not launches
    "assert-flag-absent": W("assert not (kw['creationflags'] & subprocess.DETACHED_PROCESS)\n"),
    "constants-table": W("DETACHED_PROCESS = 0x00000008\nCREATE_NO_WINDOW = 0x08000000\n"),
    "blocklist-tuple": W("BANNED = (subprocess.DETACHED_PROCESS, 'x')\n"),
    "flag-from-table-index": W("p = Popen(c, creationflags=table[8])\n"),
    "flag-from-call-arg": W("p = Popen(c, creationflags=pick(level=8))\n"),
    "rg-unquoted": ("Bash", {"command": "rg -n creationflags=8 ."}),
    "fstring-prose": W('HELP = f"""\nUse NO_WINDOW | {GROUP}, never DETACHED_PROCESS | creationflags=0x8\n"""\n'),
    "write-then-commit": ("Bash", {"command": 'git show HEAD~1:tools/v.py > old.py && git commit -m "drop creationflags=0x8"'}),
    "write-then-search": ("Bash", {"command": 'git show HEAD~1:tools/v.py > old.py && rg "DETACHED_PROCESS|creationflags=8" old.py'}),
    "python-floor-div": W("half = n // 2\nPopen(c, creationflags=NO_WINDOW)\n"),
    "js-block-comment":W("/* old: spawn(c, {detached: true}) */\nspawn(c, {windowsHide: true});\n", "x.js"),
    "js-bracket-in-string": W('let o = "(";\nconsole.log(DETACHED_PROCESS);\nspawn(c);\n', "x.js"),
    "unbalanced-regex-literal": W("let r = /[(]/;\n" + "let z = 1;\n" * 60 + "console.log(DETACHED_PROCESS);\nspawn(c);\n", "x.js"),
    "definition-then-launch": W("win32con.DETACHED_PROCESS = 0x8; Popen(c)\n"),
    "js-template-in-call": W("run(`\ncreationflags: NO_WINDOW\nretries: 8\n`);\n", "x.js"),
    "ps-herestring-notes":("PowerShell", {"command": "@'\ncreationflags=0x8 was the bug\n'@ | Set-Content notes.md"}),
    "echo-to-notes":("Bash", {"command": 'echo "creationflags=0x8 was the bug" > notes.md'}),
    "multiline-commit": ("Bash", {"command": "git commit -m 'fix\n\nflags = DETACHED_PROCESS was the bug'"}),
    "flag-then-other-kwargs":W("p = subprocess.run(cmd, capture_output=True, creationflags=0x08000000, timeout=120)\n"),
    "value-ends-at-line": W('options = dict(\n    x=1)\nif nt:\n    options["creationflags"] = NO_WINDOW\nelse:\n    tail = 64 * 1024 + 8\n'),
    "code-in-long-string": W("patch('''\nif nt:\n    options[\"creationflags\"] = NO_WINDOW\ntail = 64 * 1024 + 8\n''')\n"),
    "bracket-in-string": W('opener = "("\nprint(subprocess.DETACHED_PROCESS)\nx = 1\n'),
    "name-read-not-used":W("print(subprocess.DETACHED_PROCESS)\n"),
    "token-compare":W('if tok.string == "DETACHED_PROCESS":\n    bad = True\n'),
    "removes-flag-name": W("Popen(c, creationflags=flags & ~subprocess.DETACHED_PROCESS)\n"),
    "removes-flag-literal": W("Popen(c, creationflags=flags & ~0x8)\n"),
    "grep-search": ("Bash", {"command": r'grep -rn "0x00000008\|DETACHED_PROCESS" tools --include=*.py'}),
    "rg-search": ("Bash", {"command": 'rg -n "DETACHED_PROCESS|creationflags" tools'}),
    "select-string": ("PowerShell", {"command": "Select-String -Pattern 'DETACHED_PROCESS|CREATE_NO_WINDOW' *.py"}),
    "git-commit": ("Bash", {"command": 'git commit -m "drop DETACHED_PROCESS: was creationflags=0x8"'}),
    "docstring-flags": W('def f():\n    """Was creationflags=0x8 once; now hidden."""\n    return 1\n'),
    "heredoc-to-notes": ("Bash", {"command": "cat > notes.md <<'EOF'\ncreationflags=NO_WINDOW | DETACHED_PROCESS\nEOF"}),
    "markdown-note": W(INCIDENT, "notes.md"),
    "own-test-file": W(INCIDENT, "C:\\x\\tests\\test_detached_launch.py"),
    "own-detector": W(INCIDENT, "C:/x/tools/skyguard/detached.py"),
    "hidden-only": W("Popen(c, creationflags=0x08000000)\n"),
    "hidden-group": W("Popen(c, creationflags=NO_WINDOW | 0x00000200)\n"),
    "named-no-window": W("Popen(c, creationflags=subprocess.CREATE_NO_WINDOW)\n"),
    "the-fix-comment": W("    creationflags=NO_WINDOW | 0x00000200)  # hidden console, new group. NEVER detached (0x8):\n"),
    "comment-holds-old-flags": W("x = Popen(c, creationflags=NO_WINDOW)  # was creationflags=NO_WINDOW | 0x00000008\n"),
    "comment-slash": W("x = spawn(c, {windowsHide: true}); // was detached: true\n", "x.js"),
    "comment-names-it": W("# never use DETACHED_PROCESS here\nx = 1\n"),
    "batch-rem": W("REM creationflags=8 DETACHED_PROCESS = bad\r\n:: Popen(c, creationflags=8)\r\necho ok\r\n", "x.bat"),
    "unrelated-0x8": W("caps = 0x1000 | 0x400000 | 0x8\n"),
    "huge-literal": W("Popen(c, creationflags=0x100000008)\n"),
    "plain-bash": ("Bash", {"command": "git status"}),
    "read-tool": ("Read", {"file_path": "x.py"}),
}


@pytest.mark.parametrize("name", sorted(DENY))
def test_denies(name):
    assert decide(*DENY[name]) == "deny", name


@pytest.mark.parametrize("name", sorted(ALLOW))
def test_allows(name):
    assert decide(*ALLOW[name]) == "allow", name


def test_small_edit_judged_in_context(tmp_path):
    f = tmp_path / "drive.py"
    f.write_text("p = subprocess.Popen(cmd,\n    creationflags=NO_WINDOW | 0x00000200)\n", encoding="utf-8")
    edit = {"file_path": str(f), "old_string": "NO_WINDOW | 0x00000200)", "new_string": "NO_WINDOW | 0x00000200 | 0x00000008)"}
    assert decide("Edit", edit) == "deny"
    harmless = {"file_path": str(f), "old_string": "cmd,", "new_string": "cmd2,"}
    assert decide("Edit", harmless) == "allow"


def test_edit_elsewhere_in_a_file_with_an_old_bad_line_is_allowed(tmp_path):
    f = tmp_path / "old.py"
    f.write_text("a = Popen(c, creationflags=0x8)\n\n\n\ndef other():\n    return 1\n", encoding="utf-8")
    assert decide("Edit", {"file_path": str(f), "old_string": "return 1", "new_string": "return 2"}) == "allow"


def test_edit_below_a_docstring(tmp_path):
    f = tmp_path / "drive.py"
    f.write_text('"""Driver for the headless run.\n\nMore text.\n"""\nimport subprocess\n'
                 "p = subprocess.Popen(cmd,\n    creationflags=NO_WINDOW | 0x00000200)\n", encoding="utf-8")
    edit = {"file_path": str(f), "old_string": "NO_WINDOW | 0x00000200)", "new_string": "NO_WINDOW | 0x00000200 | 0x00000008)"}
    assert decide("Edit", edit) == "deny"


def test_replace_all_judges_every_site(tmp_path):
    f = tmp_path / "x.py"
    f.write_text("GROUP = 0x00000200\n\n\n\np = Popen(c, creationflags=NO_WINDOW | 0x00000200)\n", encoding="utf-8")
    edit = {"file_path": str(f), "old_string": "0x00000200", "new_string": "0x00000208", "replace_all": True}
    assert decide("Edit", edit) == "deny"


def test_crlf_file_multiline_old_string(tmp_path):
    f = tmp_path / "x.py"
    f.write_bytes(b"p = subprocess.Popen(cmd,\r\n    creationflags=NO_WINDOW\r\n    | GROUP)\r\nq = 1\r\n")
    # the new text alone holds no creationflags: only judging it IN the file (LF-normalised) sees the bit
    edit = {"file_path": str(f), "old_string": "NO_WINDOW\n    | GROUP)", "new_string": "NO_WINDOW\n    | GROUP | 8)"}
    assert decide("Edit", edit) == "deny"
    harmless = {"file_path": str(f), "old_string": "    | GROUP)\nq = 1", "new_string": "    | GROUP)\nq = 2"}
    assert decide("Edit", harmless) == "allow"


def test_multiedit_applies_in_order(tmp_path):
    f = tmp_path / "x.py"
    f.write_text("p = Popen(c,\n    creationflags=NO_WINDOW)\n", encoding="utf-8")
    edits = [{"old_string": "NO_WINDOW)", "new_string": "NO_WINDOW | GROUP)"},
             {"old_string": "GROUP)", "new_string": "GROUP | 0x8)"}]
    assert decide("MultiEdit", {"file_path": str(f), "edits": edits}) == "deny"


def test_docstring_edit_in_a_big_file(tmp_path):
    body = ["import subprocess", "", "def outer():", "    if True:", "        x = 1"]
    body += ["        y = %d" % i for i in range(300)]
    body += ["    z = 2"]  # a window starting inside the 8-space block cannot tokenize this dedent
    body += ["", "def later():", '    """Notes.', "", "    never NO_WINDOW | DETACHED_PROCESS here", '    """', "    return 1", ""]
    f = tmp_path / "big.py"
    f.write_text("\n".join(body), encoding="utf-8")
    edit = {"file_path": str(f), "old_string": "    never NO_WINDOW | DETACHED_PROCESS here",
            "new_string": "    never NO_WINDOW | DETACHED_PROCESS anywhere"}
    assert decide("Edit", edit) == "allow"


def test_multiedit_after_replace_all_keeps_spans(tmp_path):
    f = tmp_path / "x.py"
    f.write_text("foo\nfoo\nfoo\nfoo\nfoo\n\n\n\np = Popen(c,\n    creationflags=NO_WINDOW)\n", encoding="utf-8")
    edits = [{"old_string": "NO_WINDOW)", "new_string": "NO_WINDOW | 0x8)"},
             {"old_string": "foo", "new_string": "x" * 200, "replace_all": True}]
    assert decide("MultiEdit", {"file_path": str(f), "edits": edits}) == "deny"


def test_unbalanced_comment_does_not_glue_the_file(tmp_path):
    f = tmp_path / "x.js"
    f.write_text("/**\n * see (below\n */\nfunction a() {}\n" + "let z = 1;\n" * 60 + "spawn(c, {detached: true});\n",
                 encoding="utf-8")
    edit = {"file_path": str(f), "old_string": "function a() {}", "new_string": "function a() { return 1; }"}
    assert decide("Edit", edit) == "allow"


# ---- the wiring: every agent's entry point reaches the detector ---------------------------------------------------

def _bash():
    from test_hooks import _bash as b
    return b()


def _claude(tmp_path, hook, payload):
    root = install_hooks(tmp_path / "install", names={hook})
    payload = dict(payload, cwd=str(root))
    r = subprocess.run([_bash(), str(root / ".claude" / "hooks" / hook)], input=json.dumps(payload),
                       capture_output=True, text=True, env=dict(os.environ, CLAUDE_PROJECT_DIR=str(root)), timeout=120)
    h = json.loads(r.stdout).get("hookSpecificOutput") if r.stdout.strip() else None
    return (h or {}).get("permissionDecision", "allow"), (h or {}).get("permissionDecisionReason", "")


def test_claude_code_refuses_a_detached_write_through_its_hook(tmp_path):
    decision, reason = _claude(tmp_path, "protect-files.sh",
                               {"tool_name": "Write", "tool_input": {"file_path": str(tmp_path / "drive.py"),
                                                                     "content": INCIDENT}})
    assert decision == "deny" and "DETACHED" in reason and "CREATE_NO_WINDOW" in reason


def test_claude_code_allows_the_hidden_launch_through_its_hook(tmp_path):
    decision, _ = _claude(tmp_path, "protect-files.sh",
                          {"tool_name": "Write", "tool_input": {"file_path": str(tmp_path / "drive.py"),
                                                                "content": "Popen(c, creationflags=0x08000200)\n"}})
    assert decision == "allow"


def test_claude_code_refuses_a_detached_inline_script_through_its_hook(tmp_path):
    decision, reason = _claude(tmp_path, "protect-bash.sh", {"tool_name": "Bash", "tool_input": {
        "command": 'python -c "import subprocess; subprocess.Popen(c, creationflags=0x8)"'}})
    assert decision == "deny" and "DETACHED" in reason


@pytest.fixture()
def game(tmp_path):
    g = tmp_path / "Game"
    (g / "Data").mkdir(parents=True)
    (g / "SkyrimVR.exe").write_bytes(b"e")
    return g


def _codex(game, tool, tool_input):
    g = game.as_posix()
    req = codex.to_request(json.dumps({"hook_event_name": "PreToolUse", "tool_name": tool, "cwd": g,
                                       "tool_input": tool_input}), "win32")
    audit = Audit(g)
    v = engine.pre_tool(req, g, lambda: paths.roots(g, "", {}), audit)
    return v, {rid for rid, _ in audit.fired}


PATCH = "*** Begin Patch\n*** Add File: drive.py\n+p = Popen(c, creationflags=0x00000008)\n*** End Patch"


def test_codex_refuses_a_detached_patch(game):
    v, fired = _codex(game, "apply_patch", {"command": PATCH})
    assert v.decision == "deny" and "W-NO-DETACHED-LAUNCH" in fired


def test_codex_refuses_a_detached_patch_on_an_updated_file(game):
    patch = ("*** Begin Patch\n*** Update File: drive.py\n@@\n p = Popen(c,\n-    creationflags=NO_WINDOW)\n"
             "+    creationflags=NO_WINDOW | DETACHED_PROCESS)\n*** End Patch")
    v, fired = _codex(game, "apply_patch", {"command": patch})
    assert v.decision == "deny" and "W-NO-DETACHED-LAUNCH" in fired


def test_codex_allows_a_patch_that_removes_the_flag(game):
    # the removed and the added line are one statement: a removed line judged as still there would refuse it
    patch = ("*** Begin Patch\n*** Update File: drive.py\n@@\n p = Popen(c,\n"
             "-    creationflags=NO_WINDOW | DETACHED_PROCESS,\n+    creationflags=NO_WINDOW,\n     cwd=x)\n*** End Patch")
    v, fired = _codex(game, "apply_patch", {"command": patch})
    assert "W-NO-DETACHED-LAUNCH" not in fired


def test_codex_refuses_a_detached_shell_command(game):
    v, fired = _codex(game, "shell", {"command": ["bash", "-lc", "node -e \"spawn(c, a, {detached: true})\""]})
    assert v.decision == "deny" and "S-NO-DETACHED-LAUNCH" in fired


def _opencode(game, tool, args):
    data = {"v": 1, "tool": tool, "args": args, "directory": str(game)}
    if tool == "bash":
        data["shell"] = "bash"                     # OpenCode's transport names the shell that runs the command
    return opencode.handle(json.dumps(data), game)


def test_opencode_refuses_a_detached_write(game):
    got = _opencode(game, "write", {"filePath": str(game / "drive.py"), "content": INCIDENT})
    assert got["decision"] == "deny" and "DETACHED" in got["reason"]


def test_opencode_refuses_a_detached_edit_in_context(game):
    f = game / "drive.py"
    f.write_text("p = subprocess.Popen(cmd,\n    creationflags=NO_WINDOW | 0x00000200)\n", encoding="utf-8")
    got = _opencode(game, "edit", {"filePath": str(f), "oldString": "NO_WINDOW | 0x00000200)",
                                   "newString": "NO_WINDOW | 0x00000200 | 0x00000008)"})
    assert got["decision"] == "deny" and "DETACHED" in got["reason"]


def test_opencode_refuses_a_detached_patch(game):
    got = _opencode(game, "apply_patch", {"patchText": PATCH})
    assert got["decision"] == "deny" and "DETACHED" in got["reason"]


def test_opencode_refuses_a_detached_command(game):
    got = _opencode(game, "bash", {"command": 'python -c "Popen(c, creationflags=subprocess.DETACHED_PROCESS)"'})
    assert got["decision"] == "deny" and "DETACHED" in got["reason"]


def test_opencode_allows_a_visible_new_console(game):
    got = _opencode(game, "write", {"filePath": str(game / "drive.py"),
                                    "content": "Popen(c, creationflags=subprocess.CREATE_NEW_CONSOLE)\n"})
    assert got["decision"] != "deny"


def test_a_detector_failure_allows_the_call(game, monkeypatch):
    """Fail open: a bug in the detector must not refuse every edit (the rule prevents pop-ups, preserves nothing)."""
    def boom(*_a, **_k):
        raise RuntimeError("detector bug")
    monkeypatch.setattr(detached, "judge", boom)
    monkeypatch.setattr(detached, "judge_shell", boom)
    got = _opencode(game, "write", {"filePath": str(game / "drive.py"), "content": INCIDENT})
    assert got["decision"] != "deny"
    got = _opencode(game, "bash", {"command": 'python -c "Popen(c, creationflags=0x8)"'})
    assert got["decision"] != "deny"
    log = (game / ".claude" / "backups" / "AUDIT_LOG.txt").read_text(encoding="utf-8", errors="replace")
    assert log.count("no-detached-launch check raised RuntimeError") == 2, "a failed check must leave a trace"


def test_a_sentinel_flag_value_without_the_detached_bit_is_allowed():
    """The narrowed scope (user ruling 2026-11-24): only bit 0x8. 0x1234 has 0x10 (a new console) and 0x4, not 0x8."""
    assert decide("Write", {"file_path": "x.py", "content": "Popen(c, creationflags=0x1234)\n"}) == "allow"


def test_a_non_code_file_is_never_judged():
    assert decide("Write", {"file_path": "notes.md", "content": INCIDENT}) == "allow"


def test_opencode_resolves_a_relative_edit_path_against_its_folder(game):
    """The new text alone holds no creationflags: only the file it lands in (found through the folder) shows the bit."""
    (game / "drive.py").write_text("p = subprocess.Popen(cmd,\n    creationflags=NO_WINDOW | 0x00000200)\n",
                                   encoding="utf-8")
    got = _opencode(game, "edit", {"filePath": "drive.py", "oldString": "NO_WINDOW | 0x00000200)",
                                   "newString": "NO_WINDOW | 0x00000200 | 0x00000008)"})
    assert got["decision"] == "deny" and "DETACHED" in got["reason"]


def test_codex_refuses_a_detached_patch_run_through_the_shell(game):
    # a plain command string (no -c script, so only the patch reader sees the added line), with its workdir
    v, fired = _codex(game, "shell", {"command": "apply_patch <<'EOF'\n" + PATCH + "\nEOF\n", "workdir": game.as_posix()})
    assert v.decision == "deny" and "S-NO-DETACHED-LAUNCH" in fired


# ---- release review fixes (2026-11-24) ---------------------------------------------------------------------------

def test_the_two_rules_have_their_own_codes(game):
    _v, fired = _codex(game, "apply_patch", {"command": PATCH})
    v, _ = _codex(game, "apply_patch", {"command": PATCH})
    assert v.code == "write.W-NO-DETACHED-LAUNCH"
    v, _ = _codex(game, "shell", {"command": ["bash", "-lc", 'node -e "spawn(c, a, {detached: true})"']})
    assert v.code == "shell.S-NO-DETACHED-LAUNCH"


@pytest.mark.parametrize("argv", [
    ["python", "-c", "import subprocess as s; s.Popen(['x'], creationflags=8)"],
    ["pwsh", "-NoProfile", "-Command", "python -c \"import subprocess as s; s.Popen(['x'], creationflags=8)\""],
])
def test_codex_argv_scripts_with_quotes_are_judged(game, argv):
    """Codex quotes each argv element ('"'"' in bash, '' in PowerShell); the script after -c is still read."""
    v, fired = _codex(game, "shell", {"command": argv})
    assert v.decision == "deny" and "S-NO-DETACHED-LAUNCH" in fired


@pytest.mark.parametrize("cmd", [
    'timeout 60 "$PY" -c "Popen(c, creationflags=8)"',
    'env FOO=1 "$PY" -c "Popen(c, creationflags=8)"',
    'PYTHONUTF8=1 "$PY" -c "Popen(c, creationflags=8)"',
    'nohup "$PY" -c "Popen(c, creationflags=8)"',
    '"$PY" "${X4_PYI[@]}" -c "Popen(c, creationflags=8)"',
    '"${PY:-python}" -c "Popen(c, creationflags=8)"',
    "python -c 'import subprocess as s; s.Popen(['\"'\"'x'\"'\"'], creationflags=8)'",
])
def test_an_interpreter_after_a_prefix_or_as_a_variable_is_seen(cmd):
    assert detached.judge_words(cmd, "bash")


@pytest.mark.parametrize("cmd", [
    '& "$env:PY" -c "Popen(c, creationflags=8)"',
    "& ${env:PY} -c 'Popen([''x''], creationflags=8)'",
])
def test_a_powershell_variable_interpreter_is_seen(cmd):
    assert detached.judge_words(cmd, "powershell")


@pytest.mark.parametrize("cmd", [
    'timeout 60 "$PY" -c "Popen(c, creationflags=0x08000200)"',
    "echo python -c 'creationflags=8'",
    "git commit -m 'creationflags=8 is refused now'",
    'rg "creationflags=8" tools',
])
def test_words_allow_the_hidden_launch_and_mentions(cmd):
    assert not detached.judge_words(cmd, "bash")


@pytest.mark.parametrize("header", ["  *** Add File: drive.py", "*** Add File:drive.py"])
def test_patch_headers_the_appliers_accept_are_read(header):
    patch = "*** Begin Patch\n" + header + "\n+p = Popen(c, creationflags=0x8)\n*** End Patch\n"
    assert detached.judge_patch(patch)


def test_an_updated_file_is_judged_in_the_file(tmp_path):
    """The hunk alone holds no creationflags; the file it lands in does (as an Edit is judged)."""
    f = tmp_path / "drive.py"
    f.write_text("p = subprocess.Popen(cmd,\n    creationflags=NO_WINDOW\n    | GROUP)\nq = 1\n", encoding="utf-8")
    patch = "*** Begin Patch\n*** Update File: drive.py\n@@\n-    | GROUP)\n+    | GROUP | 8)\n q = 1\n*** End Patch\n"
    assert detached.judge_patch(patch, str(tmp_path))
    harmless = "*** Begin Patch\n*** Update File: drive.py\n@@\n-q = 1\n+q = 2\n*** End Patch\n"
    assert not detached.judge_patch(harmless, str(tmp_path))


def test_a_docstring_hunk_is_judged_in_the_file_not_alone(tmp_path):
    f = tmp_path / "notes.py"
    f.write_text('def later():\n    """Notes.\n\n    never NO_WINDOW | DETACHED_PROCESS here\n    """\n    return 1\n',
                 encoding="utf-8")
    patch = ("*** Begin Patch\n*** Update File: notes.py\n@@\n-    never NO_WINDOW | DETACHED_PROCESS here\n"
             "+    never NO_WINDOW | DETACHED_PROCESS anywhere\n*** End Patch\n")
    assert not detached.judge_patch(patch, str(tmp_path))


def test_opencode_takes_the_path_its_transport_resolved(game):
    """An MSYS spelling (/c/...) is normalised by the transport; the in-context read uses that path."""
    f = game / "drive.py"
    f.write_text("p = subprocess.Popen(cmd,\n    creationflags=NO_WINDOW | 0x00000200)\n", encoding="utf-8")
    p = f.as_posix()
    if os.name != "nt" or p[1] != ":":
        pytest.skip("an MSYS drive spelling exists on Windows only")
    msys = "/" + p[0].lower() + p[2:]
    got = _opencode(game, "edit", {"filePath": msys, "oldString": "NO_WINDOW | 0x00000200)",
                                   "newString": "NO_WINDOW | 0x00000200 | 0x00000008)"})
    assert got["decision"] == "deny" and "DETACHED" in got["reason"]


def test_a_command_that_only_mentions_a_patch_is_not_judged_as_one(game):
    msg = "*** Begin Patch\n*** Add File: drive.py\n+p = Popen(c, creationflags=8)\n*** End Patch"
    v, fired = _codex(game, "shell", {"command": ["git", "commit", "-m", msg], "workdir": game.as_posix()})
    assert "S-NO-DETACHED-LAUNCH" not in fired


def test_an_existing_refusal_keeps_its_own_rule(game):
    """A call another rule already refuses keeps that refusal's code; the detached check does not run on it."""
    target = (game / "Data").as_posix()
    v, fired = _codex(game, "shell", {"command": 'rm -rf "' + target + '"; python -c "Popen(c, creationflags=8)"',
                                      "workdir": game.as_posix()})
    assert v.decision == "deny" and "S-NO-DETACHED-LAUNCH" not in fired and fired


def test_a_4mb_write_is_judged_well_inside_the_engine_budget():
    import time
    big = "".join("def f%d(x):\n    return subprocess.run(['a'], cwd=x, creationflags=0x08000000)  # c\n" % i
                  for i in range(48000))
    assert len(big) > 4_000_000
    t0 = time.perf_counter()
    assert not detached.judge("Write", {"file_path": "x.py", "content": big})
    assert time.perf_counter() - t0 < 12, "the engine's whole budget is 20 s"


def test_a_powershell_doubled_quote_keeps_a_string_a_string():
    """'' inside '...' is one quote in PowerShell: the flag here is text in a Python string, not a launch."""
    assert not detached.judge_words("& $py -c 'print(''creationflags=8'')'", "powershell")


# ---- confirmation review fixes (2026-11-24) ----------------------------------------------------------------------

@pytest.mark.parametrize("cmd", [
    "cat >> notes.md <<'EOF'\n| python -c \"Popen(c, creationflags=8)\" | the bug |\nEOF\n",
    "git commit -F - <<EOF\nFix (python -c \"Popen(c, creationflags=8)\") so it doesn't pop up\nEOF\n",
])
def test_prose_in_a_heredoc_is_not_read_as_commands(cmd):
    assert not detached.judge_words(cmd, "bash")


@pytest.mark.parametrize("cmd,shell", [
    ("pwsh -Command \"python -c 'Popen([''x''], creationflags=8)'\"", "powershell"),
    ("bash -lc 'python -c '\"'\"'Popen([\"x\"], creationflags=8)'\"'\"''", "bash"),
])
def test_a_script_nested_in_a_shell_is_read(cmd, shell):
    assert detached.judge_words(cmd, shell)


def test_a_scripts_own_arguments_are_not_code():
    assert not detached.judge_words('python tools/find.py -e "DETACHED_PROCESS|creationflags=8"', "bash")
    assert detached.judge_words('python -X utf8 -c "Popen(c, creationflags=8)"', "bash")


def test_a_hunk_lands_after_its_anchor_when_the_context_repeats(tmp_path):
    f = tmp_path / "drive.py"
    f.write_text("def first():\n    p = Popen(c,\n        x)\n\n\ndef second():\n    q = Popen(c,\n"
                 "        creationflags=NO_WINDOW\n        x)\n", encoding="utf-8")
    patch = ("*** Begin Patch\n*** Update File: drive.py\n@@ def second():\n-        x)\n+        | 8, x)\n"
             "*** End Patch\n")
    assert detached.judge_patch(patch, str(tmp_path))


def test_a_pure_insertion_after_its_anchor_is_judged_in_the_file(tmp_path):
    f = tmp_path / "drive.py"
    f.write_text("p = Popen(c,\n    creationflags=NO_WINDOW\n    )\n", encoding="utf-8")
    patch = "*** Begin Patch\n*** Update File: drive.py\n@@     creationflags=NO_WINDOW\n+    | 8\n*** End Patch\n"
    assert detached.judge_patch(patch, str(tmp_path))


def test_many_hunks_on_a_big_file_stay_fast(tmp_path):
    import time
    f = tmp_path / "big.py"
    f.write_text("a\n" * 1_000_000, encoding="utf-8")
    patch = "*** Begin Patch\n*** Update File: big.py\n" + "@@\n a\n+b\n" * 100_000 + "*** End Patch\n"
    t0 = time.perf_counter()
    assert not detached.judge_patch(patch, str(tmp_path))
    assert time.perf_counter() - t0 < 12


def test_an_indented_header_inside_an_update_is_context():
    patch = ("*** Begin Patch\n*** Update File: drive.py\n@@\n  *** Delete File: x\n+p = Popen(c, creationflags=8)\n"
             "*** End Patch\n")
    assert detached.judge_patch(patch)


# ---- second confirmation review fixes (2026-11-24) ---------------------------------------------------------------

def test_a_shift_in_code_is_not_a_heredoc(game):
    """`a<<b` inside a quoted -c script is code, not a heredoc that would hide the next line."""
    v, fired = _codex(game, "shell", {"command": ["python", "-c", "v = a<<b\nsubprocess.Popen(['x'], creationflags=8)"]})
    assert v.decision == "deny" and "S-NO-DETACHED-LAUNCH" in fired


def test_a_command_after_a_heredoc_is_still_read():
    cmd = "cat > notes.md <<'EOF'\nprose with python -c \"creationflags=8\"\nEOF\npython -c 'Popen(c, creationflags=8)'\n"
    assert detached.judge_words(cmd, "bash")


def test_a_backslash_heredoc_body_is_data():
    assert not detached.judge_words("cat >> notes.md <<\\EOF\n| python -c \"creationflags=8\" |\nEOF\n", "bash")


def test_a_powershell_here_string_is_data():
    # the apostrophe in "it's" would end a plain quote early; only reading the here-string as one keeps it data
    cmd = "$note = @'\nit's the bug:\n| python -c \"creationflags=8\" |\n'@\nSet-Content notes.md $note"
    assert not detached.judge_words(cmd, "powershell")


@pytest.mark.parametrize("cmd", [
    'python /c/x/tools/find.py -e "DETACHED_PROCESS|creationflags=8"',
    'python "$T/find.py" -e "DETACHED_PROCESS|creationflags=8"',
    'python -m tools.find -e "DETACHED_PROCESS|creationflags=8"',
])
def test_a_script_path_or_module_ends_the_interpreter_options(cmd):
    assert not detached.judge_words(cmd, "bash")


def test_a_nested_powershell_body_is_read_with_powershell_quoting():
    cmd = "powershell.exe -Command \"python -c 'print(''creationflags=8'')'\""
    assert not detached.judge_words(cmd, "")


def test_only_cmd_takes_a_slash_option():
    assert detached._option("/d", True) and not detached._option("/c/x/find.py", False)
    assert detached._option("${X4_PYI[@]}", False) and not detached._option("$T/find.py", False)


def test_the_adversarial_patch_stays_inside_the_engine_budget(tmp_path):
    import time
    (tmp_path / "big.py").write_text("a\n" * 1_000_000, encoding="utf-8")
    patch = "*** Begin Patch\n*** Update File: big.py\n" + "@@\n a\n a\n-zz\n+b\n" * 1900 + "*** End Patch\n"
    t0 = time.perf_counter()
    detached.judge_patch(patch, str(tmp_path))
    assert time.perf_counter() - t0 < 15, "the engine's whole budget is 20 s"


@pytest.mark.parametrize("cmd", [
    "cat <<< \"x\"\npython -c 'Popen([\"x\"], creationflags=8)'\n",
    "echo $((flags<<shift))\npython -c 'Popen([\"x\"], creationflags=8)'\n",
])
def test_a_here_string_or_a_shift_does_not_hide_the_next_line(cmd):
    words = detached._words(cmd, "bash")
    assert "python" in words and detached.judge_words(cmd, "bash")
