"""Updates from GitHub (skyguard/update_check.py + the vendored modding_harness.release): the session note, the
installed version, this install's render config, and the agent-harness commands. No test reaches the network: urlopen
is refused for every test, and GitHub is a fake that serves a release zip built here."""
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import urllib.error
import urllib.request
import zipfile

import pytest

from conftest import REPO
sys.path.insert(0, str(REPO / "tools"))
from modding_harness import installation, release  # noqa: E402
from skyguard import update_check  # noqa: E402

COMMIT = "c" * 40


@pytest.fixture(autouse=True)
def _no_network(monkeypatch, tmp_path_factory):
    def refuse(*a, **k):
        raise AssertionError("an update test reached the real network")
    monkeypatch.setattr(urllib.request, "urlopen", refuse)
    # update-fetch writes outside the install, per user: never the real %LOCALAPPDATA% from a test
    monkeypatch.setenv("MODDING_HARNESS_UPDATES_DIR", str(tmp_path_factory.mktemp("updates")))


def zip_of(version, commit=COMMIT, files=None):
    buf = io.BytesIO()
    top = "skyrim-ai-assistant-toolkit-" + version
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(top + "/BUILD-INFO", "commit={}\nversion={}\nbuilt-from=git archive\n".format(commit, version))
        for name, data in (files or {"setup.sh": "echo new\n"}).items():
            z.writestr(top + "/" + name, data)
    return buf.getvalue()


class GitHub:
    """GET /releases/latest, /commits/<tag>, /releases/tags/<tag> and the asset downloads, for update_check.REPO."""

    def __init__(self, latest="4.3.0", installed="4.2.0", body=None, digest=None):
        self.latest, self.installed = latest, installed
        self.body = zip_of(latest) if body is None else body
        self.previous = zip_of(installed, commit="d" * 40)
        self.digest = digest or "sha256:" + hashlib.sha256(self.body).hexdigest()
        self.calls = []

    def url(self, version):
        return "https://github.com/{}/releases/download/v{}/{}".format(update_check.REPO, version,
                                                                       update_check.ASSET.format(version=version))

    def __call__(self, url, limit=release.MAX_META, accept=None, stream_to=None):
        self.calls.append(url)
        base = "{}/repos/{}".format(release.API, update_check.REPO)
        if url == base + "/releases/latest":
            return json.dumps({"tag_name": "v" + self.latest, "draft": False, "prerelease": False, "assets": [
                {"name": update_check.ASSET.format(version=self.latest), "size": len(self.body),
                 "digest": self.digest, "browser_download_url": self.url(self.latest)}]}).encode()
        if url == base + "/commits/v" + self.latest:
            return json.dumps({"sha": COMMIT}).encode()
        if url == base + "/releases/tags/v" + self.installed:
            return json.dumps({"assets": [{"name": update_check.ASSET.format(version=self.installed),
                                           "digest": "sha256:" + hashlib.sha256(self.previous).hexdigest(),
                                           "browser_download_url": self.url(self.installed)}]}).encode()
        for version, body in ((self.latest, self.body), (self.installed, self.previous)):
            if url == self.url(version):
                stream_to.write(body)
                return hashlib.sha256(body).hexdigest(), len(body)
        raise urllib.error.HTTPError(url, 404, "Not Found", None, None)


def install(tmp_path, version="4.2.0", build_info=True, paths=None):
    root = tmp_path / "Skyrim VR"
    (root / "tools").mkdir(parents=True)
    (root / "SkyrimVR.exe").write_bytes(b"")
    (root / "setup.sh").write_text("echo old\n")
    if build_info:
        (root / "BUILD-INFO").write_text("commit={}\nversion={}\n".format("d" * 40, version))
    (root / "GUARD-FILES.sha256").write_text("# release {}\n".format(version))
    config = tmp_path / "My Games" / "Skyrim VR"
    config.mkdir(parents=True)
    loadorder = tmp_path / "Local" / "Skyrim VR"
    loadorder.mkdir(parents=True)
    lines = paths or {"SKYRIM_GAME_ROOT": root.as_posix(), "SKYRIM_CONFIG_DIR": config.as_posix(),
                      "SKYRIM_LOADORDER_DIR": loadorder.as_posix(), "SKYRIM_MO2_MODS": "", "SKYRIM_MO2_OVERWRITE": "",
                      "SKYGUARD_PYTHON": Path(sys.executable).as_posix()}
    (root / "skyrim-paths.env").write_text("".join("{}='{}'\n".format(k, v) for k, v in lines.items()))
    return root


