"""#26 review round 1: built save paths, moves away, os.open / io.FileIO, and read-only module opens.
Decision input only; nothing here is executed."""
import pytest

from test_issue10_shapes import Audit, decide, heredoc, install, shell_rules  # noqa: F401  (install is a fixture)


def _docs(inst):
    return inst["saves"].rsplit("/My Games/", 1)[0]


BUILT = {
    "os-path-join": "import glob, os\nS = os.path.join(r'{D}', 'My Games', 'Skyrim VR', 'Saves')\n"
                    "for n in glob.glob(os.path.join(S, '*.ess')):\n    open(n, 'wb').close()\n",
    "path-chain": "from pathlib import Path\nfor p in (Path(r'{D}') / 'My Games' / 'Skyrim VR' / 'Saves').glob('*.ess'):\n"
                  "    p.write_bytes(b'')\n",
    "f-string": "from pathlib import Path\nD = r'{D}'\nfor p in Path(f'{{D}}/My Games/Skyrim VR/Saves').glob('*.ess'):\n"
                "    p.write_bytes(b'')\n",
}


@pytest.mark.parametrize("body", list(BUILT.values()), ids=list(BUILT))
def test_a_save_path_built_from_parts_is_still_named(install, body):
    assert decide(install, heredoc(body.format(D=_docs(install)))) == "deny"


MOVES = {
    "shutil-move-source": "import shutil\nfrom pathlib import Path\nfor p in Path(r'{S}').glob('*.ess'):\n"
                          "    shutil.move(p, 'C:/Temp/')\n",
    "path-rename": "from pathlib import Path\nfor p in Path(r'{S}').glob('*.ess'):\n    p.rename(p.with_suffix('.bak'))\n",
    "path-replace-onto": "from pathlib import Path\nfor p in Path(r'{S}').glob('*.ess'):\n"
                         "    Path(r'C:/Temp/blank.bin').replace(p)\n",
    "os-rename-keywords": "import os\nfor n in os.listdir(r'{S}'):\n"
                          "    os.rename(src=os.path.join(r'{S}', n), dst='C:/Temp/x')\n",
}


@pytest.mark.parametrize("body", list(MOVES.values()), ids=list(MOVES))
def test_a_save_moved_away_or_replaced_is_counted(install, body):
    assert decide(install, heredoc(body.format(S=install["saves"]))) == "deny"


LOW_LEVEL = {
    "os-open": "import os\nfrom pathlib import Path\nfor p in Path(r'{S}').glob('*.ess'):\n"
               "    os.close(os.open(p, os.O_WRONLY | os.O_TRUNC))\n",
    "io-fileio": "import io\nfrom pathlib import Path\nfor p in Path(r'{S}').glob('*.ess'):\n    io.FileIO(p, 'w').close()\n",
    "copy-dst-keyword": "import shutil\nfrom pathlib import Path\nfor p in Path(r'{S}').glob('*.ess'):\n"
                        "    shutil.copy2(src='C:/Temp/x.ess', dst=p)\n",
}


@pytest.mark.parametrize("body", list(LOW_LEVEL.values()), ids=list(LOW_LEVEL))
def test_low_level_writes_onto_a_computed_save_are_counted(install, body):
    assert decide(install, heredoc(body.format(S=install["saves"]))) == "deny"


READS = {
    "gzip-open": "import gzip\nfrom pathlib import Path\nfor p in Path(r'{S}').glob('*.ess'):\n    gzip.open(p).close()\n",
    "tarfile-open": "import tarfile\nfrom pathlib import Path\nfor p in Path(r'{S}').glob('*.ess'):\n"
                    "    tarfile.open(p).close()\n",
    "codecs-open-read": "import codecs\nfrom pathlib import Path\nfor p in Path(r'{S}').glob('*.ess'):\n"
                        "    codecs.open(p, 'r', 'utf-8').close()\n",
    "open-rb": "from pathlib import Path\nfor p in Path(r'{S}').glob('*.ess'):\n    open(p, 'rb').close()\n",
    "os-open-read-only": "import os\nfrom pathlib import Path\nfor p in Path(r'{S}').glob('*.ess'):\n"
                         "    os.close(os.open(p, os.O_RDONLY))\n",
    "str-replace": "from pathlib import Path\nnames = [p.name.replace('.ess', '') for p in Path(r'{S}').glob('*.ess')]\n"
                   "Path('C:/Temp/names.txt').write_text(chr(10).join(names))\n",
}


