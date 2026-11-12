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


MOVES = {
    "shutil-move-source": "import shutil\nfrom pathlib import Path\nfor p in Path(r'{S}').glob('*.ess'):\n"
                          "    shutil.move(p, 'C:/Temp/')\n",
    "path-rename": "from pathlib import Path\nfor p in Path(r'{S}').glob('*.ess'):\n    p.rename(p.with_suffix('.bak'))\n",
    "path-replace-onto": "from pathlib import Path\nfor p in Path(r'{S}').glob('*.ess'):\n"
                         "    Path(r'C:/Temp/blank.bin').replace(p)\n",
    "os-rename-keywords": "import os\nfor n in os.listdir(r'{S}'):\n"
                          "    os.rename(src=os.path.join(r'{S}', n), dst='C:/Temp/x')\n",
}


LOW_LEVEL = {
    "os-open": "import os\nfrom pathlib import Path\nfor p in Path(r'{S}').glob('*.ess'):\n"
               "    os.close(os.open(p, os.O_WRONLY | os.O_TRUNC))\n",
    "io-fileio": "import io\nfrom pathlib import Path\nfor p in Path(r'{S}').glob('*.ess'):\n    io.FileIO(p, 'w').close()\n",
    "copy-dst-keyword": "import shutil\nfrom pathlib import Path\nfor p in Path(r'{S}').glob('*.ess'):\n"
                        "    shutil.copy2(src='C:/Temp/x.ess', dst=p)\n",
}


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


def test_a_named_script_reading_a_heredoc_is_not_read_as_code(install):
    """`python tool.py <<EOF` hands the body to tool.py as data (writes._script_reads_stdin)."""
    body = 'from pathlib import Path\nfor p in Path(r"' + install["saves"] + '").glob("*.ess"):\n    p.write_bytes(b"")\n'
    (install["work"] / "tool.py").write_text("import sys\nsys.stdin.read()\n")
    cmd = "python " + install["work"].as_posix() + "/tool.py <<'EOF'\n" + body + "EOF"
    assert decide(install, cmd) == "allow"


def _decide_in(inst, command, cwd):
    return shell_rules.decide(command, inst["roots"], Audit(), cwd=cwd, shell="bash", env=inst["env"]).decision


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


def test_text_swapped_inside_a_path_call_is_not_a_location(install):
    """Inside a path call, a str.replace's arguments are text: the folder written is Backups, not Saves."""
    body = ("import os, sys\nrel = sys.argv[2]\n"
            "open(os.path.join(sys.argv[1], rel.replace('Saves', 'Backups')), 'w').close()\n")
    assert decide(install, heredoc(body)) == "allow"


def test_a_dict_label_mentioning_saves_is_not_a_location(install):
    """A value without a separator is a label, not a path."""
    body = ("import json, sys\nfrom pathlib import Path\nmeta = {'title': 'Saves backup report', 'kind': 'x.ess'}\n"
            "Path(sys.argv[1]).write_text(json.dumps(meta))\n")
    assert decide(install, heredoc(body)) == "allow"
