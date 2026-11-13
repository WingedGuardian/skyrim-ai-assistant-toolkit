"""Configured permanent stores keep their own rule even outside the install tree."""
import json
import sys

import pytest

from conftest import REPO

sys.path.insert(0, str(REPO / "tools"))
from skyguard import audit, paths, shell_rules, write_rules  # noqa: E402


@pytest.mark.parametrize("route", ["shell", "files"])
def test_configured_store_mutations_are_denied_before_ordinary_location_rules(tmp_path, route):
    project, store = tmp_path / "toolkit", tmp_path / "permanent-store"
    (project / ".modding-agent-harness").mkdir(parents=True)
    (project / ".modding-agent-harness/config.json").write_text(
        json.dumps({"v": 1, "save_store": str(store)}), encoding="utf-8")
    store.mkdir()
    target = store / "owned-blob"
    target.write_bytes(b"owned permanent recovery bytes")
    roots = paths.roots(str(project), str(project), {"SKYRIM_GAME_ROOT": str(project)})

    def decide(path):
        log = audit.Audit("")
        log.raw = lambda line: None
        if route == "shell":
            value = shell_rules.decide('printf x > "' + path.as_posix() + '"', roots, log,
                                       cwd=project.as_posix(), shell="bash", env={})
        else:
            value = write_rules.decide(str(path), log, roots=roots, cwd=str(project), env={})
        return value, {rid for rid, _ in log.fired}

    value, fired = decide(target)
    rid = "S-SAVE-STORE" if route == "shell" else "W-SAVE-STORE"
    assert value.decision == "deny" and fired == {rid}
    assert "permanent save backup store" in value.reason
    ordinary, _ = decide(tmp_path / "ordinary-output")
    assert ordinary.decision == "allow"
    assert target.read_bytes() == b"owned permanent recovery bytes"


@pytest.mark.parametrize("which", ["skyrim", "skyrim-config"])
@pytest.mark.parametrize("route", ["shell-delete", "shell-delete-parent", "shell-write", "files-write",
                                   "files-delete-parent"])
def test_the_save_vault_and_its_ini_vault_are_save_stores(tmp_path, route, which):
    """4.1 review: the vault (the only way back for a changed save since 4.1) lives outside the install, under
    SAVE_VAULT_DIR or %LOCALAPPDATA%; S-SAVE-STORE / W-SAVE-STORE must cover it and its INI sibling."""
    project, vault = tmp_path / "toolkit", tmp_path / "data" / "save-vault" / "skyrim"
    project.mkdir()
    store = vault.with_name(which)
    (store / "blobs").mkdir(parents=True)
    env = {"SKYRIM_GAME_ROOT": str(project), "SAVE_VAULT_DIR": str(vault)}
    roots = paths.roots(str(project), str(project), env)

    def decide(path):
        log = audit.Audit("")
        log.raw = lambda line: None
        p = path.as_posix()
        if route == "shell-delete":
            value = shell_rules.decide('rm -rf "' + p + '"', roots, log, cwd=project.as_posix(), shell="bash", env=env)
        elif route == "shell-delete-parent":
            value = shell_rules.decide('rm -rf "' + path.parent.as_posix() + '"', roots, log,
                                       cwd=project.as_posix(), shell="bash", env=env)
        elif route == "shell-write":
            value = shell_rules.decide('printf x > "' + p + '/blobs/x"', roots, log, cwd=project.as_posix(),
                                       shell="bash", env=env)
        elif route == "files-write":
            value = write_rules.decide(str(path / "blobs" / "x"), log, roots=roots, cwd=str(project), env=env)
        else:
            value = write_rules.decide(str(path.parent), log, roots=roots, cwd=str(project), env=env, op="delete")
        return value, {rid for rid, _ in log.fired}

    value, fired = decide(store)
    rid = "S-SAVE-STORE" if route.startswith("shell") else "W-SAVE-STORE"
    assert value.decision == "deny" and fired == {rid}, (value, fired)
    other, _ = decide(tmp_path / "data" / "unrelated" / "cache")
    if route.endswith("parent"):
        other, _ = decide(tmp_path / "unrelated" / "x" / "cache")
    assert other.decision == "allow"


def test_the_vault_is_a_save_store_even_with_no_paths_file(tmp_path):
    """Confirmation review: an unreadable paths file gives write_rules roots=None; the vault check needs no roots."""
    vault = tmp_path / "data" / "save-vault" / "skyrim"
    (vault / "blobs").mkdir(parents=True)
    log = audit.Audit("")
    log.raw = lambda line: None
    value = write_rules.decide(str(vault / "blobs" / "x"), log, roots=None, cwd=str(tmp_path),
                               env={"SAVE_VAULT_DIR": str(vault)})
    assert value.decision == "deny" and {rid for rid, _ in log.fired} == {"W-SAVE-STORE"}
