"""#26 round 7 review: the unknown-cwd word scan's spellings, star imports of every counted module, and computed
deletes (the user's ruling covers write AND delete targets; a computed delete run from inside Saves was allowed in
4.0). Decision input only; nothing here is executed."""
import pytest

from test_issue10_shapes import heredoc, install  # noqa: F401  (install is a fixture)
from test_issue26_round4 import _cwd_is_unknown, decide_in

RN = " python -c \"import os;[os.rename(f, f.lower()) for f in os.listdir()]\""

OPERATOR_SPELLINGS = {
    "and-and": 'cd "$D"/Saves&&' + RN,
    "or-or": 'cd "$D"/Saves|| exit;' + RN,
    "redirect": 'cd "$D"/Saves>/dev/null;' + RN,
    "backtick": 'cd `echo "$D"`/Saves&&' + RN,
}


@pytest.mark.parametrize("cmd", list(OPERATOR_SPELLINGS.values()), ids=list(OPERATOR_SPELLINGS))
def test_saves_followed_by_a_shell_operator_is_seen(install, cmd):
    cmd = "D=\"$(cat C:/Temp/where.txt)\"; " + cmd
    assert _cwd_is_unknown(cmd)
    assert decide_in(install, cmd, str(install["work"])) == "deny"


ALTERNATIVES = {
    "escaped-my-games": 'cd "$(cat C:/Temp/d.txt)"/My\\ Games/Skyrim\\ VR;' + RN,
    "percent-my-games": 'cd "$(cat C:/Temp/d.txt)/My%20Games/Skyrim VR";' + RN,
    "save-name-only": 'cd "$(dirname "$(find C:/ -maxdepth 6 -name \'Save1.ess\' | head -1)")";' + RN,
    "save-dirs-variable": 'cd "$(echo "$SKYRIM_SAVE_DIRS" | cut -d";" -f1)";' + RN,
}


@pytest.mark.parametrize("cmd", list(ALTERNATIVES.values()), ids=list(ALTERNATIVES))
def test_each_way_a_command_names_a_save_location_is_seen(install, cmd):
    assert _cwd_is_unknown(cmd)
    assert decide_in(install, cmd, str(install["work"])) == "deny"


STAR = {
    "io-fileio-literal": "from io import *\nFileIO(r'{S}/Save1.ess', 'w').close()\n",
    "io-fileio-loop": "import glob\nfrom io import *\nfor p in glob.glob(r'{S}/*.ess'):\n    FileIO(p, 'w').close()\n",
    "gzip-class": "import glob\nfrom gzip import *\nfor p in glob.glob(r'{S}/*.ess'):\n    GzipFile(p, 'wb').close()\n",
    "gzip-open-by-name": "import glob\nfrom gzip import open as gopen\nfor p in glob.glob(r'{S}/*.ess'):\n    gopen(p, 'wb').close()\n",
    "tarfile-class": "import glob\nfrom tarfile import *\nfor p in glob.glob(r'{S}/*.ess'):\n    TarFile(p, 'w').close()\n",
    "zipfile-class": "import glob\nfrom zipfile import *\nfor p in glob.glob(r'{S}/*.ess'):\n    ZipFile(p, 'w').close()\n",
    "shutil-unpack": "from shutil import *\nunpack_archive('C:/Temp/b.zip', r'{S}')\n",
    "os-link": "import glob\nfrom os import *\nfor p in glob.glob('C:/Temp/*.ess'):\n    link(p, r'{S}/' + p[-9:])\n",
}


@pytest.mark.parametrize("body", list(STAR.values()), ids=list(STAR))
def test_star_imports_of_every_counted_module_are_read(install, body):
    assert decide_in(install, heredoc(body.format(S=install["saves"])), str(install["work"])) == "deny"


DELETES_FROM_SAVES = {
    "os-remove-listdir": "import os\n[os.remove(f) for f in os.listdir()]\n",
    "path-unlink-iterdir": "from pathlib import Path\n[p.unlink() for p in Path.cwd().iterdir()]\n",
    "os-unlink-star": "from os import *\nfor f in listdir():\n    unlink(f)\n",
    "send2trash-listdir": "import os\nfrom send2trash import send2trash\nsend2trash(os.listdir())\n",
}


@pytest.mark.parametrize("body", list(DELETES_FROM_SAVES.values()), ids=list(DELETES_FROM_SAVES))
def test_a_computed_delete_run_from_the_saves_folder_is_refused(install, body):
    assert decide_in(install, heredoc(body), install["saves"]) == "deny"


def test_a_computed_delete_from_a_cd_into_saves_is_refused(install):
    cmd = 'cd "' + install["saves"] + '" && python -c "import os;[os.remove(f) for f in os.listdir()]"'
    assert decide_in(install, cmd, str(install["work"])) == "deny"


def test_a_computed_delete_from_a_named_unknown_cwd_is_refused(install):
    cmd = 'cd "$(cat C:/Temp/d.txt)"/Saves&& python -c "import os;[os.remove(f) for f in os.listdir()]"'
    assert _cwd_is_unknown(cmd)
    assert decide_in(install, cmd, str(install["work"])) == "deny"


# --- reverse: ordinary deletes elsewhere stay allowed

def test_a_computed_delete_of_scratch_files_is_allowed(install):
    body = "import glob, os\nfor f in glob.glob('C:/Temp/build/*.tmp'):\n    os.remove(f)\n"
    assert decide_in(install, heredoc(body), str(install["work"])) == "allow"


def test_a_literal_delete_of_a_scratch_file_is_allowed(install):
    body = "import os\nos.remove('C:/Temp/build/x.tmp')\n"
    assert decide_in(install, heredoc(body), str(install["work"])) == "allow"


def test_a_word_containing_saves_is_not_a_location(install):
    """`autosaves`, `saves.json` and `savestate` are not the Saves component (word boundary, not substring)."""
    cmd = ('S="' + install["work"].as_posix() + '"; cd "$S"; python -c "import zipfile; '
           "zipfile.ZipFile('autosaves-x.zip').extractall('.')\"; cmd.exe //c \"$(cygpath -w \"$S/b.bat\")\"")
    assert _cwd_is_unknown(cmd)
    assert decide_in(install, cmd, str(install["work"])) == "allow"


def test_an_escaped_space_in_a_custom_settings_folder_is_resolved(tmp_path):
    """No Saves / My Games words: only the bash-unescaped word `.../my\ cfg` resolves into the settings area."""
    from test_issue10_shapes import paths
    game, config = tmp_path / "Game", tmp_path / "my cfg"
    (game / "Data").mkdir(parents=True)
    (config / "Saves").mkdir(parents=True)
    env = {"SKYRIM_GAME_ROOT": str(game), "SKYRIM_CONFIG_DIR": str(config)}
    inst = dict(roots=paths.roots(game.as_posix(), str(game), env), env=env)
    cmd = "D=" + config.as_posix().replace(" ", "\ ") + "; cd \"$(echo \"$D\")\";" + RN
    assert _cwd_is_unknown(cmd)
    assert decide_in(inst, cmd, str(tmp_path)) == "deny"
