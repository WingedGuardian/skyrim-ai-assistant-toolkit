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


def test_a_computed_write_into_the_saves_folder_is_refused(install):
    """write_bytes on a glob result, the folder named by a literal: the reported shape."""
    cmd = heredoc('from pathlib import Path\nfor p in Path(r"' + install["saves"] + '").glob("*.ess"):\n'
                  '    p.write_bytes(b"")\n')
    assert decide(install, cmd) == "deny"


def test_a_computed_open_over_a_save_glob_is_refused(install):
    """open(name, 'wb') on glob.glob(<literal ending .ess>). The saves sit in a custom save folder (sLocalSavePath
    need not be called Saves), so only the literal's save name tells the guard these are saves."""
    custom = install["saves"].rsplit("/", 1)[0] + "/MyChar"
    cmd = heredoc('import glob\nfor n in glob.glob(r"' + custom + '/*.ess"):\n    open(n, "wb").close()\n')
    assert decide(install, cmd) == "deny"


def test_a_computed_copy_onto_saves_is_refused(install):
    cmd = heredoc("import shutil\nfrom pathlib import Path\nS = Path(r'" + install["saves"] + "')\n"
                  "for n in ('Save1.ess', 'Save2.ess', 'Save3.ess'):\n    shutil.copy2('C:/Temp/x.ess', S / n)\n")
    assert decide(install, cmd) == "deny"


def test_a_computed_write_below_the_settings_folder_is_refused(install):
    """Only the settings folder is named; the Saves part is computed."""
    settings = install["saves"].rsplit("/", 1)[0]
    cmd = heredoc("from pathlib import Path\nfor p in (Path(r'" + settings + "') / 'Saves').glob('*.ess'):\n"
                  "    p.write_text('')\n")
    assert decide(install, cmd) == "deny"


def test_an_open_whose_mode_is_chosen_at_run_time_counts_as_a_write(install):
    cmd = heredoc("import sys\nfrom pathlib import Path\nmode = sys.argv[-1]\n"
                  "for p in Path(r'" + install["saves"] + "').glob('*.ess'):\n    open(p, mode).close()\n")
    assert decide(install, cmd) == "deny"


def test_a_computed_report_path_beside_a_saves_read_is_refused_by_design(install):
    """The accepted cost of the rule: the hash audit with a COMPUTED report path. The guard cannot tell where a
    computed path lands, so code that also names the Saves folder is refused; spell the report path literally,
    or split the read from the write (the #10 shapes, with a literal report path, stay allowed)."""
    cmd = heredoc("import hashlib, json, os\nfrom pathlib import Path\n"
                  "h = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in Path(r'" + install["saves"] +
                  "').glob('*.ess')}\nPath(os.environ['TEMP'], 'r.json').write_text(json.dumps(h))\n")
    assert decide(install, cmd) == "deny"


def test_real_save_changes_are_still_refused(install):
    saves = install["saves"]
    assert decide(install, 'rm "' + saves + '"/*.ess') == "deny"
    assert decide(install, "python - <<'X'\nimport os\nos.remove(r'" + saves + "/Save1.ess')\nX") == "deny"
    assert decide(install, "python - <<'X'\nimport shutil\nshutil.rmtree(r'" + saves + "')\nX") == "deny"
