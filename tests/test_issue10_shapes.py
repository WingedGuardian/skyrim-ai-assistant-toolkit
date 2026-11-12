"""Issue #10: read-only audit shapes the INSTALL guard refused must stay allowed by the toolkit guard, while real
save changes stay refused. Parser/decision input only; nothing here is executed."""
import pytest

from test_review_guards import Audit, shell_rules
from skyguard import paths

DOC = '"""Read-only audit. It performs neither runtime persistence nor engine loading."""\n'


@pytest.fixture()
def install(tmp_path):
    game = tmp_path / "Game"
    (game / "Data").mkdir(parents=True)
    (game / "SkyrimVR.exe").write_bytes(b"")
    config = tmp_path / "Documents" / "My Games" / "Skyrim VR"
    saves = config / "Saves"
    saves.mkdir(parents=True)
    for n in ("Save1", "Save2", "Save3"):
        (saves / (n + ".ess")).write_bytes(b"ess")
    work = tmp_path / "work"
    work.mkdir()
    env = {"SKYRIM_GAME_ROOT": str(game), "SKYRIM_CONFIG_DIR": str(config)}
    roots = paths.roots(game.as_posix(), str(game), env)
    return dict(game=game, saves=saves.as_posix(), work=work, roots=roots, env=env)


def decide(inst, command):
    return shell_rules.decide(command, inst["roots"], Audit(), cwd=str(inst["work"]), shell="bash",
                              env=inst["env"]).decision


def scripts(inst):
    w, saves = inst["work"], inst["saves"]
    (w / "deploy_isolation.py").write_text('import shutil\nshutil.copytree("C:/Temp/stage", "C:/Temp/out")\n')
    (w / "freeze_helper.py").write_text(
        'import json, os, sys\nfrom pathlib import Path\nPROTECTED = (".ess", ".skse")\n'
        'files = [p for p in sys.argv[2:] if not p.lower().endswith(PROTECTED)]\n'
        'Path("C:/Temp/freeze-report.json").write_text(json.dumps(files))\n'
        'tmp = Path("C:/Temp/freeze.tmp"); tmp.write_text("x"); os.remove(tmp)\n')
    (w / "profile_probe.py").write_text(
        'import json, shutil\nfrom pathlib import Path\n'
        'listing = sorted(p.name for p in Path(r"' + saves + '").iterdir())\n'
        'Path("C:/Temp/probe-report.json").write_text(json.dumps(listing))\n'
        'shutil.copy2("C:/Temp/profile.ini", "C:/Temp/profile.ini.bak")\n')
    return w.as_posix()


def test_the_five_reported_shapes_are_allowed(install):
    w = scripts(install)
    saves = install["saves"]
    shapes = {
        "source with a docstring and a localhost URL": "cat > " + w + "/audit_src.py <<'EOF'\n" + DOC +
        'URL = "http://127.0.0.1:8921/api/health"\nEOF',
        "hash audit of the saves plus a report": "python - <<'EOF'\n" + DOC +
        "import hashlib, json\nfrom pathlib import Path\n"
        'h = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in Path(r"' + saves + '").glob("*.ess")}\n'
        'Path("C:/Temp/audit-report.json").write_text(json.dumps(h))\nEOF',
        "a helper whose name ends in _isolation.py": "python " + w + "/deploy_isolation.py",
        "a freeze helper that refuses save extensions": "python " + w + "/freeze_helper.py freeze a.txt b.esp",
        "a profile probe that lists the saves folder": "python " + w + "/profile_probe.py",
    }
    decisions = {name: decide(install, cmd) for name, cmd in shapes.items()}
    assert all(d == "allow" for d in decisions.values()), decisions


# ---- issue #26: a computed write target in code that names a save location cannot be counted or backed up.
# One test per clause of writes.python_computed_writes and of the location check in writes.scan. A one- or two-save
# change spelled literally stays allowed with its pair backup (test_review_v4_guard_gaps). Known limit (4.2): a script
# FILE is not read, so `python probe.py` doing the same is not seen (saves.py KNOWN LIMITS).
def heredoc(body):
    return "python - <<'EOF'\n" + DOC + body + "EOF"


def test_the_saves_folder_itself_is_still_refused(install):
    """4.1 (user ruling 2026-11-10): no rule decides on a save, the save vault keeps every version. The whole Saves
    folder, everything in it at once, and the folder holding it still cannot be deleted or moved."""
    saves = install["saves"]
    assert decide(install, 'rm "' + saves + '"/*.ess') == "allow"                  # saves by name pattern: the vault
    for refused in ('rm -rf "' + saves + '"', 'rm -rf "' + saves + '"/*', 'mv "' + saves + '" /c/Temp/elsewhere',
                    'rm -rf "' + saves.rsplit("/", 1)[0] + '"',
                    "python - <<'X'\nimport shutil\nshutil.rmtree(r'" + saves + "')\nX"):
        assert decide(install, refused) == "deny", refused
