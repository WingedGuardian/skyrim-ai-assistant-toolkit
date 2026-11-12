"""#26 round 4 review: computed writes run from inside the settings area, and literal targets of calls the literal
reader never reads. Decision input only; nothing here is executed."""
import pytest

from test_issue10_shapes import Audit, heredoc, install, shell_rules  # noqa: F401  (install is a fixture)


def decide_in(inst, command, cwd):
    return shell_rules.decide(command, inst["roots"], Audit(), cwd=cwd, shell="bash", env=inst["env"]).decision


FROM_SAVES = {
    "extractall-no-destination": "import zipfile\nzipfile.ZipFile('C:/Temp/bk.zip').extractall()\n",
    "unpack-no-destination": "import shutil\nshutil.unpack_archive('C:/Temp/saves-backup.zip')\n",
    "extract-one-member": "import zipfile\nzipfile.ZipFile('C:/Temp/bk.zip').extract('Save1.ess')\n",
    "listdir-rename": "import os\nfor n in os.listdir():\n    os.rename(n, n.lower())\n",
    "cwd-iterdir": "from pathlib import Path\nfor p in Path.cwd().iterdir():\n    p.write_bytes(b'')\n",
}


def test_the_same_computed_write_run_elsewhere_is_allowed(install):
    body = "import zipfile\nzipfile.ZipFile('C:/Temp/bk.zip').extractall()\n"
    assert decide_in(install, heredoc(body), str(install["work"])) == "allow"


UNREAD_LITERALS = {
    "fileio": "import io\nio.FileIO(r'{S}/Save1.ess', 'w').close()\n",
    "os-open": "import os\nos.close(os.open(r'{S}/Save1.ess', os.O_WRONLY | os.O_TRUNC))\n",
    "run-time-mode": "M = 'wb'\nopen(r'{S}/Save1.ess', M).close()\n",
    "path-open-run-time-mode": "from pathlib import Path\nM = 'wb'\nPath(r'{S}/Save1.ess').open(M).close()\n",
    "gzip-write": "import gzip\ngzip.open(r'{S}/Save1.ess', 'wb').close()\n",
    "touch": "from pathlib import Path\nPath(r'{S}/Save9.ess').touch()\n",
    "os-link": "import os\nos.link('C:/Temp/x.ess', r'{S}/Save9.ess')\n",
    "zipfile-write": "import zipfile\nzipfile.ZipFile(r'{S}/Save1.ess', 'w').close()\n",
}


READ_LITERALS = {
    "open-wb": "open(r'{S}/Save1.ess', 'wb').close()\n",
    "path-write-bytes": "from pathlib import Path\nPath(r'{S}/Save1.ess').write_bytes(b'')\n",
    "copyfile": "import shutil\nshutil.copyfile('C:/Temp/x.ess', r'{S}/Save1.ess')\n",
    "pathlib-dotted": "import pathlib\npathlib.Path(r'{S}/Save1.ess').write_bytes(b'')\n",
}


ROUND5 = {
    "renames-keywords-onto": "import os\nos.renames(old='C:/Temp/x.ess', new=r'{S}/Save1.ess')\n",
    "renames-keywords-away": "import os\nos.renames(old=r'{S}/Save1.ess', new='C:/Temp/x.ess')\n",
    "path-rename-target-kw": "from pathlib import Path\nPath('C:/Temp/x.ess').rename(target=r'{S}/Save1.ess')\n",
    "path-replace-target-kw": "from pathlib import Path\nPath('C:/Temp/x.ess').replace(target=r'{S}/Save1.ess')\n",
    "tarfile-name-kw": "import tarfile\ntarfile.open(name=r'{S}/Save1.ess', mode='w').close()\n",
    "tarfile-class": "import tarfile\ntarfile.TarFile(r'{S}/Save1.ess', 'w').close()\n",
    "gzip-class": "import gzip\ngzip.GzipFile(r'{S}/Save1.ess', 'wb').close()\n",
    "star-os-rename": "from os import *\nfor n in listdir(r'{S}'):\n    rename(r'{S}/' + n, r'{S}/' + n + '.x')\n",
    "star-shutil-move": "import glob\nfrom shutil import *\nfor f in glob.glob(r'{S}/*.ess'):\n    move(f, 'C:/Temp/')\n",
}


_EXTRACT_HERE = " && python -c \"import zipfile; zipfile.ZipFile('C:/Temp/b.zip').extractall()\""


def _cwd_is_unknown(cmd):
    """The test must reach the unknown-cwd clause, not pass through the settings-area cwd clause."""
    from skyguard import shellparse
    return [e.cd_before for e in shellparse.parse(cmd, "bash").execs if e.program == "python"] == ["?"]


def test_an_unknown_cwd_in_ordinary_scratch_work_is_allowed(install):
    """The priced false-positive class (227 history commands): a scratch folder the parser will not trust."""
    w = str(install["work"].as_posix())
    cmd = ('S="' + w + '"; cd "$S"; python -c "\nimport zipfile;z=zipfile.ZipFile(\'openvr.zip\')\n'
           'for n in z.namelist():\n  if n.endswith(\'.h\'): z.extract(n,\'.\')\n"; '
           'cmd.exe //c "$(cygpath -w "$S/build.bat")"; cd ..; ls')
    assert _cwd_is_unknown(cmd)
    assert decide_in(install, cmd, w) == "allow"


def _my_games(inst):
    return inst["saves"].rsplit("/", 2)[0]


def test_an_absolute_literal_after_a_chdir_is_still_placed(install):
    """After chdir only RELATIVE literals are computed: an absolute report path stays the literal reader's."""
    body = "import os\nos.chdir(r'" + install["saves"] + "')\nopen(r'C:/Temp/report.txt', 'w').close()\n"
    assert decide_in(install, heredoc(body), str(install["work"])) == "allow"

