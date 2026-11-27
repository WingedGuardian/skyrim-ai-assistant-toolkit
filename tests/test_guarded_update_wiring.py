"""The Skyrim wiring of the core's guarded update (update-stage / update-apply / update-clear-failed): the policy is
this install's own code, the pre-update canary is kept outside the install, a failed or interrupted update is said
at every session start, and every refusal happens before anything installed changes. No test reaches the network."""
import json
from pathlib import Path
import subprocess
import sys

import pytest

from conftest import REPO, run_setup
sys.path.insert(0, str(REPO / "tools"))
from modding_harness import guarded_update  # noqa: E402
from skyguard import update_check, upgrade_render  # noqa: E402
from test_update_check import GitHub, _no_network, harness, install, zip_of  # noqa: E402,F401  (fixture + helpers)


# --- the policy -------------------------------------------------------------------------------------------------

def test_every_shipped_guard_file_is_guard_code_to_the_update():
    """GUARD-FILES.sha256 names every guard file the release ships; the update must treat each as guard code, or a
    release could change it without the user's separate go."""
    from modding_harness.update_review import _is_guard_path
    policy = update_check.update_policy(REPO)
    names = [line.split()[1] for line in (REPO / "GUARD-FILES.sha256").read_text().splitlines()[1:] if line.strip()]
    assert names and [n for n in names if not _is_guard_path(n, policy)] == []


def test_every_policy_file_exists_and_names_what_runs():
    policy = update_check.update_policy(REPO)
    for name in policy.policy_files:
        assert (REPO / name).is_file(), name
    assert policy.setup[1:] == ("setup.sh", "--agent", "auto", "--guarded-update-python", policy.canary[0])
    assert Path(policy.setup[0]).name.lower().startswith("bash")
    assert Path(policy.canary[2]) == (REPO / update_check.CANARY).resolve() and policy.canary[3:] == (
        "--root", str(REPO.resolve()))


def test_a_kept_canary_is_the_installed_bytes_outside_the_install(tmp_path):
    root = tmp_path / "install"
    (root / "tools" / "skyguard").mkdir(parents=True)
    (root / update_check.CANARY).write_bytes(b"print('installed canary')\n")
    bundle = tmp_path / "updates" / "4.4.0" / "bundle"
    bundle.mkdir(parents=True)
    kept = update_check.keep_installed_canary(root, bundle)
    assert kept.parent == bundle.resolve().parent and kept.read_bytes() == b"print('installed canary')\n"
    (root / update_check.CANARY).write_bytes(b"print('new canary')\n")
    other = update_check.keep_installed_canary(root, bundle)
    assert other != kept and other.read_bytes() == b"print('new canary')\n"
    assert kept.read_bytes() == b"print('installed canary')\n"
    policy = update_check.update_policy(root, canary=kept)
    assert policy.canary[2] == str(kept)


def test_no_installed_canary_refuses(tmp_path):
    with pytest.raises(ValueError, match="missing"):
        update_check.keep_installed_canary(tmp_path, tmp_path / "bundle")


# --- the commands -----------------------------------------------------------------------------------------------

def test_stage_is_refused_while_an_earlier_update_failed(tmp_path, monkeypatch):
    root = install(tmp_path)
    marker = root / guarded_update.FAILED_MARKER
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text('{"v": 1}')
    status, out = harness(root, "update-stage", http=GitHub(), monkeypatch=monkeypatch)
    assert status == 2 and out["ok"] is False and "failed" in out["error"]
    assert (root / "setup.sh").read_text() == "echo old\n"


def test_clear_failed_sets_the_marker_aside(tmp_path, monkeypatch):
    root = install(tmp_path)
    marker = root / guarded_update.FAILED_MARKER
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text('{"v": 1}')
    status, out = harness(root, "update-clear-failed", monkeypatch=monkeypatch)
    assert status == 0 and out["cleared"] == [guarded_update.FAILED_MARKER] and not marker.exists()


def test_apply_runs_with_the_kept_canary_and_the_new_policy(tmp_path, monkeypatch):
    root = install(tmp_path)
    (root / "tools" / "skyguard").mkdir(parents=True, exist_ok=True)
    (root / update_check.CANARY).write_bytes(b"# installed canary\n")
    bundle = tmp_path / "updates" / "bundle"
    bundle.mkdir(parents=True)
    seen = {}

    def apply_update(root_, policy, bundle_, review_sha256, choices, **kw):
        seen.update(policy=policy, kw=kw)
        return guarded_update.Applied(True, "0" * 32, False, "ok\n")
    monkeypatch.setattr(guarded_update, "apply_update", apply_update)
    status, out = harness(root, "update-apply", "--bundle", str(bundle), "--review-sha256", "a" * 64, "--quiescent",
                          monkeypatch=monkeypatch)
    assert status == 0 and out["ok"] is True
    retained = Path(seen["policy"].canary[2])
    assert retained.parent == bundle.resolve().parent and retained.read_bytes() == b"# installed canary\n"
    assert seen["kw"]["new_policy"].canary[2] == str((root / update_check.CANARY).resolve())
    assert "skyrim-paths.env" in seen["kw"]["notes_files"]
    assert seen["kw"]["candidate_validator"] is not None


