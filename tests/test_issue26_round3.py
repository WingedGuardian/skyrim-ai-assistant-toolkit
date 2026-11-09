"""#26 round 3: the round-2 review's measured shapes. Decision input only; nothing here is executed."""
import pytest

from test_issue10_shapes import Audit, decide, heredoc, install, shell_rules  # noqa: F401  (install is a fixture)


def _docs(inst):
    return inst["saves"].rsplit("/My Games/", 1)[0]


# ---- finding 1: ordinary agent code that changes saves, the location outside round 2's path positions
MISSED = {
    "helper-function": "import os\ndef bak(d):\n    for n in os.listdir(d):\n        if n.endswith('.ess'):\n"
                       "            os.rename(os.path.join(d, n), os.path.join(d, n + '.bak'))\nbak(r'{S}')\n",
    "default-parameter": "import os\ndef wipe(d=r'{S}'):\n    for n in os.listdir(d):\n"
                         "        if n.lower().endswith(('.ess', '.skse')):\n"
                         "            open(os.path.join(d, n), 'wb').close()\nwipe()\n",
    "argv-fallback": "import os, sys\nd = sys.argv[1] if len(sys.argv) > 1 else r'{S}'\nfor n in os.listdir(d):\n"
                     "    open(os.path.join(d, n), 'wb').close()\n",
    "tuple-constant": "from pathlib import Path\nS, B = r'{S}', 'x'\nfor p in Path(S).glob('*'):\n    p.write_bytes(b'')\n",
    "helper-move": "import os, shutil\ndef stash(src):\n    for n in os.listdir(src):\n"
                   "        shutil.move(os.path.join(src, n), 'C:/Temp/')\nstash(r'{S}')\n",
    "helper-restore": "import glob, shutil\ndef restore(dst):\n    for n in glob.glob(dst + '/*'):\n"
                      "        shutil.copy('C:/Temp/blank', n)\nrestore(r'{S}')\n",
    "argparse-default": "import argparse, glob\nap = argparse.ArgumentParser()\nap.add_argument('--dir', default=r'{S}')\n"
                        "a = ap.parse_args()\nfor n in glob.glob(a.dir + '/*'):\n    open(n, 'wb').close()\n",
    "augmented": "import glob\nd = r'{D}'\nd += '/My Games/Skyrim VR/Saves'\nfor n in glob.glob(d + '/*'):\n"
                 "    open(n, 'wb').close()\n",
    "walk-endswith": "import os\nfor root, dirs, files in os.walk(r'{D}'):\n    for f in files:\n"
                     "        if f.endswith('.ess'):\n            open(os.path.join(root, f), 'wb').close()\n",
    "suffix-compare": "import os\nfrom pathlib import Path\nfor p in Path(os.environ['X']).rglob('*'):\n"
                      "    if p.suffix == '.ess':\n        p.write_bytes(b'')\n",
    "percent-format": "import glob\nD = r'{D}'\nfor n in glob.glob('%s/My Games/Skyrim VR/Saves/*' % D):\n"
                      "    open(n, 'wb').close()\n",
}


@pytest.mark.parametrize("body", list(MISSED.values()), ids=list(MISSED))
def test_ordinary_code_that_changes_saves_is_refused(install, body):
    assert decide(install, heredoc(body.format(S=install["saves"], D=_docs(install)))) == "deny"


# ---- finding 2: audits and copy-outs whose destination is literal, through a name or a literal folder
ALLOWED = {
    "report-path-in-a-name": "import json\nfrom pathlib import Path\nOUT = 'C:/Temp/report.json'\n"
                             "rows = [p.name for p in Path(r'{S}').glob('*.ess')]\nopen(OUT, 'w').write(json.dumps(rows))\n",
    "manifest-with": "import hashlib\nfrom pathlib import Path\nout = r'C:/Temp/manifest.txt'\nwith open(out, 'w') as f:\n"
                     "    for p in Path(r'{S}').glob('*.ess'):\n        f.write(hashlib.sha256(p.read_bytes()).hexdigest())\n",
    "report-path-object": "from pathlib import Path\nout = Path('C:/Temp/report.json')\n"
                          "out.write_text(str([p.name for p in Path(r'{S}').glob('*.ess')]))\n",
    "copy-out-to-backup": "import shutil\nfrom pathlib import Path\nfor p in Path(r'{S}').glob('*.ess'):\n"
                          "    shutil.copy2(p, Path('C:/Temp/bk') / p.name)\n",
}


@pytest.mark.parametrize("body", list(ALLOWED.values()), ids=list(ALLOWED))
def test_reading_saves_and_writing_to_a_literal_place_is_allowed(install, body):
    assert decide(install, heredoc(body.format(S=install["saves"]))) == "allow"


