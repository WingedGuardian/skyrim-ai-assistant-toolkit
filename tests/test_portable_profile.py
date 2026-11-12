import json
from pathlib import Path
import sys

import pytest

from conftest import REPO
sys.path.insert(0, str(REPO / "tools"))
from modding_harness import approval, contracts
from skyguard import portable, save_store
from skyguard.adapters import opencode


@pytest.fixture
def install(tmp_path):
    root = tmp_path / "toolkit"
    root.mkdir()
    saves = tmp_path / "config/Saves"
    saves.mkdir(parents=True)
    (root / "skyrim-paths.env").write_text("SKYRIM_CONFIG_DIR='" + saves.parent.as_posix() + "'\n")
    files = []
    for index in range(3):
        pair = tuple(saves / (str(index) + extension) for extension in (".ess", ".skse"))
        for member in pair:
            member.write_bytes(member.name.encode())
        files.append(pair)
    return root, files


def action(root, files, host="future-agent"):
    return contracts.Action(host, "edit", "files", cwd=str(root), session_id="s", tool_call_id="c",
                            paths=tuple(str(path) for path in files), operations=("write",) * len(files))


def test_generic_upgrade_stage_keeps_private_install_bytes_and_refuses_bundle_reuse(tmp_path):
    import hashlib
    import subprocess
    installed, source, bundle = tmp_path / "installed", tmp_path / "source", tmp_path / "review"
    installed.mkdir()
    source.mkdir()
    (installed / "AGENTS.md").write_bytes(b"private instructions")
    (source / "AGENTS.md").write_bytes(b"candidate instructions")
    inventory = tmp_path / "candidate.json"
    inventory.write_text(json.dumps({"v": 1, "files": {"AGENTS.md": hashlib.sha256(b"candidate instructions").hexdigest()}}))
    args = [sys.executable, "-B", str(REPO / "tools/agent-harness.py"), "upgrade-stage", "--installed-root", str(installed),
            "--source", str(source), "--candidate", str(inventory), "--destination", str(bundle)]
    first = subprocess.run(args, capture_output=True, timeout=45)
    assert first.returncode == 0, first.stderr
    result = json.loads(first.stdout)
    assert result["complete"] and not result["application_authorized"] and result["collisions"] == 1
    assert (installed / "AGENTS.md").read_bytes() == b"private instructions"
    assert (bundle / "rollback/AGENTS.md").read_bytes() == b"private instructions"
    assert subprocess.run(args, capture_output=True, timeout=45).returncode == 2
    choices = tmp_path / 'choices.json'
    choices.write_text(json.dumps({'AGENTS.md': 'candidate'}))
    review_sha = hashlib.sha256((bundle / 'complete.json').read_bytes()).hexdigest()
    applying = [sys.executable, '-B', str(REPO / 'tools/agent-harness.py'), 'upgrade-apply',
                '--installed-root', str(installed), '--bundle', str(bundle), '--review-sha256', review_sha,
                '--choices', str(choices), '--quiescent']
    applied = subprocess.run(applying, capture_output=True, timeout=45)
    assert applied.returncode == 0, applied.stdout
    receipt = json.loads(applied.stdout)
    assert receipt['host_review_required'] and (installed / 'AGENTS.md').read_bytes() == b'candidate instructions'
    rolled = subprocess.run([sys.executable, '-B', str(REPO / 'tools/agent-harness.py'), 'upgrade-rollback',
        receipt['operation'], '--installed-root', str(installed), '--quiescent'], capture_output=True, timeout=45)
    assert rolled.returncode == 0, rolled.stdout
    assert (installed / 'AGENTS.md').read_bytes() == b'private instructions'


@pytest.mark.parametrize("in_place", [False, True])
def test_neutral_restore_carries_one_deadline_through_every_copy(install, tmp_path, monkeypatch, in_place):
    from modding_harness import durable
    root, files = install
    transaction = save_store.store(root).backup([files[0]])["id"]
    for member in files[0]:
        member.write_bytes(b"current")
    real, seen = durable.copy_verified, []
    def observe(source, target, deadline=None):
        seen.append(deadline)
        return real(source, target, deadline)
    monkeypatch.setattr(durable, "copy_verified", observe)
    result = save_store.restore_result(root, transaction, to=None if in_place else tmp_path / "out", in_place=in_place)
    assert len(result["published"]) == 2
    assert seen and all(deadline is not None for deadline in seen)
    assert len(set(seen)) == 1, "pre-preservation and restore must share one total budget"


def test_unknown_records_each_gap_and_file_plugin_rule_still_denies(install):
    root, files = install
    request = contracts.Action("future-agent", "future-MCP-code-mode", "unknown", session_id="s")
    outcome, count = portable.check(request, root)
    assert (outcome.result.decision, outcome.result.evaluation, count) == ("allow", "unguarded", 1)
    assert "WARN AND ALLOW" in outcome.result.warnings[0]
    _, count = portable.check(request, root)
    assert count == 2
    outcome, _ = portable.check(action(root, [root / "plugin.esp"]), root)
    assert outcome.result.decision == "deny" and not outcome.transactions


def test_opencode_uses_its_native_grammar_and_refuses_bad_known_payload(install):
    root, files = install
    data = {"v": 1, "tool": "edit", "args": {"filePath": str(files[0][0])}, "directory": str(root)}
    result = opencode.handle(json.dumps(data), root)
    assert result["decision"] in ("allow", "advise") and "backup verified" in result["context"]
    data["args"] = {}
    result = opencode.handle(json.dumps(data), root)
    assert result["decision"] == "deny" and result["inert"]
    data.update(tool="apply_patch", args={"patchText": "*** Begin Patch\n*** Add File: harmless.txt\n+x\n  *** Delete File: plugin.esp\n*** End Patch"})
    result = opencode.handle(json.dumps(data), root)
    assert result["decision"] != "deny", result  # OpenCode skips that indented header.


def test_recovery_selecting_cosave_restores_pair_and_rejects_unexpected_counterpart(install, tmp_path):
    root, files = install
    target = save_store.store(root)
    record = target.backup(files)
    out = tmp_path / "recovered"
    result = save_store.restore_result(root, record["id"], wanted=[str(files[0][1])], to=out)
    assert {Path(path).suffix for path in result["published"]} == {".ess", ".skse"}
    assert len(result["published"]) == 2
    files[0][1].unlink()
    record = target.backup([files[0]])
    files[0][1].write_bytes(b"unexpected counterpart")
    with pytest.raises(OSError, match="counterpart|unexpected"):
        save_store.restore_result(root, record["id"], in_place=True)
    assert files[0][1].read_bytes() == b"unexpected counterpart"

