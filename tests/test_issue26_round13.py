"""#26 second safe-list review (2026-11-10): argument unpacking, decoy imports, a spoofed O_RDONLY, change methods
handed on as values, typing's string evaluation, and read-only audits the list wrongly refused. Decision input only;
nothing here is executed."""
import pytest

from test_issue10_shapes import heredoc, install  # noqa: F401  (install is a fixture)
from test_issue26_round4 import decide_in

LOOP = "import os\nfor n in os.listdir():\n"
FROM_SAVES = {
    "remove-kwargs": LOOP + "    os.remove(**{'path': n})\n",
    "rename-kwargs": LOOP + "    os.rename(**{'src': n, 'dst': 'C:/Temp/' + n})\n",
    "move-kwargs": "import os, shutil\nfor n in os.listdir():\n    shutil.move(**{'src': n, 'dst': 'C:/Temp/'})\n",
    "truncate-kwargs": LOOP + "    os.truncate(**{'path': n, 'length': 0})\n",
    "open-kwargs-mode": "import os\nkw = {'mode': 'w'}\nfor n in os.listdir():\n    open(n, **kw).close()\n",
    "open-starred": LOOP + "    open(*[n, 'w']).close()\n",
    "path-open-kwargs": "from pathlib import Path\nfor p in Path('.').glob('*.ess'):\n    p.open(**{'mode': 'w'}).close()\n",
    "decoy-module-import": "import os as json\ndef never():\n    import json\nfor n in json.listdir():\n    json.remove(n)\n",
    "decoy-name-import": "import os\nfrom os import remove as dumps\ndef never():\n    from json import dumps\nfor n in os.listdir():\n    dumps(n)\n",
    "spoofed-rdonly": "import os\nclass K:\n    O_RDONLY = 0x301\nfor n in os.listdir():\n    os.close(os.open(n, K.O_RDONLY))\n",
    "chmod-value": "from pathlib import Path\nfor p in Path('.').glob('*.ess'):\n    list(map(p.chmod, [0o444]))\n",
    "typing-eval": "import typing\ndef f(x: \"__import__('os').remove('Save1.ess')\"):\n    pass\ntyping.get_type_hints(f)\n",
}


@pytest.mark.parametrize("body", list(FROM_SAVES.values()), ids=list(FROM_SAVES))
def test_the_second_list_review_probes_are_refused(install, body):
    assert decide_in(install, heredoc(body), install["saves"]) == "deny"


READS = {
    "hash-variable": "import hashlib, os\nfor n in os.listdir():\n    hash = hashlib.sha256(open(n, 'rb').read())\n    print(hash.hexdigest())\n",
    "type-loop-var": "import os\nfor type, n in [('ess', x) for x in os.listdir()]:\n    print(type.upper(), n)\n",
    "id-variable": "import os\nfor n in os.listdir():\n    id = n.split('_')[0]\n    print(id.upper())\n",
    "path-loop-var": "import glob\nfrom os import path\nfor path in glob.glob('*.ess'):\n    print(path.lower())\n",
    "argparse-cli": "import argparse, os\np = argparse.ArgumentParser()\np.add_argument('--n', type=int, default=3)\na = p.parse_args([])\nprint(sorted(os.listdir())[:a.n])\n",
    "system-exit": "import os\nif not os.listdir():\n    raise SystemExit('no saves')\n",
    "csv-stdout": "import csv, os, sys\nw = csv.writer(sys.stdout)\nfor n in os.listdir():\n    w.writerow([n, os.stat(n).st_size])\n",
    "stdout-buffer": "import os, sys\nsys.stdout.buffer.write(open(os.listdir()[0], 'rb').read(4))\n",
    "print-starred": "import os\nprint(*sorted(os.listdir()), sep='\\n')\n",
}


@pytest.mark.parametrize("body", list(READS.values()), ids=list(READS))
def test_read_only_audits_the_list_refused_are_allowed(install, body):
    assert decide_in(install, heredoc(body), install["saves"]) == "allow"