# --- the installed version -------------------------------------------------------------------------------------------

def test_the_installed_version_comes_from_build_info(tmp_path):
    root = install(tmp_path, "4.2.0")
    (root / "GUARD-FILES.sha256").write_text("# release 9.9.9\n")
    assert update_check.installed_version(root) == "4.2.0"


def test_without_build_info_the_guard_manifest_names_the_release(tmp_path):
    assert update_check.installed_version(install(tmp_path, "4.2.0", build_info=False)) == "4.2.0"


def test_a_checkout_with_neither_has_no_installed_version(tmp_path):
    root = install(tmp_path, build_info=False)
    (root / "GUARD-FILES.sha256").unlink()
    assert update_check.installed_version(root) is None


# --- the session note ------------------------------------------------------------------------------------------------

def test_the_session_note_never_runs_or_spawns_under_a_test(tmp_path, monkeypatch):
    """Even the toolkit's own folder: a test must never start a background GitHub check."""
    spawned = []
    monkeypatch.setattr("subprocess.Popen", lambda *a, **k: spawned.append(a))
    assert update_check.session_note(REPO) == ""
    assert spawned == []


def test_the_session_note_is_only_for_the_installed_toolkits_own_folder(tmp_path, monkeypatch):
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.delenv("HARNESS_FIXTURE_ROOT", raising=False)
    spawned = []
    monkeypatch.setattr("subprocess.Popen", lambda *a, **k: spawned.append(a))
    assert update_check.session_note(install(tmp_path)) == ""      # a folder that is not this toolkit
    assert spawned == []


def test_the_session_note_shows_the_cached_release_and_starts_a_detached_hidden_check(monkeypatch, tmp_path):
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.delenv("HARNESS_FIXTURE_ROOT", raising=False)
    monkeypatch.delenv("SKYGUARD_UPDATE_CHECK", raising=False)
    monkeypatch.delenv("MODDING_HARNESS_UPDATE_CHECK", raising=False)
    monkeypatch.setattr(release, "note", lambda root, profile: "The Skyrim toolkit 4.3.0 is out (you have 4.2.0)")
    spawned = []
    monkeypatch.setattr("subprocess.Popen", lambda args, **k: spawned.append((args, k)))
    line = update_check.session_note(REPO)
    assert line == "The Skyrim toolkit 4.3.0 is out (you have 4.2.0)\n"
    (args, kwargs), = spawned
    assert args[-1] == "update-check" and Path(args[-2]).name == "agent-harness.py"
    if os.name == "nt":
        assert kwargs["creationflags"] & 0x08000000           # CREATE_NO_WINDOW: never a console window
        assert kwargs["creationflags"] & 0x00000008           # DETACHED_PROCESS: the session start never waits
    assert kwargs["stdout"] is kwargs["stderr"] is kwargs["stdin"]


