"""Stock-build tier of the ReSaver Plus native backend: the user's own resaverpp-cli.exe, any upstream version.

Imported by tools/resaver_native.py only; it has no entry point of its own, so every run goes through that adapter
and its backups and checks. (Since 4.1 no guard rule refuses a ReSaver command; running the exe directly gets an advisory note,
S-RESAVER-NATIVE-DIRECT, and the save vault keeps every version.)

A stock build has no structured output, no preview and no save verification; those come only from the toolkit's
patches (tools/resaver-plus/build.py). So this tier:
  * reads the executable's own `--help` on every call and offers an operation only when that version lists every
    option it needs. Anything else exits 3: "this ReSaver version cannot do X". It never guesses;
  * passes the tool's text through instead of parsing it;
  * writes only a separate new output (-o), after permanent pair backups, then checks the input pair is unchanged,
    the output exists, the co-save came along, and the same build re-reads the output without an error. It cannot
    preview the change or verify its delta, and says so in every write result.
"""
from __future__ import annotations

import os
from pathlib import Path
import re

# The upstream versions whose --help this toolkit has read. Any other version still works when its --help lists the
# options; its results say "untested version".
TESTED_VERSIONS = {"51", "56", "68"}     # 68: its --help read 2026-11-12, every option below listed
CAPABILITY_EXIT = 3
VERSION = re.compile(r"ReSaver\+\+ version (\S+)")
OPTION = re.compile(r"(?<![\w-])(--[a-z][a-z0-9-]*|-o)(?![\w-])")

# Read operations: toolkit name -> (stock options it needs, how many positional arguments after the save).
# The stock build reads the save with no action as its analysis report.
STOCK_READS = {
    "info": ((), 0),
    "describe": (("--describe",), 0),
    "quests": (("--quests",), (0, 1)),
    "journal": (("--journal",), 0),
    "player": (("--player",), 0),
    "npcs": (("--npcs",), (0, 1)),
    "compare": (("--compare",), 1),
    "crash-log": (("--crash-log",), 1),
    "path-idles": (("--path-idles",), 0),
    "charged": (("--charged",), 0),
    "spawned": (("--spawned",), 0),
}
# Writes (each makes a new output beside the save): toolkit flag -> stock option.
STOCK_CLEAN = {"--unattached": "--remove-unattached", "--undefined": "--remove-undefined",
               "--terminate-threads": "--terminate-undefined"}
STOCK_WRITES = {"clean": None, "reset-havok": "--reset-havok", "cleanse-formlists": "--purify-formlists"}


class CannotDo(Exception):
    """The installed ReSaver version lacks what the operation needs (exit 3)."""


def probe(text: str) -> dict:
    """What a stock build's --help says: its version and every option it lists."""
    m = VERSION.search(text)
    return {"version": m.group(1) if m else None, "options": set(OPTION.findall(text))}


def is_resaver(caps: dict) -> bool:
    """Its --help names itself ("ReSaver++ version N"). Without that line nothing is run: a different program, or a
    ReSaver whose no-action default is unknown, would be handed the save."""
    return caps["version"] is not None


def writes_offered(caps: dict) -> list:
    """The write operations this version's --help lists every needed option for (-o included)."""
    out = []
    for op, option in STOCK_WRITES.items():
        needed = ["-o"] + ([option] if option else [])
        if op == "clean" and not any(o in caps["options"] for o in STOCK_CLEAN.values()):
            continue
        if all(o in caps["options"] for o in needed):
            out.append(op)
    return sorted(out)


def _needs(caps: dict, op: str, options) -> None:
    missing = [o for o in options if o not in caps["options"]]
    if missing:
        raise CannotDo("this ReSaver version ({}) cannot do {}: its --help lists no {}".format(
            caps["version"] or "unknown", op, ", ".join(missing)))


def plan_read(caps: dict, op: str, source: Path, positions: list) -> list:
    options, count = STOCK_READS[op]
    allowed = count if isinstance(count, tuple) else (count,)
    if len(positions) not in allowed:
        raise ValueError("{} takes {} argument(s) after the save".format(op, " or ".join(map(str, allowed))))
    _needs(caps, op, options)
    argv = [str(source)]
    if options:
        argv.append(options[0])
    argv += positions
    return argv


def plan_write(caps: dict, op: str, source: Path, output: Path, flags: set) -> list:
    if op == "clean":
        if not flags:
            raise ValueError("clean on a stock build needs at least one of " + ", ".join(sorted(STOCK_CLEAN)))
        stock = [STOCK_CLEAN[f] for f in sorted(flags)]
    else:
        if flags:
            raise ValueError(op + " takes no options")
        stock = [STOCK_WRITES[op]]
    _needs(caps, op, stock + ["-o"])
    return [str(source), *stock, "-o", str(output)]


def result(caps: dict, op: str, text: str, wrote: bool = False) -> dict:
    out = {"ok": True, "backend": "ReSaver Plus stock", "tier": "stock", "operation": op,
           "resaverVersion": caps["version"], "testedVersion": caps["version"] in TESTED_VERSIONS, "text": text}
    if not out["testedVersion"]:
        out["note"] = "untested version: this toolkit has not read this version's --help; the options it lists were used"
    if wrote:
        out["verification"] = ("stock build: the change was not previewed and its delta not verified (that needs the "
                               "toolkit's patched build). Checked: pair backups first, input unchanged, new output and "
                               "co-save present, re-read by the same build without error. Load-test it in game.")
    return out


def configured_exe(root: Path, test_mode: bool) -> Path | None:
    """RESAVER_EXE, else the one path in tools/resaver-plus/stock-exe; None when neither names a file."""
    value = os.environ.get("RESAVER_EXE", "").strip()
    if not value:
        config = root / "tools" / "resaver-plus" / "stock-exe"
        value = config.read_text(encoding="utf-8").strip() if config.is_file() else ""
    if not value:
        return None
    path = Path(value)
    if not path.is_absolute():
        raise ValueError("the stock ReSaver path must be absolute: " + value)
    if not test_mode and path.name.lower() != "resaverpp-cli.exe":
        raise ValueError("the stock ReSaver path must name resaverpp-cli.exe (the command-line tool, not the GUI)")
    if not path.is_file():
        raise ValueError("the stock ReSaver executable does not exist: " + value)
    return path.resolve()
