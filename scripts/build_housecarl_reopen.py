#!/usr/bin/env python3
"""Rebuild and compare the managed House Carl reader shipped in the toolkit."""

import argparse
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "tools/housecarl_integration/validator"
SDK = "10.0.404"  # Reproducible artifact input only. Installed toolkit users need only the runtime.
ARTIFACT = "HouseCarlReopen.dll"
sys.path.insert(0, str(ROOT / "tools"))

from housecarl_integration import UPSTREAM_VERSION
from housecarl_integration.setup import download_archive, install_archive


def runtime_controls(work, server):
    """Exercise the shipped DLL in a private .NET installation with no SDK."""
    installed_host = Path(shutil.which("dotnet")).resolve()
    isolated = work / "runtime-only"
    isolated.mkdir()
    host = isolated / installed_host.name
    shutil.copy2(installed_host, host)
    for source in (installed_host.parent / "host/fxr", installed_host.parent / "shared/Microsoft.NETCore.App"):
        compatible = [d for d in source.glob("10.0.*") if d.is_dir() and d.name.split(".")[-1].isdigit()]
        version = max(compatible, key=lambda d: int(d.name.split(".")[-1]))
        shutil.copytree(version, isolated / version.relative_to(installed_host.parent))
    env = dict(os.environ, DOTNET_ROOT=str(isolated))
    opts = dict(env=env, timeout=30, capture_output=True, text=True, encoding="utf-8", errors="replace",
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if subprocess.run([str(host), "--list-sdks"], check=True, **opts).stdout.strip():
        raise SystemExit("The reader control must use a runtime-only .NET directory.")

    def subrecord(sig, content):
        return struct.pack("<4sH", sig, len(content)) + content

    def record(sig, content, form_id=0):
        return struct.pack("<4sIIIIHH", sig, len(content), 0, form_id, 0, 44, 0) + content

    def plugin(path, weather=None):
        header = subrecord(b"HEDR", struct.pack("<fII", 1.7, bool(weather), 0x801))
        group = b"" if weather is None else struct.pack("<4sI4sIII", b"GRUP", 24 + len(weather),
                                                        b"WTHR", 0, 0, 0) + weather
        path.write_bytes(record(b"TES4", header) + group)

    empty, valid, invalid = (work / name for name in ("Empty.esp", "Complete.esp", "Incomplete.esp"))
    plugin(empty)
    edid = subrecord(b"EDID", "Café".encode("cp1252") + b"\0")
    dalc = subrecord(b"DALC", bytes(32))
    plugin(valid, record(b"WTHR", edid + dalc * 4, 0x800))
    plugin(invalid, record(b"WTHR", edid + dalc, 0x800))
    command = [str(host), "exec", "--runtimeconfig", str(server.with_name("housecarl-mcp.runtimeconfig.json")),
               str(SOURCE / ARTIFACT), str(server.parent)]
    for path in (empty, valid, ROOT / "tests/fixtures/spriggit/FixLimitsRoundTrip.esp"):
        result = subprocess.run([*command, str(path)], **opts)
        if result.returncode != 0 or result.stdout.strip() != "Readable":
            raise SystemExit("The runtime-only reader rejected " + path.name + ": " + result.stderr)
    bad = subprocess.run([*command, str(invalid)], **opts)
    if bad.returncode != 1 or "SubrecordException" not in bad.stderr:
        raise SystemExit("The runtime-only reader failed to detect malformed Weather: " + bad.stderr)
    print("The runtime-only reader accepted the controls and refused incomplete Weather.")


def build(archive, write, verify_runtime):
    with tempfile.TemporaryDirectory(prefix="housecarl-reader-build-") as temp:
        work = Path(temp)
        if archive is None:
            archive = work / ("houseCARL-" + UPSTREAM_VERSION + ".zip")
            download_archive(archive)
        server, _hashes = install_archive(archive, work / "upstream")
        for filename in ("Program.cs", "Reopen.csproj"):
            shutil.copyfile(SOURCE / filename, work / filename)
        (work / "global.json").write_text(json.dumps({"sdk": {"version": SDK, "rollForward": "disable"}}),
                                          encoding="utf-8")
        (work / "NuGet.Config").write_text("<configuration><packageSources><clear /></packageSources></configuration>",
                                            encoding="utf-8")
        subprocess.run(["dotnet", "build", "Reopen.csproj", "--configuration", "Release",
                        "--disable-build-servers", "--verbosity", "quiet", "-p:HouseCarlServer=" + str(server.parent)],
                       cwd=work, timeout=120, check=True,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        output = (work / "bin/Release/net10.0" / ARTIFACT).read_bytes()
        shipped = SOURCE / ARTIFACT
        if write:
            shipped.write_bytes(output)
            print("Updated the shipped House Carl reader.")
        elif not shipped.is_file() or shipped.read_bytes() != output:
            raise SystemExit("House Carl reader differs from the reproducible build. Rebuild with --write and review.")
        else:
            print("The shipped House Carl reader matches the reproducible build.")
        if verify_runtime:
            runtime_controls(work, server)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, help="Use a local copy of the pinned, hash-verified House Carl zip.")
    parser.add_argument("--write", action="store_true", help="Replace the committed reader with the rebuilt bytes.")
    parser.add_argument("--verify-runtime", action="store_true", help="Run controls in a private runtime with no SDK.")
    args = parser.parse_args()
    build(args.archive, args.write, args.verify_runtime)


if __name__ == "__main__":
    main()
