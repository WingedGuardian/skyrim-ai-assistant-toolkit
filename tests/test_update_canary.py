"""The toolkit-update canary (tools/skyguard/update_canary.py): run after an update's setup, it must pass on a working
install and fail when a guard is broken, because a failure is what rolls the update back."""
import json
import os
import shutil
import subprocess
import sys

import pytest

from conftest import REPO, install_hooks

CANARY = REPO / "tools" / "skyguard" / "update_canary.py"


def toolkit(tmp_path, hooks=True, opencode=True):
    root = tmp_path / "Skyrim VR"
    (root / "Data").mkdir(parents=True)
    (root / "SkyrimVR.exe").write_bytes(b"")
    if hooks:
        install_hooks(root)
        shutil.copy(REPO / ".claude" / "settings.json", root / ".claude" / "settings.json")
    else:
        shutil.copytree(REPO / "tools" / "skyguard", root / "tools" / "skyguard",
                        ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copytree(REPO / "tools" / "modding_harness", root / "tools" / "modding_harness",
                        ignore=shutil.ignore_patterns("__pycache__"))
    if opencode:
        shutil.copytree(REPO / ".opencode" / "hooks", root / ".opencode" / "hooks",
                        ignore=shutil.ignore_patterns("__pycache__"))
    return root


def canary(root):
    done = subprocess.run([sys.executable, "-B", str(CANARY), "--root", str(root)], capture_output=True, text=True,
                          timeout=600)
    return done.returncode, done.stdout


def test_a_working_install_passes(tmp_path):
    code, out = canary(toolkit(tmp_path))
    assert code == 0, out
    assert "ok    Claude Code refuses a raw plugin write" in out
    assert "ok    Claude Code allows a harmless command" in out
    assert "ok    OpenCode refuses a raw plugin write" in out
    assert "ok    OpenCode allows a harmless command" in out


def test_a_guard_that_allows_everything_fails(tmp_path):
    root = toolkit(tmp_path)
    (root / ".claude" / "hooks" / "protect-files.sh").write_text("#!/bin/bash\nexit 0\n", newline="\n")
    code, out = canary(root)
    assert code == 1 and "FAIL  Claude Code refuses a raw plugin write" in out, out


def test_a_guard_that_refuses_everything_fails(tmp_path):
    root = toolkit(tmp_path)
    (root / ".claude" / "hooks" / "protect-bash.sh").write_text(
        "#!/bin/bash\necho '{\"hookSpecificOutput\":{\"hookEventName\":\"PreToolUse\",\"permissionDecision\":\"deny\"}}'\n",
        newline="\n")
    code, out = canary(root)
    assert code == 1 and "FAIL  Claude Code allows a harmless command" in out, out


def test_a_claude_hook_can_allow_with_json(tmp_path):
    root = toolkit(tmp_path, opencode=False)
    (root / ".claude" / "hooks" / "protect-bash.sh").write_text(
        "#!/bin/bash\necho '{\"hookSpecificOutput\":{\"hookEventName\":\"PreToolUse\",\"permissionDecision\":\"allow\"}}'\n",
        newline="\n")
    code, out = canary(root)
    assert code == 0 and "ok    Claude Code allows a harmless command" in out, out


@pytest.mark.parametrize("program", [
    "print('SKYGUARD-OPENCODE {\"v\": 1, \"decision\": \"allow\"}')",
    "raise SystemExit(2)",
    "print('{\"decision\": \"deny\"}')",
    "print('debug\\nSKYGUARD-OPENCODE {\"decision\": \"deny\"}')",
    "print('SKYGUARD-OPENCODE {\"decision\": \"deny\", \"inert\": true}')",
])
def test_a_broken_opencode_adapter_fails(tmp_path, program):
    root = toolkit(tmp_path, hooks=False)
    (root / ".opencode" / "hooks" / "skyguard_adapter.py").write_text(program + "\n")
    code, out = canary(root)
    assert code == 1 and "FAIL  OpenCode refuses a raw plugin write" in out, out


def test_an_opencode_adapter_that_denies_everything_fails(tmp_path):
    root = toolkit(tmp_path, hooks=False)
    (root / ".opencode" / "hooks" / "skyguard_adapter.py").write_text(
        "print('SKYGUARD-OPENCODE {\"decision\": \"deny\"}')\n")
    code, out = canary(root)
    assert code == 1 and "FAIL  OpenCode allows a harmless command" in out, out


def test_an_installed_opencode_plugin_requires_its_adapter(tmp_path):
    root = toolkit(tmp_path, opencode=False)
    plugin = root / ".opencode" / "plugins" / "skyguard.js"
    plugin.parent.mkdir(parents=True)
    shutil.copyfile(REPO / ".opencode" / "plugins" / "skyguard.js", plugin)
    code, out = canary(root)
    assert code == 1 and "FAIL  OpenCode adapter" in out, out


def test_nothing_to_check_fails(tmp_path):
    code, out = canary(toolkit(tmp_path, hooks=False, opencode=False))
    assert code == 1 and "nothing could be checked" in out, out


def test_the_canary_never_writes_the_plugin_it_names(tmp_path):
    root = toolkit(tmp_path)
    canary(root)
    assert not list((root / "Data").glob("*.esp"))


def test_the_canary_is_standard_library_only():
    """The update runs a copy of the pre-update canary from outside the install: it may not import the toolkit."""
    import ast
    tree = ast.parse(CANARY.read_text(encoding="utf-8"))
    names = {a.name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    names |= {n.module.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
    assert names <= set(sys.stdlib_module_names), names - set(sys.stdlib_module_names)


# --- Codex: the real entry script, rendered the way setup leaves it (test_codex_entry's install) ---------------------

def codex_toolkit(tmp_path):
    from conftest import BASH, BS
    root = toolkit(tmp_path, hooks=False, opencode=False)
    hooks = root / ".codex" / "hooks"
    hooks.mkdir(parents=True)
    src = REPO / "agent" / "guards" / "adapters" / "codex"
    for name in ("skyguard-entry.ps1", "skyguard-entry.sh"):
        text = (src / name).read_text(encoding="ascii")
        text = text.replace("{{PYTHON_PATH}}", sys.executable.replace(BS, "/")).replace("{{BASH_PATH}}",
                                                                                       BASH.replace(BS, "/"))
        (hooks / name).write_text(text, encoding="ascii", newline="\n")
    shutil.copy2(REPO / "tools" / "kb-guard.sh", root / "tools" / "kb-guard.sh")
    hooks_json = (REPO / ".codex" / "hooks.json").read_text(encoding="utf-8")
    (root / ".codex" / "hooks.json").write_text(hooks_json.replace("{{CODEX_ROOT_POSIX}}", root.as_posix())
                                                .replace("{{CODEX_ROOT}}", root.as_posix()), encoding="utf-8")
    return root


def test_a_set_up_codex_hook_refuses_the_plugin_write(tmp_path):
    """The canary's Codex request has the recorded Codex 0.160.0 apply_patch shape; the real entry must refuse it,
    or every update on a Codex install would roll itself back."""
    code, out = canary(codex_toolkit(tmp_path))
    assert code == 0 and "ok    Codex refuses a raw plugin write" in out, out
    assert "ok    Codex allows a harmless command" in out, out


def replace_codex_entry(root, ps, sh):
    hooks = root / ".codex" / "hooks"
    entry = hooks / ("skyguard-entry.ps1" if os.name == "nt" else "skyguard-entry.sh")
    entry.write_text(ps if os.name == "nt" else sh, encoding="ascii", newline="\n")


def test_a_codex_hook_that_allows_everything_fails(tmp_path):
    root = codex_toolkit(tmp_path)
    replace_codex_entry(root, "exit 0\n", "cat >/dev/null\nexit 0\n")
    code, out = canary(root)
    assert code == 1 and "FAIL  Codex refuses a raw plugin write" in out, out


def test_a_codex_canary_runs_the_rendered_command(tmp_path):
    root = codex_toolkit(tmp_path)
    definitions = root / ".codex" / "hooks.json"
    data = json.loads(definitions.read_text(encoding="utf-8"))
    hook = data["hooks"]["PreToolUse"][0]["hooks"][0]
    hook["commandWindows" if os.name == "nt" else "command"] = "echo not-a-hook-verdict"
    definitions.write_text(json.dumps(data), encoding="utf-8")
    code, out = canary(root)
    assert code == 1 and "FAIL  Codex refuses a raw plugin write" in out, out


def test_a_codex_hook_that_exits_2_does_not_prove_a_refusal(tmp_path):
    root = codex_toolkit(tmp_path)
    replace_codex_entry(root, "exit 2\n", "cat >/dev/null\nexit 2\n")
    code, out = canary(root)
    assert code == 1 and "FAIL  Codex refuses a raw plugin write" in out, out


def test_a_codex_hook_that_refuses_everything_fails(tmp_path):
    root = codex_toolkit(tmp_path)
    deny = '{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"deny"}}'
    replace_codex_entry(root, "[Console]::WriteLine('" + deny + "')\n", "printf '%s\\n' '" + deny + "'\n")
    code, out = canary(root)
    assert code == 1 and "ok    Codex refuses a raw plugin write" in out, out
    assert "FAIL  Codex allows a harmless command" in out, out


def test_an_unrendered_codex_install_is_not_checked(tmp_path):
    root = codex_toolkit(tmp_path)
    (root / ".codex" / "hooks.json").write_text((REPO / ".codex" / "hooks.json").read_text(encoding="utf-8"))
    code, out = canary(root)
    assert "Codex" not in out and code == 1 and "nothing could be checked" in out, out
