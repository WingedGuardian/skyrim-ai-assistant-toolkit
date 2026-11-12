"""#26 third safe-list review (2026-11-10): a command past the parser's size cap, builtin open handed on as a value,
a shadowed local def, a rebound O_RDONLY, and a too-deep attribute chain. Decision input only; nothing is executed."""
import pytest

from test_issue10_shapes import heredoc, install  # noqa: F401  (install is a fixture)
from test_issue26_round4 import decide_in

FROM_SAVES = {
    "map-open": "import os\nns = os.listdir()\nlist(map(open, ns, ['w'] * len(ns)))\n",
    "map-io-open": "import io, os\nns = os.listdir()\nlist(map(io.open, ns, ['w'] * len(ns)))\n",
    "imported-open-value": "import os\nfrom io import open as fopen\nns = os.listdir()\nlist(map(fopen, ns, ['w'] * len(ns)))\n",
    # the shadowing value is fetched through data the value scan cannot read as a function
    "def-shadowed-by-data": ("import os\ndef w(*a):\n    pass\nw = (lambda: 0).__globals__['__builtins__']['open']\n"
                             "for n in os.listdir():\n    w(n, 'w')\n"),
    "reduce-open":"import functools, os\nfor n in os.listdir():\n    functools.reduce(open, ['w'], n).close()\n",
    "shadowed-def": "import os\ndef w(*a):\n    pass\nw = open\nfor n in os.listdir():\n    w(n, 'w')\n",
    "shadowed-class": "import os\nclass K:\n    pass\nK = open\nfor n in os.listdir():\n    K(n, 'w')\n",
    "parameter-shadows-def": "import os\ndef o():\n    pass\ndef go(n, o=open):\n    o(n, 'w')\nfor n in os.listdir():\n    go(n)\n",
    "comprehension-shadows-def": "import os\ndef o():\n    pass\n[o(n, 'w') for o in [open] for n in os.listdir()]\n",
    "rdonly-name-rebound": "import os\nfrom os import O_RDONLY\nO_RDONLY = 0x301\nfor n in os.listdir():\n    os.close(os.open(n, O_RDONLY))\n",
    "rdonly-attribute-rebound": "import os\nos.O_RDONLY = 0x301\nfor n in os.listdir():\n    os.close(os.open(n, os.O_RDONLY))\n",
    "too-deep-chain": "import os\nos" + ".path" * 2000 + ".join('a')\n",
}


def test_a_too_deep_chain_outside_the_settings_area_is_allowed(install):
    body = "import os\nos" + ".path" * 2000 + ".join('a')\n"
    assert decide_in(install, heredoc(body), str(install["work"])) == "allow"


def _padded(body):
    return "DATA = '" + "x" * (300 * 1024) + "'\n" + body


def test_a_command_past_the_size_cap_elsewhere_is_judged_as_before(install):
    body = _padded("print(len(DATA))\n")
    assert decide_in(install, heredoc(body), str(install["work"])) == "allow"


def test_a_local_def_called_by_its_one_name_stays_trusted(install):
    body = "import os\ndef size(n):\n    return os.stat(n).st_size\nprint([size(n) for n in os.listdir()])\n"
    assert decide_in(install, heredoc(body), install["saves"]) == "allow"
