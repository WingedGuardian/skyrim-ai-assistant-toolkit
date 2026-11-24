"""The Skyrim toolkit's release profile and its session-start update note.

Users find the toolkit on Nexus and download it there the first time; updates come from the GitHub releases, through
the agent (user ruling 2026-11-23). The shared core (`modding_harness.release`) does the checking and the verified
download; this file names the repository and the zip, reads the installed version, and gives the session start its
one line. The line comes from the cache the PREVIOUS check wrote: the check itself runs detached, so a session start
never waits on GitHub. Turn it off: `{"v": 1, "check": false}` in `.modding-agent-harness/updates.json`, or
MODDING_HARNESS_UPDATE_CHECK=off.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sys
from pathlib import Path

from modding_harness import release

REPO = "WingedGuardian/skyrim-ai-assistant-toolkit"
ASSET = "skyrim-ai-assistant-toolkit-{version}.zip"


def installed_version(root):
    """The release this install came from: BUILD-INFO's `version=`, else GUARD-FILES.sha256's `# release X` line
    (an install that never copied BUILD-INFO). None when neither says (a git checkout: nothing to compare)."""
    version = release.build_info(root).get("version")
    if release.parse_version(version):
        return version
    try:
        with (Path(root) / "GUARD-FILES.sha256").open("rb") as stream:
            first = stream.readline(200).decode("utf-8", "replace")
    except OSError:
        return None
    match = re.match(r"#\s*release\s+(\S+)", first)
    return match.group(1) if match and release.parse_version(match.group(1)) else None


PROFILE = release.ReleaseProfile(REPO, ASSET, "The Skyrim toolkit", installed_version)


GIT_BASH = "C:/Program Files/Git/bin/bash.exe"


def _bash():
    """Git Bash, never WSL's launcher: `bash` on PATH can be System32/bash.exe or the WindowsApps alias, and a hook
    command rendered with it would run in WSL (setup.sh keeps an already filled value, so the wrong one would stick)."""
    for candidate in (GIT_BASH, shutil.which("bash")):
        if not candidate or not Path(candidate).is_file():
            continue
        lowered = Path(candidate).as_posix().lower()
        if "/windows/system32/" in lowered or "/windowsapps/" in lowered:
            continue
        return Path(candidate).as_posix()
    raise ValueError("Git Bash was not found; the hooks need it (run setup.sh)")


def render_config(root):
    """`upgrade-stage --render-config` values for THIS install, from the paths setup.sh recorded
    (skyrim-paths.env, which keeps a path the user corrected) plus the same derivations setup.sh makes. The applied
    files must hold working hook commands at once: Claude Code blocks every call when a hook fails, which would
    block the very `setup.sh` run that follows. upgrade_render refuses any value that differs from the retained ones."""
    from skyguard import paths
    root = Path(root)
    cfg = paths.read_install(str(root), {})
    if cfg.malformed:
        raise ValueError("skyrim-paths.env names a folder the guard cannot read; re-run setup.sh before updating")
    config_dir, loadorder = cfg.first("SKYRIM_CONFIG_DIR"), cfg.first("SKYRIM_LOADORDER_DIR")
    if not config_dir or not loadorder:
        raise ValueError("skyrim-paths.env has no settings or load-order folder; re-run setup.sh before updating")
    game_root = cfg.first("SKYRIM_GAME_ROOT") or root.resolve().as_posix()
    # setup.sh's SKYRIM_FOLDER: it writes SKYRIM_DOCUMENTS_DIR as Documents/My Games/<that folder> (an MO2 profile
    # moves only SKYRIM_CONFIG_DIR), so its last part IS the folder setup.sh chose; the exe check is the fallback.
    documents = cfg.first("SKYRIM_DOCUMENTS_DIR")
    vr = (Path(game_root) / "SkyrimVR.exe").is_file() and not (Path(game_root) / "SkyrimSE.exe").is_file()
    se = (Path(game_root) / "SkyrimSE.exe").is_file() and not (Path(game_root) / "SkyrimVR.exe").is_file()
    if documents and Path(documents).name in ("Skyrim VR", "Skyrim Special Edition"):
        folder = Path(documents).name
    else:
        folder = "Skyrim VR" if vr else "Skyrim Special Edition" if se else "Skyrim VR"   # setup.sh's last fallback
    mods, overwrite = cfg.first("SKYRIM_MO2_MODS"), cfg.first("SKYRIM_MO2_OVERWRITE")
    manager = ({"kind": "mo2", "instance": Path(mods).parent.as_posix(), "profile": Path(config_dir).name,
                "mods": mods, "overwrite": overwrite, "profile_dir": config_dir} if mods else {"kind": "stock"})
    return {"v": 1, "config_dir": config_dir, "loadorder_dir": loadorder, "skyrim_folder": folder,
            "python": cfg.first("SKYGUARD_PYTHON") or Path(sys.executable).as_posix(), "bash": _bash(),
            "mod_manager": manager, "game_root": game_root}


def write_render_config(root, folder):
    path = Path(folder) / "render-config.json"
    path.write_bytes((json.dumps(render_config(root), indent=1, sort_keys=True) + "\n").encode("utf-8"))
    return str(path)


def off(env):
    """The Skyrim switches that turn update checks off, for the session note AND `update-check` itself:
    SKYGUARD_UPDATE_CHECK=off, MODDING_HARNESS_UPDATE_CHECK=off, and SKYGUARD_HOUSEKEEP_SWEEP=off -- the switch that
    turns off every background helper a session start launches (the housekeep sweep, and since 4.3 this check); test
    helpers that clean the environment set it, and without it a hook run on a test install could reach GitHub."""
    return (env.get("SKYGUARD_UPDATE_CHECK", "").strip().lower() == "off"
            or env.get("SKYGUARD_HOUSEKEEP_SWEEP", "").strip().lower() == "off"
            or env.get("MODDING_HARNESS_UPDATE_CHECK", "").strip().lower() in ("off", "0", "false", "no"))


def session_note(project) -> str:
    """The cached "a newer release is out" line (once per version), and a fresh check launched DETACHED (it asks
    GitHub at most once a day by itself). Only for the installed toolkit's own entry, never under the test harness
    (the same markers as the housekeep sweep and the vault watcher, plus SKYGUARD_UPDATE_CHECK=off): a test must never
    reach GitHub. Never raises: an update check must not cost the session its banner."""
    env = os.environ
    tool = Path(__file__).resolve().parents[1] / "agent-harness.py"
    try:
        own = (Path(project).resolve() / "tools" / "agent-harness.py") == tool
    except OSError:
        own = False
    if not (own and tool.is_file()) or off(env):
        return ""
    if env.get("PYTEST_CURRENT_TEST") or env.get("HARNESS_FIXTURE_ROOT"):
        return ""
    if installed_version(project) is None:
        return ""                               # a git checkout: nothing to compare, so never ask GitHub
    line = release.note(project, PROFILE)
    try:
        import subprocess
        # A new process group with a HIDDEN console (CREATE_NO_WINDOW), never DETACHED_PROCESS: Windows ignores
        # CREATE_NO_WINDOW beside DETACHED_PROCESS, and a detached process's console children (a venv's python.exe
        # redirector starts one) would each open a visible console window. Popen never waits, so neither does the
        # session start.
        flags = 0x00000200 | 0x08000000 if os.name == "nt" else 0
        subprocess.Popen([sys.executable, "-B", str(tool), "update-check"], cwd=str(project),
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         close_fds=True, creationflags=flags, start_new_session=os.name != "nt")
    except Exception:
        pass
    return line + "\n" if line else ""
