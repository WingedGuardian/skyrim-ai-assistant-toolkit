"""Load the libloot Python module (`loot`): a wheel the toolkit ships, or one built here with tools/libloot/build.py.

Shipped wheels (tools/libloot/wheels/) are built on GitHub CI from the upstream commit in PROVENANCE.json and listed in
that folder's SHA256SUMS; a local build goes to <toolkit>/.skyguard/loot/built/ with its own SHA256SUMS. A wheel whose
bytes differ from its SHA256SUMS line, or that has no line, is refused, never extracted.

A wheel is a zip holding a compiled extension, which Python cannot import from inside a zip, so it is extracted once
into <toolkit>/.skyguard/loot/<sha256[:16]>/ (into a temporary folder first, renamed into place when complete) and
imported from there. Every load re-hashes each extracted file against the wheel's own RECORD, so a changed extracted
file is refused too. libloot is GPL-3.0-or-later: see tools/libloot/NOTICE.md and tools/libloot/source/.
"""
from __future__ import annotations

import base64
import csv
import hashlib
import importlib
import io
import os
import shutil
import sys
import uuid
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
WHEELS = HERE / "wheels"
TOOLKIT = HERE.parents[1]
CACHE = TOOLKIT / ".skyguard" / "loot"
BUILT = CACHE / "built"
SHIPPED_PLATFORM = "win_amd64"


class Unavailable(RuntimeError):
    """libloot cannot be loaded here; the message says why and what to do."""


def _tag() -> str:
    return "cp%d%d" % sys.version_info[:2]


def sums(folder: Path) -> dict:
    out = {}
    path = folder / "SHA256SUMS"
    if not path.is_file():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        parts = line.split()
        if len(parts) == 2:
            out[parts[1].lstrip("*")] = parts[0].lower()
    return out


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _candidates(folder: Path, platform_ok) -> list:
    tag = _tag()
    return sorted(p for p in folder.glob("libloot-*-%s-%s-*.whl" % (tag, tag)) if platform_ok(p.stem.rsplit("-", 1)[-1]))


def wheel(shipped: Path = WHEELS, built: Path = BUILT) -> Path:
    """The wheel for this interpreter, verified against its folder's SHA256SUMS. A local build wins over a shipped wheel
    (it was built here on purpose); a shipped wheel is used only on Windows x64, the one platform it is built for."""
    found = [(p, built) for p in _candidates(built, lambda plat: True)]
    if sys.platform == "win32":
        found += [(p, shipped) for p in _candidates(shipped, lambda plat: plat == SHIPPED_PLATFORM)]
    if not found:
        where = "the shipped wheels cover Python 3.10-3.13 on Windows x64" if sys.platform == "win32" else \
            "the shipped wheels are Windows-only"
        raise Unavailable("no libloot wheel for Python %d.%d here (%s); build one with `python tools/libloot/build.py "
                          "build` (needs Rust; about a minute)" % (sys.version_info[0], sys.version_info[1], where))
    path, folder = found[0]
    want = sums(folder).get(path.name)
    if want is None:
        raise Unavailable("%s is not listed in %s; refusing an unlisted binary" % (path.name, folder / "SHA256SUMS"))
    got = _sha256(path)
    if got != want:
        raise Unavailable("%s does not match SHA256SUMS (%s, expected %s); refusing it" % (path.name, got[:16], want[:16]))
    return path


def _record(z: zipfile.ZipFile) -> dict:
    """{member: urlsafe-b64 sha256} from the wheel's RECORD (the wheel spec's own per-file hashes)."""
    name = next((n for n in z.namelist() if n.endswith(".dist-info/RECORD")), None)
    if name is None:
        raise Unavailable("the wheel has no RECORD")
    out = {}
    for row in csv.reader(io.StringIO(z.read(name).decode("utf-8"))):
        if len(row) >= 2 and row[1].startswith("sha256="):
            out[row[0]] = row[1][len("sha256="):]
    return out


def _b64(path: Path) -> str:
    return base64.urlsafe_b64encode(bytes.fromhex(_sha256(path))).rstrip(b"=").decode("ascii")


def _extract(path: Path, target: Path) -> None:
    part = target.with_name(target.name + ".part-" + uuid.uuid4().hex[:8])
    part.mkdir(parents=True)
    try:
        with zipfile.ZipFile(path) as z:
            for member in z.namelist():
                dest = (part / member).resolve()
                if dest != part.resolve() and part.resolve() not in dest.parents:
                    raise Unavailable("wheel member escapes its folder: " + member)
            z.extractall(part)
        try:
            os.replace(part, target)
        except OSError:
            if not target.is_dir():          # another process finished first: its copy is verified below like ours
                raise
    finally:
        if part.exists():
            shutil.rmtree(part)              # our own unfinished extraction, made above


def verify_extracted(path: Path, target: Path) -> None:
    with zipfile.ZipFile(path) as z:
        record = _record(z)
    for member, want in record.items():
        f = target / member
        if not f.is_file() or _b64(f) != want:
            raise Unavailable("the extracted libloot in %s does not match its wheel (%s); delete that folder and run "
                              "again" % (target, member))


def load(shipped: Path = WHEELS, built: Path = BUILT, cache: Path = CACHE):
    """Import and return the `loot` module from the verified wheel."""
    path = wheel(shipped, built)
    target = cache / _sha256(path)[:16]
    if not target.is_dir():
        target.parent.mkdir(parents=True, exist_ok=True)
        _extract(path, target)
    verify_extracted(path, target)
    if str(target) not in sys.path:
        sys.path.insert(0, str(target))
    try:
        return importlib.import_module("loot")
    except ImportError as exc:
        raise Unavailable("the libloot module did not import: %s" % exc) from None
