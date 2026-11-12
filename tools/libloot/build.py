#!/usr/bin/env python3
"""Build libloot's Python wheel here from the shipped GPL source pack, or record a CI build into the toolkit.

  build [--python PY]          compile tools/libloot/source/<pack>.tar.gz (every crate vendored: no network) into
                               <toolkit>/.skyguard/loot/built/, which tools/libloot/libloot_loader.py prefers over the
                               shipped wheels. Needs Rust (rustup.rs) and maturin (build-requirements.lock). For
                               Linux, macOS, or a Python the shipped wheels do not cover.
  record ARTIFACTS --run ID    maintainers: ARTIFACTS is the downloaded output of .github/workflows/libloot-wheels.yml
                               run ID. Each file is checked against the SHA256SUMS that run wrote beside it, then the
                               wheels go to tools/libloot/wheels/ and the source pack to tools/libloot/source/, each
                               with a SHA256SUMS, and PROVENANCE.json gets the run id. Nothing is replaced unchecked.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import libloot_loader                     # noqa: E402

SOURCE = HERE / "source"
PROVENANCE = HERE / "PROVENANCE.json"


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _write_sums(folder: Path, files) -> None:
    lines = ["%s *%s" % (_sha256(f), f.name) for f in sorted(files)]
    (folder / "SHA256SUMS").write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")


def source_pack(folder: Path = SOURCE) -> Path:
    packs = sorted(folder.glob("libloot-*-source.tar.gz"))
    if len(packs) != 1:
        raise SystemExit("expected one source pack in %s, found %d" % (folder, len(packs)))
    want = libloot_loader.sums(folder).get(packs[0].name)
    if want is None or _sha256(packs[0]) != want:
        raise SystemExit("%s does not match %s; refusing to build from it" % (packs[0].name, folder / "SHA256SUMS"))
    return packs[0]


def _safe_extract(t: tarfile.TarFile, dest: Path) -> None:
    """Extract only regular files and folders that stay inside dest (tarfile's own filter= needs 3.10.12+)."""
    root = dest.resolve()
    for m in t.getmembers():
        target = (dest / m.name).resolve()
        if not (m.isfile() or m.isdir()) or (target != root and root not in target.parents):
            raise SystemExit("the source pack holds an unsafe member: " + m.name)
    t.extractall(dest)


def build(python: str, out: Path = libloot_loader.BUILT) -> Path:
    pack = source_pack()
    for tool, hint in (("cargo", "install Rust from https://rustup.rs"), ):
        if shutil.which(tool) is None:
            raise SystemExit("%s not found: %s" % (tool, hint))
    if subprocess.run([python, "-m", "maturin", "--version"], capture_output=True).returncode != 0:
        raise SystemExit("maturin not found for %s: python -m pip install --only-binary=:all: --require-hashes -r "
                         "tools/libloot/build-requirements.lock" % python)
    with tempfile.TemporaryDirectory(prefix="libloot-build-") as tmp:
        with tarfile.open(pack) as t:
            _safe_extract(t, Path(tmp))
        [root] = [p for p in Path(tmp).iterdir() if p.is_dir()]
        out.mkdir(parents=True, exist_ok=True)
        before = set(out.glob("*.whl"))
        rc = subprocess.run([python, "-m", "maturin", "build", "--release", "--locked", "--offline",
                             "-m", "python/Cargo.toml", "-i", python, "--out", str(out)], cwd=root).returncode
        if rc != 0:
            raise SystemExit("maturin build failed (exit %d)" % rc)
    new = sorted(set(out.glob("*.whl")) - before) or sorted(out.glob("*.whl"))
    _write_sums(out, out.glob("*.whl"))
    return new[-1]


def record(artifacts: Path, run: str, wheels: Path = libloot_loader.WHEELS, source: Path = SOURCE,
           provenance: Path = PROVENANCE) -> dict:
    found_wheels, found_source = [], []
    for sums_file in sorted(artifacts.rglob("SHA256SUMS")):
        listed = libloot_loader.sums(sums_file.parent)
        for name, want in listed.items():
            f = sums_file.parent / name
            if not f.is_file() or _sha256(f) != want:
                raise SystemExit("%s does not match the SHA256SUMS the CI run wrote; nothing recorded" % f)
            (found_wheels if name.endswith(".whl") else found_source).append(f)
    if not found_wheels or len(found_source) != 1:
        raise SystemExit("expected wheels and one source pack under %s, found %d and %d"
                         % (artifacts, len(found_wheels), len(found_source)))
    meta = json.loads(provenance.read_text(encoding="utf-8"))
    version = meta["upstream"]["version"]
    if any(("libloot-%s-" % version) not in f.name for f in found_wheels + found_source):
        raise SystemExit("an artifact is not libloot %s (PROVENANCE.json)" % version)
    for folder, files, pattern in ((wheels, found_wheels, "*.whl"), (source, found_source, "*.tar.gz")):
        folder.mkdir(parents=True, exist_ok=True)
        for old in folder.glob(pattern):
            if old.name not in {f.name for f in files}:
                old.unlink()                      # a superseded tracked file: git history keeps it
        for f in files:
            shutil.copyfile(f, folder / f.name)
        _write_sums(folder, folder.glob(pattern))
    meta["build"]["run"] = run
    provenance.write_text(json.dumps(meta, indent=1) + "\n", encoding="utf-8", newline="\n")
    return {"wheels": sorted(f.name for f in found_wheels), "source": found_source[0].name}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build")
    b.add_argument("--python", default=sys.executable)
    r = sub.add_parser("record")
    r.add_argument("artifacts", type=Path)
    r.add_argument("--run", required=True)
    a = ap.parse_args(argv)
    if a.cmd == "build":
        print("built " + str(build(a.python)))
    else:
        print(json.dumps(record(a.artifacts, a.run), indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
