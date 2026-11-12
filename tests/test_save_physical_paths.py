"""Synthetic Windows saves: comparison aliases must never select backup bytes."""
import os
from pathlib import Path
import sys

import pytest

from conftest import REPO

sys.path.insert(0, str(REPO / "tools"))
from modding_harness import contracts, preservation
from skyguard import paths, portable, targets

pytestmark = pytest.mark.skipif(os.name != "nt", reason="actual Windows namespace I/O")


def quoted(value):
    return "'" + str(value).replace("'", "''") + "'"


@pytest.fixture
def owned(tmp_path):
    root = tmp_path / "toolkit"
    root.mkdir()
    profile = tmp_path / "profile"
    normal = profile / "Saves"
    normal.mkdir(parents=True)
    (root / "skyrim-paths.env").write_text("SKYRIM_CONFIG_DIR=" + quoted(profile) + "\n", encoding="utf-8")
    (root / ".modding-agent-harness").mkdir()
    (root / ".modding-agent-harness/config.json").write_text('{"v":1,"save_reserve_bytes":0}', encoding="utf-8")
    directories, members = [], []

    def raw_folder(suffix):
        folder = Path("\\\\?\\" + str(normal) + suffix)
        folder.mkdir()
        directories.append(folder)
        return folder

    def pair(folder, stem, content):
        result = tuple(folder / (stem + ext) for ext in (".ess", ".skse"))
        for member in result:
            member.write_bytes(content + member.suffix.encode())
            if str(member).startswith("\\\\?\\"):
                members.append(member)
        return result

    try:
        yield root, profile, normal, raw_folder, pair
    finally:
        # Only the exact owned raw files and empty directories created above.
        for member in reversed(members):
            member.unlink()
        for folder in reversed(directories):
            folder.rmdir()


def fire(root, target, route, *, cwd=None):
    cwd = str(cwd or root)
    q = quoted(target)
    commands = {
        "write": "Set-Content -LiteralPath " + q + " -Value fixture",
        "delete": "Remove-Item -LiteralPath " + q,
        "move": "Move-Item -LiteralPath " + q + " -Destination " + quoted(root / "away.bin"),
        "copy": "Copy-Item -LiteralPath " + quoted(root / "input.bin") + " -Destination " + q,
        "dotnet": "[IO.File]::WriteAllText(" + q + ", 'fixture')",
        "python": 'python -c "open(r' + quoted(target) + ", 'wb').write(b'fixture')\"",
        "python-escaped": "python -c " + quoted("open(" + repr(str(target)) + ", 'wb').write(b'fixture')"),
        "python-path": "python -c " + quoted("from pathlib import Path; Path(" + repr(str(target)) + ").write_bytes(b'fixture')"),
        "recycle": "[Microsoft.VisualBasic.FileIO.FileSystem]::DeleteFile(" + q + ", 'OnlyErrorDialogs', 'SendToRecycleBin')",
    }
    command = commands.get(route, "")
    if route == "cd":
        command = "Set-Location " + quoted(Path(target).parent) + "; Set-Content -LiteralPath " + quoted(Path(target).name) + " -Value fixture"
    action = contracts.Action("generic", "shell" if command else "edit", "shell" if command else "files",
        cwd=cwd, shell="powershell" if command else "", command=command,
        paths=() if command else (str(target),), operations=() if command else ("write",),
        session_id="owned-physical", tool_call_id=route)
    return portable.check(action, root)[0]


def assert_pair(root, outcome, pair):
    assert outcome.result.decision == "allow", outcome.result.as_dict()
    assert outcome.result.preservation == "verified" and len(outcome.transactions) == 1
    store = preservation.SaveStore(root, reserve=0)
    record = store.load(outcome.transactions[0])
    members = [m for g in record["groups"] for m in g["members"] if not m["missing"]]
    assert len(members) == 2
    for member in members:
        expected = next(p for p in pair if p.suffix == Path(member["path"]).suffix)
        assert os.path.samefile(member["path"], expected)
        assert (store.root / "blobs" / member["sha256"]).read_bytes() == expected.read_bytes()


def test_missing_raw_target_does_not_preserve_existing_folded_peer(owned):
    root, _, normal, raw_folder, make_pair = owned
    peer = make_pair(normal, "Distinct", b"PEER")
    missing = raw_folder(".") / "Distinct.ess"
    outcome = fire(root, missing, "files")
    assert outcome.result.decision == "allow" and not outcome.transactions
    assert not missing.exists() and peer[0].read_bytes() == b"PEER.ess"


def test_unknown_python_syntax_refuses_before_any_save_permission(owned):
    root, _, normal, _, make_pair = owned
    requested = make_pair(normal, 'Version', b'UNCHANGED')
    code = "print 'another syntax version'; open(" + repr(str(requested[0])) + ", 'wb').write(b'x')"
    action = contracts.Action('generic', 'shell', 'shell', shell='powershell', cwd=str(root),
        command='python2 -c ' + quoted(code))
    result = portable.check(action, root)[0]
    assert result.result.decision == 'deny' and result.result.evaluation == 'failed' and not result.transactions
    assert requested[0].read_bytes() == b'UNCHANGED.ess'