@pytest.mark.parametrize("body", list(READS.values()), ids=list(READS))
def test_reading_the_saves_in_a_loop_stays_allowed(install, body):
    assert decide(install, heredoc(body.format(S=install["saves"]))) == "allow"


def test_the_refusal_says_what_to_do_instead(install):
    body = 'from pathlib import Path\nfor p in Path(r"' + install["saves"] + '").glob("*.ess"):\n    p.write_bytes(b"")\n'
    v = shell_rules.decide(heredoc(body), install["roots"], Audit(), cwd=str(install["work"]), shell="bash",
                           env=install["env"])
    assert v.decision == "deny" and "computes the target" in v.reason and "split read and write" in v.reason, v.reason


def test_a_named_script_reading_a_heredoc_is_not_read_as_code(install):
    """`python tool.py <<EOF` hands the body to tool.py as data (writes._script_reads_stdin)."""
    body = 'from pathlib import Path\nfor p in Path(r"' + install["saves"] + '").glob("*.ess"):\n    p.write_bytes(b"")\n'
    (install["work"] / "tool.py").write_text("import sys\nsys.stdin.read()\n")
    cmd = "python " + install["work"].as_posix() + "/tool.py <<'EOF'\n" + body + "EOF"
    assert decide(install, cmd) == "allow"


def _decide_in(inst, command, cwd):
    return shell_rules.decide(command, inst["roots"], Audit(), cwd=cwd, shell="bash", env=inst["env"]).decision


def test_a_relative_literal_run_inside_the_saves_folder_names_it(install):
    """Only resolution says `.` is the Saves folder: no literal spells Saves or .ess."""
    body = "from pathlib import Path\nfor p in Path('.').iterdir():\n    p.write_bytes(b'')\n"
    assert _decide_in(install, heredoc(body), install["saves"]) == "deny"


def test_a_relative_literal_run_inside_the_settings_folder_names_it(install):
    body = "from pathlib import Path\nfor p in Path('.').rglob('*'):\n    p.write_bytes(b'')\n"
    assert _decide_in(install, heredoc(body), install["saves"].rsplit("/", 1)[0]) == "deny"


def test_a_saves_component_alone_names_the_location(install):
    """No literal resolves and none ends .ess: only the `Saves` path component says these are saves."""
    body = ("import os\nfrom pathlib import Path\nfor p in (Path(os.environ['DOCS']) / 'Saves').glob('*'):\n"
            "    p.write_bytes(b'')\n")
    assert decide(install, heredoc(body)) == "deny"


def test_a_save_pattern_alone_names_the_location(install):
    """The folder comes from the environment; only the `*.ess` pattern says these are saves."""
    body = ("import os\nfrom pathlib import Path\nfor p in Path(os.environ['SAVEDIR']).glob('*.ess'):\n"
            "    p.write_bytes(b'')\n")
    assert decide(install, heredoc(body)) == "deny"


# ---- #26 pricing round: only literals in path positions name a location. 22 of 23 refusals in recorded history
# were agents editing their own source or notes, whose replacement TEXT mentioned Saves or .ess.
def test_an_assigned_save_folder_names_the_location(install):
    body = ("import glob, os\nSAVES = r'" + install["saves"] + "'\nfor n in os.listdir(SAVES):\n"
            "    open(os.path.join(SAVES, n), 'wb').close()\n")
    assert decide(install, heredoc(body)) == "deny"


def test_a_path_call_argument_alone_names_the_location(install):
    """No loop, no assignment, no `/`: only os.path.join's argument says this is a save."""
    body = "import os\nopen(os.path.join(os.environ['D'], 'Save1.ess'), 'wb').close()\n"
    assert decide(install, heredoc(body)) == "deny"