@pytest.mark.parametrize("env", [{"SKYGUARD_UPDATE_CHECK": "off"}, {"MODDING_HARNESS_UPDATE_CHECK": "off"}])
def test_turned_off_the_session_neither_notes_nor_checks(monkeypatch, env):
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.delenv("HARNESS_FIXTURE_ROOT", raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    spawned = []
    monkeypatch.setattr("subprocess.Popen", lambda *a, **k: spawned.append(a))
    assert update_check.session_note(REPO) == ""
    assert spawned == []


def test_the_note_is_shown_once_per_version_from_the_cache(tmp_path):
    root = install(tmp_path)
    release.check(root, update_check.PROFILE, GitHub(), force=True, environ={})
    assert "4.3.0 is out (you have 4.2.0)" in release.note(root, update_check.PROFILE, environ={})
    assert release.note(root, update_check.PROFILE, environ={}) == ""


def test_the_session_starts_show_the_note_before_the_vault_line():
    text = (REPO / "tools/skyguard/run.py").read_text(encoding="utf-8")
    assert text.count("out = _update_note(a.project) + _vault_note(a.project) + out") == 2   # Claude and Codex


# --- this install's render config ------------------------------------------------------------------------------------

def test_the_render_config_is_this_installs_recorded_paths(tmp_path):
    root = install(tmp_path)
    data = update_check.render_config(root)
    assert data["config_dir"] == (tmp_path / "My Games" / "Skyrim VR").as_posix()
    assert data["loadorder_dir"] == (tmp_path / "Local" / "Skyrim VR").as_posix()
    assert data["skyrim_folder"] == "Skyrim VR"
    assert data["mod_manager"] == {"kind": "stock"}
    assert data["game_root"] == root.as_posix()


def test_an_mo2_render_config_names_the_profile_the_settings_live_in(tmp_path):
    instance = tmp_path / "MO2"
    profile = instance / "profiles" / "Survival"
    profile.mkdir(parents=True)
    root = install(tmp_path, paths={"SKYRIM_GAME_ROOT": (tmp_path / "Skyrim VR").as_posix(),
                                    "SKYRIM_CONFIG_DIR": profile.as_posix(), "SKYRIM_LOADORDER_DIR": profile.as_posix(),
                                    "SKYRIM_MO2_MODS": (instance / "mods").as_posix(),
                                    "SKYRIM_MO2_OVERWRITE": (instance / "overwrite").as_posix()})
    manager = update_check.render_config(root)["mod_manager"]
    assert manager == {"kind": "mo2", "instance": instance.as_posix(), "profile": "Survival",
                       "mods": (instance / "mods").as_posix(), "overwrite": (instance / "overwrite").as_posix(),
                       "profile_dir": profile.as_posix()}


def test_no_recorded_settings_folder_refuses_rather_than_guessing(tmp_path):
    root = install(tmp_path, paths={"SKYRIM_GAME_ROOT": (tmp_path / "Skyrim VR").as_posix()})
    with pytest.raises(ValueError, match="re-run setup.sh"):
        update_check.render_config(root)


def test_the_render_config_passes_the_renderers_own_checks(tmp_path):
    from skyguard import upgrade_render
    root = install(tmp_path)
    upgrade_render._config(root, update_check.render_config(root))      # raises on any value it would refuse


# --- the agent-harness commands --------------------------------------------------------------------------------------

def harness(root, *argv, http=None, monkeypatch=None):
    spec = __import__("importlib.util").util.spec_from_file_location("agent_harness", REPO / "tools/agent-harness.py")
    module = __import__("importlib.util").util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if http is not None:
        monkeypatch.setattr(release, "http_get", http)
        monkeypatch.setattr(release.fetch, "__defaults__", (http, None))
        monkeypatch.setattr(release.check, "__defaults__", (http, None, False, None))
    out = io.BytesIO()
    monkeypatch.setattr(sys, "stdout", type("S", (), {"buffer": out})())
    status = module.main([*argv, "--installed-root", str(root)])
    return status, json.loads(out.getvalue())


def test_update_check_reports_a_newer_release(tmp_path, monkeypatch):
    root = install(tmp_path)
    status, out = harness(root, "update-check", "--now", http=GitHub(), monkeypatch=monkeypatch)
    assert status == 0
    assert (out["installed"], out["latest"], out["update_available"]) == ("4.2.0", "4.3.0", True)


def test_update_fetch_gives_the_reviewed_upgraders_inputs_and_changes_nothing_installed(tmp_path, monkeypatch):
    root = install(tmp_path)
    before = {p: p.read_bytes() for p in root.rglob("*") if p.is_file()}
    status, out = harness(root, "update-fetch", http=GitHub(), monkeypatch=monkeypatch)
    assert status == 0 and out["installed_changed"] is False
    assert {p: p.read_bytes() for p in root.rglob("*") if p.is_file() and ".modding-agent-harness" not in p.parts} == before
    for key in ("source", "candidate", "previous", "render_config"):
        assert Path(out[key]).exists(), key
    assert json.loads(Path(out["render_config"]).read_text())["mod_manager"] == {"kind": "stock"}


def test_a_refused_fetch_says_why_and_fails(tmp_path, monkeypatch):
    root = install(tmp_path)
    status, out = harness(root, "update-fetch", http=GitHub(digest="sha256:" + "0" * 64), monkeypatch=monkeypatch)
    assert status == 2
    assert out["ok"] is False and "SHA-256" in out["error"] and out["installed_changed"] is False


def test_the_update_setting_survives_every_upgrade():
    assert release.SETTINGS in installation.LOCAL_ONLY


def test_the_skill_is_shipped_and_names_every_step():
    text = (REPO / "agent/skills/toolkit-update/SKILL.md").read_text(encoding="utf-8")
    for step in ("update-check --now", "update-fetch", "upgrade-stage", "--render-config", "upgrade-apply",
                 "--quiescent", "setup.sh --agent auto", "hook-canary.sh", "upgrade-rollback",
                 "preserve_collision", "merged_settings_hashes", "previous_sha256"):   # learned from the E2E run
        assert step in text, step


# --- one refusal per clause (each is the named test of a mutation twin) ---------------------------------------------

def _fetch_refuses(tmp_path, monkeypatch, gh, match):
    root = install(tmp_path)
    status, out = harness(root, "update-fetch", http=gh, monkeypatch=monkeypatch)
    assert status == 2 and match in out["error"] and out["installed_changed"] is False
    assert (root / "setup.sh").read_text() == "echo old\n"


def test_no_published_digest_refuses(tmp_path, monkeypatch):
    gh = GitHub()
    gh.digest = ""
    _fetch_refuses(tmp_path, monkeypatch, gh, "no SHA-256 digest")


def test_build_info_naming_another_commit_refuses(tmp_path, monkeypatch):
    _fetch_refuses(tmp_path, monkeypatch, GitHub(body=zip_of("4.3.0", commit="e" * 40)), "different commit")


def test_a_zip_with_two_folders_refuses(tmp_path, monkeypatch):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("skyrim-ai-assistant-toolkit-4.3.0/BUILD-INFO", "commit={}\nversion=4.3.0\n".format(COMMIT))
        z.writestr("elsewhere/setup.sh", "echo evil\n")
    _fetch_refuses(tmp_path, monkeypatch, GitHub(body=buf.getvalue()), "exactly one folder")


def test_a_download_from_outside_the_repository_refuses(tmp_path, monkeypatch):
    gh = GitHub()
    gh.url = lambda version: "https://example.invalid/" + update_check.ASSET.format(version=version)
    _fetch_refuses(tmp_path, monkeypatch, gh, "not this repository")


def test_a_draft_or_prerelease_is_never_offered(tmp_path):
    root = install(tmp_path)
    gh = GitHub()
    real = gh.__call__

    def draft(url, **kw):
        body = real(url, **kw)
        return body.replace(b'"draft": false', b'"draft": true') if url.endswith("/releases/latest") else body
    release.check(root, update_check.PROFILE, draft, force=True, environ={})
    assert release.note(root, update_check.PROFILE, environ={}) == ""


def test_github_is_asked_at_most_once_a_day(tmp_path):
    root = install(tmp_path)
    gh = GitHub()
    release.check(root, update_check.PROFILE, gh, now=1000, environ={})
    release.check(root, update_check.PROFILE, gh, now=1000 + release.INTERVAL - 1, environ={})
    assert len(gh.calls) == 1
