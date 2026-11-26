"""The toolkit-update canary (tools/skyguard/update_canary.py): run after an update's setup, it must pass on a working
install and fail when a guard is broken, because a failure is what rolls the update back."""
import json
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


def test_a_broken_opencode_adapter_fails(tmp_path):
    root = toolkit(tmp_path, hooks=False)
    (root / ".opencode" / "hooks" / "skyguard_adapter.py").write_text(
        "print('SKYGUARD-OPENCODE {\"v\": 1, \"decision\": \"allow\"}')\n")
    code, out = canary(root)
    assert code == 1 and "FAIL  OpenCode refuses a raw plugin write" in out, out


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
                                                .replace("{{CODEX_ROOT}}", str(root)), encoding="utf-8")
    return root


def test_a_set_up_codex_hook_refuses_the_plugin_write(tmp_path):
    """The canary's Codex request has the recorded Codex 0.160.0 apply_patch shape; the real entry must refuse it,
    or every update on a Codex install would roll itself back."""
    code, out = canary(codex_toolkit(tmp_path))
    assert code == 0 and "ok    Codex refuses a raw plugin write" in out, out


def test_a_codex_hook_that_allows_everything_fails(tmp_path):
    root = codex_toolkit(tmp_path)
    (root / ".codex" / "hooks" / "skyguard-entry.sh").write_text("#!/bin/bash\ncat >/dev/null\nexit 0\n", newline="\n")
    code, out = canary(root)
    assert code == 1 and "FAIL  Codex refuses a raw plugin write" in out, out


def test_an_unrendered_codex_install_is_not_checked(tmp_path):
    root = codex_toolkit(tmp_path)
    (root / ".codex" / "hooks.json").write_text((REPO / ".codex" / "hooks.json").read_text(encoding="utf-8"))
    code, out = canary(root)
    assert "Codex" not in out and code == 1 and "nothing could be checked" in out, out