def test_a_save_named_through_a_name_assigned_once_is_backed_up(install):
    """`p = r'<Saves>/Save1.ess'; open(p, 'wb')`: one known save, so the usual pair backup, not a refusal."""
    body = "p = r'" + install["saves"] + "/Save1.ess'\nopen(p, 'wb').close()\n"
    v = shell_rules.decide(heredoc(body), install["roots"], Audit(), cwd=str(install["work"]), shell="bash",
                           env=install["env"])
    assert v.decision == "allow" and v.saves is not None and v.saves.change.targets, v


def test_a_copy_anchored_inside_the_saves_folder_is_refused(install):
    """A literal folder that IS the Saves folder: the anchored write lands among the saves."""
    body = ("import shutil, sys\nfrom pathlib import Path\nfor n in sys.argv[1:]:\n"
            "    shutil.copy2('C:/Temp/x', Path(r'" + install["saves"] + "') / n)\n")
    assert decide(install, heredoc(body)) == "deny"


def test_a_name_bound_twice_is_not_a_literal(install):
    """`out` is also a loop variable, so it is computed, not the literal report path."""
    body = ("import sys\nfrom pathlib import Path\nout = 'C:/Temp/r.json'\n"
            "names = [p.name for p in Path(r'" + install["saves"] + "').glob('*.ess')]\n"
            "for out in sys.argv[1:]:\n    open(out, 'w').write(str(names))\n")
    assert decide(install, heredoc(body)) == "deny"


TOKENS = {
    "assigned-token": "import os\nfrom pathlib import Path\nSAVES = 'Saves'\n"
                      "for p in (Path(os.environ['D']) / SAVES).iterdir():\n    p.write_bytes(b'')\n",
    "looped-tokens": "import os\nfrom pathlib import Path\nfor sub in ('Saves', 'Logs'):\n"
                     "    for p in (Path(os.environ['D']) / sub).iterdir():\n        p.write_bytes(b'')\n",
    "concatenated-token": "import os\nS = os.environ['D'] + os.sep + 'Saves'\nfor n in os.listdir(S):\n"
                          "    open(os.path.join(S, n), 'wb').close()\n",
}


@pytest.mark.parametrize("body", list(TOKENS.values()), ids=list(TOKENS))
def test_a_bare_saves_token_in_a_value_names_the_location(install, body):
    assert decide(install, heredoc(body)) == "deny"


TEXT = {
    "command-lines-as-data": "import json, sys\nfrom pathlib import Path\n"
                             "rows = [('a', 'R=tools/resaver-cli.sh; $R reset-havok s.ess'), ('b', 'ls Saves/x.ess')]\n"
                             "Path(sys.argv[1]).write_text(json.dumps(rows))\n",
    "logged-path": "import logging, sys\nfrom pathlib import Path\nlogging.info('C:/Users/x/Documents/My Games/Skyrim VR/Saves')\n"
                   "Path(sys.argv[1]).write_text('ok')\n",
    "written-content": "import sys\nfrom pathlib import Path\nPath(sys.argv[1]).write_text('C:/Users/x/My Games/Skyrim VR/Saves/a.ess')\n",
    "list-of-command-strings": "import json, sys\nfrom pathlib import Path\ncmds = ['ls Saves/x.ess', 'b']\n"
                               "Path(sys.argv[1]).write_text(json.dumps(cmds))\n",
    "piped-command-line": "import json, sys\nfrom pathlib import Path\n"
                          "rows = [('a', 'C:/tools/resaver.exe info C:/x/Saves/a.ess | head')]\n"
                          "Path(sys.argv[1]).write_text(json.dumps(rows))\n",
    "path-line-with-newline": "import sys\nfrom pathlib import Path\nnote = 'C:/Users/x/Documents/My Games/Skyrim VR/Saves\\n'\n"
                              "Path(sys.argv[1]).write_text(note)\n",
    "path-swapped-in-an-edit": "import sys\nfrom pathlib import Path\np = Path(sys.argv[1])\n"
                               "t = p.read_text().replace('C:/old/place', 'C:/Users/x/My Games/Skyrim VR/Saves')\n"
                               "p.write_text(t)\n",
}


def test_a_docstring_naming_the_saves_folder_is_not_a_location(install):
    body = ('"""Writes a report about C:/Users/x/Documents/My Games/Skyrim VR/Saves without touching it."""\n'
            "import sys\nfrom pathlib import Path\nPath(sys.argv[1]).write_text('ok')\n")
    assert decide(install, "python - <<'EOF'\n" + body + "EOF") == "allow"


@pytest.mark.parametrize("body", list(TEXT.values()), ids=list(TEXT))
def test_text_shaped_like_a_save_path_is_not_a_location(install, body):
    assert decide(install, heredoc(body)) == "allow"
