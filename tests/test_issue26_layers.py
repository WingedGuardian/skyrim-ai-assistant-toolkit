"""#26 twin targets, one per guarded clause: the body its mutation flips, judged by the full scan or by the
named-call layer alone (conservative=False). Several checks back each other up, so a decide()-level test
cannot show which one bit. Decision input only; nothing is executed."""
import pytest

import test_issue10_shapes  # noqa: F401  (puts tools/ on sys.path, as the other #26 tests do)
from skyguard import writes

BODIES = {
    'write-bytes-not-a-write': ('named', "from pathlib import Path\nD = r'{D}'\nfor p in Path(f'{{D}}/My Games/Skyrim VR/Saves').glob('*.ess'):\n    p.write_bytes(b'')\n"),
    'copy2-not-a-write': ('named', "import shutil\nfrom pathlib import Path\nBK = Path('C:/Temp/bk')\nfor p in BK.rglob('*.ess'):\n    shutil.copy2(p, Path(r'{MG}') / p.relative_to(BK))\n"),
    'os-open-not-a-write': ('named', 'import os\nclass K:\n    O_RDONLY = 0x301\nfor n in os.listdir():\n    os.close(os.open(n, K.O_RDONLY))\n'),
    'star-import-unread': ('named', "from io import *\nFileIO(r'C:/Users/x/Documents/My Games/Skyrim VR/Saves/Save1.ess', 'w').close()\n"),
    'star-writes-unread': ('named', "from io import *\nFileIO(r'C:/Users/x/Documents/My Games/Skyrim VR/Saves/Save1.ess', 'w').close()\n"),
    'star-extract-unread': ('named', "from shutil import *\nunpack_archive('C:/Temp/b.zip', r'C:/Users/x/Documents/My Games/Skyrim VR/Saves')\n"),
    'star-deletes-unread': ('named', 'from os import *\nfor f in listdir():\n    unlink(f)\n'),
    'path-unlink-uncounted': ('full', "from pathlib import Path\nfor p in Path('.').glob('*'):\n    p.unlink(True)\n"),
    'function-value-uncounted': ('named', 'import os\nfrom os import remove\nlist(map(remove, os.listdir()))\n'),
    'path-method-value-uncounted': ('named', "from pathlib import Path\nfor f in [p.unlink for p in Path('.').iterdir()]:\n    f()\n"),
    'imported-name-value-uncounted': ('named', 'import os\nfrom os import remove\nlist(map(remove, os.listdir()))\n'),
    'nt-posix-unmapped': ('named', 'import nt, os\nfor n in os.listdir():\n    nt.remove(n)\n'),
    'import-nt-unmapped': ('named', 'import nt, os\nfor n in os.listdir():\n    nt.remove(n)\n'),
    'from-nt-unmapped': ('named', 'import os\nfrom nt import unlink\nfor n in os.listdir():\n    unlink(n)\n'),
    'module-open-mode-at-0': ('full', "__builtins__.open('SAVE1.ESS', 'w').write('')\n"),
    'tarfile-name-kw-unread': ('full', "import tarfile\ntarfile.open(name=r'C:/Users/x/Documents/My Games/Skyrim VR/Saves/Save1.ess', mode='w').close()\n"),
    'conservative-scan-unwired': ('full', "from pathlib import Path\nPath('Save2.ess').copy('Save1.ess')\n"),
    'judged-method-value-trusted': ('full', "from pathlib import Path\nfor p in Path('.').glob('*.ess'):\n    list(map(p.chmod, [0o444]))\n"),
    'unknown-function-value-trusted': ('full', "import os\nfrom io import open as fopen\nns = os.listdir()\nlist(map(fopen, ns, ['w'] * len(ns)))\n"),
    'package-import-unbound': ('full-read', "import glob, lz4.block\nfor n in glob.glob('C:/Temp/*.ess'):\n    print(len(lz4.block.decompress(open(n, 'rb').read(), uncompressed_size=16)))\n"),
    'rebound-call-trusted': ('full', 'import os\nfrom os import remove as dumps\ndef never():\n    from json import dumps\nfor n in os.listdir():\n    dumps(n)\n'),
    'parameter-rebinding-unread': ('full', 'import os, json\n(lambda json: [json.remove(n) for n in json.listdir()])(os)\n'),
}


@pytest.mark.parametrize("tid", list(BODIES))
def test_each_guarded_clause_marks_its_body_computed(tid):
    layer, body = BODIES[tid]
    result = writes.python_computed_writes(body, conservative=(layer != "named")).computed
    assert result is (layer != "full-read")