def test_a_div_operand_alone_names_the_location(install):
    """No loop, no assignment, no path call around the literals: only the `/` operands name the save."""
    body = "import os\nfrom pathlib import Path\n(Path(os.environ['DOCS']) / 'Saves' / 'Save1.ess').write_bytes(b'')\n"
    assert decide(install, heredoc(body)) == "deny"


def test_iterating_a_literal_list_of_saves_names_the_location(install):
    body = ("for n in [r'" + install["saves"] + "/Save1.ess', r'" + install["saves"] + "/Save2.ess']:\n"
            "    open(n, 'wb').close()\n")
    assert decide(install, heredoc(body)) == "deny"


EDITS = {
    "replace-text": "from pathlib import Path\np = Path(__file__).with_name('notes.md')\nt = p.read_text()\n"
                    "t = t.replace('old line', 'saves go under My Games/Skyrim VR/Saves and end .ess')\np.write_text(t)\n",
    "membership-test": "import sys\nfrom pathlib import Path\np = Path(sys.argv[1])\nt = p.read_text()\n"
                       "print('Saves' in t, t.count('.ess'))\np.write_text(t.upper())\n",
    "multi-line-text": "import sys\nfrom pathlib import Path\nbody = '''rule text\nC:/Users/x/Documents/My Games/Skyrim VR/Saves\n'''\n"
                       "Path(sys.argv[1]).write_text(body)\n",
}


@pytest.mark.parametrize("body", list(EDITS.values()), ids=list(EDITS))
def test_editing_text_that_mentions_saves_stays_allowed(install, body):
    assert decide(install, heredoc(body)) == "allow"


def test_data_rows_built_by_a_function_are_not_a_location(install):
    """Pricing round 2: corpus rows (`rows = [row('id', 'tool s.ess', ...)]`) written to a computed file."""
    body = ("import json, sys\nfrom pathlib import Path\ndef row(i, cmd):\n    return {'id': i, 'command': cmd}\n"
            "rows = [row('a', 'resaver reset-havok s.ess'), row('b', 'ls Saves/x.ess')]\n"
            "Path(sys.argv[1]).write_text(json.dumps(rows))\n")
    assert decide(install, heredoc(body)) == "allow"


def test_an_environment_default_names_the_location(install):
    body = ("import os\nfrom pathlib import Path\nS = os.environ.get('SAVES', r'" + install["saves"] + "')\n"
            "for p in Path(S).iterdir():\n    p.write_bytes(b'')\n")
    assert decide(install, heredoc(body)) == "deny"


def test_an_assigned_path_reshaped_by_a_text_method_names_the_location(install):
    """`S = r'<Saves>'.replace('/', '\\\\')`: the receiver is the path, whatever the method swaps."""
    body = ("import os\nS = r'" + install["saves"] + "'.replace('/', os.sep)\n"
            "for n in os.listdir(S):\n    open(os.path.join(S, n), 'wb').close()\n")
    assert decide(install, heredoc(body)) == "deny"


def test_text_swapped_inside_a_path_call_is_not_a_location(install):
    """Inside a path call, a str.replace's arguments are text: the folder written is Backups, not Saves."""
    body = ("import os, sys\nrel = sys.argv[2]\n"
            "open(os.path.join(sys.argv[1], rel.replace('Saves', 'Backups')), 'w').close()\n")
    assert decide(install, heredoc(body)) == "allow"


def test_a_save_path_kept_in_a_dict_names_the_location(install):
    """User decision 2026-11-09: a dict value shaped like a path counts."""
    body = ("from pathlib import Path\ncfg = {'folder': r'" + install["saves"] + "'}\n"
            "for p in Path(cfg['folder']).glob('*'):\n    p.write_bytes(b'')\n")
    assert decide(install, heredoc(body)) == "deny"


def test_a_dict_label_mentioning_saves_is_not_a_location(install):
    """A value without a separator is a label, not a path."""
    body = ("import json, sys\nfrom pathlib import Path\nmeta = {'title': 'Saves backup report', 'kind': 'x.ess'}\n"
            "Path(sys.argv[1]).write_text(json.dumps(meta))\n")
    assert decide(install, heredoc(body)) == "allow"
