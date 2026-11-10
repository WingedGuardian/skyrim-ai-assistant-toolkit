"""#26 safe-list review (2026-11-10): the list trusted rebound names, private module aliases, classes below read
modules, any-receiver .write/.open/.copy and argparse.FileType. Decision input only; nothing here is executed."""
import pytest

from test_issue10_shapes import heredoc, install  # noqa: F401  (install is a fixture)
from test_issue26_round4 import decide_in

FROM_SAVES = {
    "rebound-module": "import os, json\njson = os\nfor n in os.listdir():\n    json.remove(n)\n",
    # `json` is imported, so `json.remove` alone reads as one name below a read module: only the rebinding says no
    "lambda-param": "import os, json\n(lambda json: [json.remove(n) for n in json.listdir()])(os)\n",
    "def-param": "import os, json\ndef f(json):\n    for n in json.listdir():\n        json.remove(n)\nf(os)\n",
    "loop-var": "import os, json\nfor json in [os]:\n    for n in json.listdir():\n        json.truncate(n, 0)\n",
    "shadowed-builtin": "import os\nprint = os.remove\nfor n in os.listdir():\n    print(n)\n",
    "argparse-private-os": "import argparse\nfor n in argparse._os.listdir():\n    argparse._os.remove(n)\n",
    "random-private-os": "import random\nrandom._os.remove('Save1.ess')\n",
    "json-codecs-open": "import json\njson.codecs.open('SAVE1.ESS', 'w').close()\n",
    "elementtree-write": "import xml.etree.ElementTree as ET\nET.ElementTree(ET.Element('a')).write('Save1.ess')\n",
    "unbound-path-rename": "from pathlib import Path\nfor p in Path('.').glob('*.ess'):\n    Path.rename(p, 'C:/Temp/' + p.name)\n",
    "unbound-path-replace": "from pathlib import Path\nfor p in Path('.').glob('*.ess'):\n    Path.replace(Path('C:/Temp/junk'), p)\n",
    "pathlib-chmod": "import pathlib\npathlib.Path.chmod(pathlib.Path('Save1.ess'), 0o444)\n",
    "argparse-filetype": "import argparse\np = argparse.ArgumentParser()\np.add_argument('f', type=argparse.FileType('w'))\np.parse_args(['Save1.ess'])\n",
    "builtins-open": "__builtins__.open('SAVE1.ESS', 'w').write('')\n",
    "sys-modules-open": "import sys\nsys.modules['builtins'].open('SAVE1.ESS', 'w').close()\n",
    "path-copy-314": "from pathlib import Path\nPath('Save2.ess').copy('Save1.ess')\n",
    "module-as-value": "import os\ndef go(m):\n    for n in m.listdir():\n        m.remove(n)\ngo(os)\n",
    # an object whose .write(name) opens the file itself (ElementTree, a library writer), handed in as a parameter
    "writer-parameter": "import os\ndef dump(t, n):\n    t.write(n)\nfor n in os.listdir():\n    dump(print, n)\n",
}


@pytest.mark.parametrize("body", list(FROM_SAVES.values()), ids=list(FROM_SAVES))
def test_the_safe_list_review_probes_are_refused(install, body):
    assert decide_in(install, heredoc(body), install["saves"]) == "deny"


READS = {
    "disk-usage": "import shutil\nprint(shutil.disk_usage('.'))\n",
    "memoryview": "import os\nfor n in os.listdir():\n    print(memoryview(open(n, 'rb').read())[:4].tobytes())\n",
    "with-open-read": "import os\nfor n in os.listdir():\n    with open(n, 'rb') as f:\n        print(len(f.read()))\n",
    "datetime-now": "import datetime, os\nprint(datetime.datetime.now(), [os.stat(n).st_mtime for n in os.listdir()])\n",
    "sys-stdout": "import os, sys\nsys.stdout.write(str(os.listdir()))\n",
}


@pytest.mark.parametrize("body", list(READS.values()), ids=list(READS))
def test_read_only_audits_from_saves_stay_allowed(install, body):
    assert decide_in(install, heredoc(body), install["saves"]) == "allow"


def test_a_report_written_through_a_judged_open_beside_a_saves_read_stays_allowed(install):
    """#10: read the saves, write a literal report. The report's open() is judged; its write() follows it."""
    body = ("import glob, hashlib\nrows = [hashlib.sha256(open(n, 'rb').read()).hexdigest() for n in "
            "glob.glob(r'" + install["saves"] + "/*.ess')]\nwith open('C:/Temp/report.txt', 'w') as f:\n"
            "    f.write('\\n'.join(rows))\n")
    assert decide_in(install, heredoc(body), str(install["work"])) == "allow"


def test_a_report_written_through_an_open_call_beside_a_saves_read_stays_allowed(install):
    body = ("import glob\nnames = [n for n in glob.glob(r'" + install["saves"] + "/*.ess')]\n"
            "open('C:/Temp/report.txt', 'w').write('\\n'.join(names))\n")
    assert decide_in(install, heredoc(body), str(install["work"])) == "allow"
