"""#26 conservative scan (user decision 2026-11-10): every call must be one the guard judges or a known read; an
unknown call, or a function handed on as a value, counts as a possible change. Decision input only; nothing here is
executed."""
import pytest

from test_issue10_shapes import heredoc, install  # noqa: F401  (install is a fixture)
from test_issue26_round4 import decide_in

FROM_SAVES = {
    # the round-10 review's remaining spellings
    "rename-value": "from pathlib import Path\nfor r in [p.rename for p in Path('.').iterdir()]:\n    r('x')\n",
    "map-path-rename": "from pathlib import Path\nlist(map(Path.rename, Path('.').iterdir(), ['a', 'b']))\n",
    "open-value": "from pathlib import Path\nfor o in [p.open for p in Path('.').iterdir()]:\n    o('w').close()\n",
    "shutil-os-remove": "import shutil\nfor n in shutil.os.listdir():\n    shutil.os.remove(n)\n",
    "os-path-os-remove": "import os\nfor n in os.listdir():\n    os.path.os.remove(n)\n",
    "alias-os-remove": "import os.path as op\nfor n in op.os.listdir():\n    op.os.remove(n)\n",
    # the class the scan closes by construction: calls nothing names
    "subprocess": "import os, subprocess\nfor n in os.listdir():\n    subprocess.run(['cmd', '/c', 'del', n])\n",
    "map-subprocess": "import os, subprocess\nlist(map(subprocess.call, os.listdir()))\n",
    "imported-call-value": "import os\nfrom subprocess import call\nlist(map(call, os.listdir()))\n",
    "unknown-module": "import os, send3trash\nfor n in os.listdir():\n    send3trash.toss(n)\n",
    "subscript-call": "import os\nops = [os.remove]\nfor n in os.listdir():\n    ops[0](n)\n",
    "variable-call": "import os\nfor n in os.listdir():\n    f = os.unlink\n    f(n)\n",
}


@pytest.mark.parametrize("body", list(FROM_SAVES.values()), ids=list(FROM_SAVES))
def test_an_unknown_change_run_from_saves_is_refused(install, body):
    assert decide_in(install, heredoc(body), install["saves"]) == "deny"


READS_FROM_SAVES = {
    "hash-audit": "import hashlib, os\nfor n in sorted(os.listdir()):\n    print(n, hashlib.sha256(open(n, 'rb').read()).hexdigest())\n",
    "json-stat": "import json, os\nprint(json.dumps({n: os.stat(n).st_size for n in os.listdir()}))\n",
    "pathlib-read": "from pathlib import Path\nfor p in Path('.').glob('*.ess'):\n    print(p.name, len(p.read_bytes()), p.stat().st_mtime)\n",
    "zip-list": "import zipfile, os\nfor n in os.listdir():\n    if zipfile.is_zipfile(n):\n        print(zipfile.ZipFile(n).namelist())\n",
    "struct-header": "import struct, os\nfor n in os.listdir():\n    print(struct.unpack('<I', open(n, 'rb').read(4)))\n",
    "local-helper": "import os\ndef size(n):\n    return os.stat(n).st_size\nprint([size(n) for n in os.listdir()])\n",
    "os-data": "import os\nprint(os.sep.join(os.listdir()), os.environ.get('X'))\n",
}


@pytest.mark.parametrize("body", list(READS_FROM_SAVES.values()), ids=list(READS_FROM_SAVES))
def test_a_read_only_audit_run_from_saves_stays_allowed(install, body):
    assert decide_in(install, heredoc(body), install["saves"]) == "allow"


def test_unknown_calls_in_code_that_names_no_save_location_stay_allowed(install):
    """The scan only decides `computed`; it refuses only where the code names saves or runs in the settings area."""
    body = "import subprocess\nitems = ['a', 'b']\nitems.remove('a')\nsubprocess.run(['git', 'status'])\n"
    assert decide_in(install, heredoc(body), str(install["work"])) == "allow"


def test_a_read_only_lz4_save_parse_stays_allowed(install):
    body = ("import glob, struct, lz4.block\nfor n in glob.glob(r'" + install["saves"] + "/*.ess'):\n"
            "    b = open(n, 'rb').read()\n    print(n, len(lz4.block.decompress(b[64:], uncompressed_size=16)))\n")
    assert decide_in(install, heredoc(body), str(install["work"])) == "allow"
