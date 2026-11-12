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


ALTERNATIVES = {
    "escaped-my-games": 'cd "$(cat C:/Temp/d.txt)"/My\\ Games/Skyrim\\ VR;' + RN,
    "percent-my-games": 'cd "$(cat C:/Temp/d.txt)/My%20Games/Skyrim VR";' + RN,
    "save-name-only": 'cd "$(dirname "$(find C:/ -maxdepth 6 -name \'Save1.ess\' | head -1)")";' + RN,
    "save-dirs-variable": 'cd "$(echo "$SKYRIM_SAVE_DIRS" | cut -d";" -f1)";' + RN,
}


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


DELETES_FROM_SAVES = {
    "os-remove-listdir": "import os\n[os.remove(f) for f in os.listdir()]\n",
    "path-unlink-iterdir": "from pathlib import Path\n[p.unlink() for p in Path.cwd().iterdir()]\n",
    "os-unlink-star": "from os import *\nfor f in listdir():\n    unlink(f)\n",
    "send2trash-listdir": "import os\nfrom send2trash import send2trash\nsend2trash(os.listdir())\n",
}


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

