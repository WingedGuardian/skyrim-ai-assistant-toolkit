"""#26 round 8 review: positional/unbound Path.unlink, counted functions passed as values, and bash quoting inside the
save words. Decision input only; nothing here is executed."""
import pytest

from test_issue10_shapes import heredoc, install  # noqa: F401  (install is a fixture)
from test_issue26_round4 import _cwd_is_unknown, decide_in

FROM_SAVES = {
    "unlink-positional": "from pathlib import Path\nfor p in Path('.').glob('*'):\n    p.unlink(True)\n",
    "unlink-unbound": "from pathlib import Path\nfor p in Path('.').glob('*'):\n    Path.unlink(p)\n",
    "unlink-unbound-module": "import pathlib\nfor p in pathlib.Path('.').glob('*'):\n    pathlib.Path.unlink(p)\n",
    "map-os-remove": "import os\nlist(map(os.remove, os.listdir()))\n",
    "map-path-unlink": "from pathlib import Path\nlist(map(Path.unlink, Path('.').iterdir()))\n",
    "map-pathlib-unlink": "import pathlib\nlist(map(pathlib.Path.unlink, pathlib.Path('.').iterdir()))\n",
    "map-imported-remove": "import os\nfrom os import remove\nlist(map(remove, os.listdir()))\n",
    "assignment-alias": "import os\nrep = os.remove\nfor f in os.listdir():\n    rep(f)\n",
}


@pytest.mark.parametrize("body", list(FROM_SAVES.values()), ids=list(FROM_SAVES))
def test_round_eight_delete_shapes_run_from_saves_are_refused(install, body):
    assert decide_in(install, heredoc(body), install["saves"]) == "deny"


RD = " python -c \"import os;[os.remove(f) for f in os.listdir()]\""
QUOTED = {
    "empty-single": "cd \"$D\"/Sa''ves&&" + RD,
    "empty-double": 'cd "$D"/Sa""ves&&' + RD,
    "backslash": 'cd "$D"/S\\aves&&' + RD,
    "ansi-c": "cd \"$D\"/$'Sav\\x65s'&&" + RD,
    "quoted-space": "cd \"$D\"/My' 'Games/Skyrim' 'VR;" + RD,
}


def test_a_windows_backslash_path_naming_saves_is_seen_raw(install):
    """Taking bash quoting out turns `D:\\Games\\Saves` into `D:GamesSaves`: only the raw text names the folder."""
    cmd = "D='D:\\Games\\Saves'; cd \"$(echo \"$D\")\";" + RD
    assert _cwd_is_unknown(cmd)
    assert decide_in(install, cmd, str(install["work"])) == "deny"


@pytest.mark.parametrize("cmd", list(QUOTED.values()), ids=list(QUOTED))
def test_bash_quoting_inside_a_save_word_is_seen(install, cmd):
    cmd = 'D="$(cat C:/Temp/where.txt)"; ' + cmd
    assert _cwd_is_unknown(cmd)
    assert decide_in(install, cmd, str(install["work"])) == "deny"


@pytest.mark.parametrize("word", ["my-saves", "old.saves", "autosaves"])
def test_a_word_that_only_contains_saves_is_not_a_location(install, word):
    """The lookbehind and lookahead: `my-saves`, `old.saves`, `autosaves` are not the Saves component."""
    w = install["work"].as_posix()
    cmd = ('S="' + w + '"; cd "$S"; python -c "import zipfile; zipfile.ZipFile(\'' + word + '.zip\').extractall(\'.\')"; '
           'cmd.exe //c "$(cygpath -w "$S/b.bat")"')
    assert _cwd_is_unknown(cmd)
    assert decide_in(install, cmd, w) == "allow"


def test_a_read_only_map_over_a_scratch_folder_is_allowed(install):
    body = "import glob, os\nprint(list(map(os.path.getsize, glob.glob('C:/Temp/build/*'))))\n"
    assert decide_in(install, heredoc(body), install["saves"]) == "allow"


# --- round 9 review (round 10 fixes)
ROUND10_FROM_SAVES = {
    "bound-method-value": "from pathlib import Path\nfor f in [p.unlink for p in Path('.').iterdir()]:\n    f()\n",
    "nt-remove": "import nt, os\nfor n in os.listdir():\n    nt.remove(n)\n",
    "from-nt-unlink": "import os\nfrom nt import unlink\nfor n in os.listdir():\n    unlink(n)\n",
    "posix-remove": "import posix, os\nfor n in os.listdir():\n    posix.remove(n)\n",
}


@pytest.mark.parametrize("body", list(ROUND10_FROM_SAVES.values()), ids=list(ROUND10_FROM_SAVES))
def test_round_nine_delete_shapes_run_from_saves_are_refused(install, body):
    assert decide_in(install, heredoc(body), install["saves"]) == "deny"


ROUND10_QUOTED = {
    "locale-string": 'cd "$D"/Sa$"ves"&&' + RD,
    "line-continuation": 'cd "$D"/Sa\\\nves&&' + RD,
}


@pytest.mark.parametrize("cmd", list(ROUND10_QUOTED.values()), ids=list(ROUND10_QUOTED))
def test_more_bash_quoting_inside_a_save_word_is_seen(install, cmd):
    cmd = 'D="$(cat C:/Temp/where.txt)"; ' + cmd
    assert _cwd_is_unknown(cmd)
    assert decide_in(install, cmd, str(install["work"])) == "deny"