@pytest.mark.parametrize("marker", [None, guarded_update.FAILED_MARKER, guarded_update.IN_FLIGHT])
def test_a_failed_apply_exits_1(tmp_path, monkeypatch, marker):
    root = install(tmp_path)
    (root / "tools" / "skyguard").mkdir(parents=True, exist_ok=True)
    (root / update_check.CANARY).write_bytes(b"# installed canary\n")
    bundle = tmp_path / "updates" / "bundle"
    bundle.mkdir(parents=True)
    if marker:
        (root / marker).parent.mkdir(parents=True, exist_ok=True)
        (root / marker).write_text('{"v": 1}')
    monkeypatch.setattr(guarded_update, "apply_update",
                        lambda *a, **k: guarded_update.Applied(False, "0" * 32, True, "rolled back\n"))
    status, out = harness(root, "update-apply", "--bundle", str(bundle), "--review-sha256", "a" * 64, "--quiescent",
                          monkeypatch=monkeypatch)
    assert status == 1 and out["ok"] is False and out["rolled_back"] is True
    assert out["recovery_verified"] is (marker is None)


def test_setup_keeps_reviewed_files_recoverable_after_a_candidate_canary_fails(game_dir, monkeypatch):
    setup = run_setup(game_dir)
    assert setup.returncode == 0, setup.stdout + setup.stderr
    (game_dir / "BUILD-INFO").write_bytes(b"commit=" + b"d" * 40 + b"\nversion=4.2.0\nbuilt-from=git archive\n")
    devcontainer = upgrade_render.DEVCONTAINER
    (game_dir / devcontainer).unlink()
    hook = ".claude/hooks/protect-files.sh"
    original_hook = (game_dir / hook).read_bytes()
    original_canary = (game_dir / update_check.CANARY).read_bytes()
    names = (devcontainer, hook, update_check.CANARY, ".claude/settings.json", ".codex/hooks.json")
    previous = {name: (REPO / name).read_bytes() for name in names}
    candidate = {**previous, hook: previous[hook] + b"\n# Reviewed candidate hook\n",
                 update_check.CANARY: b"# Candidate canary\n"}
    github = GitHub(body=zip_of("4.3.0", files=candidate))
    github.previous = zip_of("4.2.0", commit="d" * 40, files=previous)
    status, stage = harness(game_dir, "update-stage", http=github, monkeypatch=monkeypatch)
    assert status == 0, stage
    bundle = Path(stage["bundle"])
    choices = bundle.parent / "choices.json"
    review = json.loads((bundle / "complete.json").read_bytes())
    selections = {row["path"]: "candidate" for row in review["plan"]["files"] if row["action"] == "preserve_collision"}
    selections.update(dict.fromkeys(review["merged_settings_hashes"], "keep"))
    choices.write_text(json.dumps(selections))
    steps = []

    def runner(argv, cwd, timeout):
        if argv[1] == "setup.sh":
            steps.append("setup")
            return run_setup(cwd, args=argv[2:])
        if Path(argv[2]) == game_dir / update_check.CANARY:
            steps.append("candidate canary")
            assert b"{{DEVCONTAINER_MODS_MOUNT}}" not in (game_dir / devcontainer).read_bytes()
            assert (game_dir / hook).read_bytes().endswith(b"# Reviewed candidate hook\n")
            return subprocess.CompletedProcess(argv, 1, b"injected candidate failure", b"")
        steps.append("restored canary")
        assert Path(argv[2]).read_bytes() == original_canary
        assert (game_dir / hook).read_bytes() == original_hook
        assert not (game_dir / devcontainer).exists()
        return guarded_update.run(argv, cwd, timeout)

    monkeypatch.setattr(guarded_update, "_default_run", runner)
    status, result = harness(game_dir, "update-apply", "--bundle", str(bundle), "--review-sha256",
                             stage["review_sha256"], "--choices", str(choices), "--quiescent", "--accept-guard-changes",
                             monkeypatch=monkeypatch)
    assert status == 1 and result.get("recovery_verified") is True, result
    assert steps == ["setup", "candidate canary", "setup", "restored canary"]


def test_stage_reviews_the_release_and_changes_nothing_installed(tmp_path, monkeypatch):
    root = install(tmp_path)
    before = {p: p.read_bytes() for p in root.rglob("*") if p.is_file()}
    status, out = harness(root, "update-stage", http=GitHub(), monkeypatch=monkeypatch)
    assert status == 0, out
    assert out["application_authorized"] is False and (out["from_version"], out["to_version"]) == ("4.2.0", "4.3.0")
    assert "update-apply" in out["review"] and Path(out["bundle"], "complete.json").is_file()
    after = {p: p.read_bytes() for p in root.rglob("*") if p.is_file() and ".modding-agent-harness" not in p.parts}
    assert after == before


# --- the session start ------------------------------------------------------------------------------------------

@pytest.mark.parametrize("env", [{}, {"SKYGUARD_UPDATE_CHECK": "off"}, {"PYTEST_CURRENT_TEST": "x"}])
def test_a_failed_update_is_said_at_every_session_start_even_with_checking_off(tmp_path, monkeypatch, env):
    root = tmp_path / "install"
    (root / "tools").mkdir(parents=True)
    marker = root / guarded_update.FAILED_MARKER
    marker.parent.mkdir(parents=True)
    marker.write_text('{"v": 1}')
    monkeypatch.setattr(update_check, "__file__", str(root / "tools" / "skyguard" / "update_check.py"))
    (root / "tools" / "agent-harness.py").write_text("")
    for name in ("SKYGUARD_UPDATE_CHECK", "SKYGUARD_HOUSEKEEP_SWEEP", "PYTEST_CURRENT_TEST", "HARNESS_FIXTURE_ROOT"):
        monkeypatch.delenv(name, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("SKYGUARD_HOUSEKEEP_SWEEP", "off")    # never spawn the background check from a test
    note = update_check.session_note(root)
    assert "TOOLKIT UPDATE NEEDS THE USER" in note and guarded_update.FAILED_MARKER in note
