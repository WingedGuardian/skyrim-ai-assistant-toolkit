"""plugin-check: every finding kind goes red on a fixture built for it, and a clean fixture stays clean."""
import importlib.util
import struct
import sys

import pytest

from conftest import REPO

sys.path.insert(0, str(REPO / "tools"))
spec = importlib.util.spec_from_file_location("plugin_check", REPO / "tools" / "plugin-check.py")
pc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pc)


def header(masters=(), flags=0):
    body = b"".join(b"MAST" + struct.pack("<H", len(m) + 1) + m.encode() + b"\0"
                    + b"DATA" + struct.pack("<H", 8) + b"\0" * 8 for m in masters)
    return b"TES4" + struct.pack("<II", len(body), flags) + b"\0" * 12 + body


@pytest.fixture
def game(tmp_path):
    data = tmp_path / "game" / "Data"
    data.mkdir(parents=True)
    for name in ("Skyrim.esm", "Update.esm"):
        (data / name).write_bytes(header(flags=1))
    return tmp_path / "game", data


def run(game, lines, folders=()):
    root, data = game
    txt = root / "plugins.txt"
    txt.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return [(f["kind"], f["plugin"], f.get("master")) for f in pc.check(txt, data, root, list(folders))["findings"]]


def test_a_clean_order_has_no_findings(game):
    (game[1] / "A.esp").write_bytes(header(["Skyrim.esm"]))
    (game[1] / "B.esp").write_bytes(header(["Skyrim.esm", "A.esp"]))
    assert run(game, ["*A.esp", "*B.esp"]) == []


def test_each_finding_kind(game):
    data = game[1]
    (data / "A.esp").write_bytes(header(["Skyrim.esm"]))
    (data / "Off.esp").write_bytes(header())
    (data / "NeedsOff.esp").write_bytes(header(["Off.esp"]))
    (data / "NeedsGone.esp").write_bytes(header(["Gone.esm"]))
    (data / "Early.esp").write_bytes(header(["A.esp"]))
    (data / "Broken.esp").write_bytes(b"NOTAPLUGIN" * 4)
    found = run(game, ["*Early.esp", "*A.esp", "Off.esp", "*NeedsOff.esp", "*NeedsGone.esp", "*Broken.esp", "*Absent.esp"])
    assert ("master-order", "Early.esp", "A.esp") in found
    assert ("master-inactive", "NeedsOff.esp", "Off.esp") in found
    assert ("master-missing", "NeedsGone.esp", "Gone.esm") in found
    assert ("unreadable", "Broken.esp", None) in found
    assert ("missing", "Absent.esp", None) in found
    assert len(found) == 5, found


def test_unowned_dlc_masters_are_not_missing_but_a_listed_one_is(game):
    assert run(game, []) == []                                   # Dawnguard etc. not owned, not listed: fine
    assert ("missing", "Dawnguard.esm", None) in run(game, ["*Dawnguard.esm"])


def test_a_plugin_in_an_enabled_mo2_folder_is_on_disk(game, tmp_path):
    mod = tmp_path / "mods" / "Thing"
    mod.mkdir(parents=True)
    (mod / "T.esp").write_bytes(header(["Skyrim.esm"]))
    assert run(game, ["*T.esp"], [mod]) == []
    assert run(game, ["*T.esp"]) == [("missing", "T.esp", None)]


def test_the_cli_exit_codes(game, capsys, monkeypatch):
    root, data = game
    (data / "A.esp").write_bytes(header(["Missing.esm"]))
    (root / "plugins.txt").write_text("*A.esp\n", encoding="utf-8")
    monkeypatch.setattr(pc, "mo2_folders", lambda environ: ([], None))
    monkeypatch.setenv("SKYRIM_GAME_ROOT", str(root))
    assert pc.main(["--plugins-txt", str(root / "plugins.txt"), "--data", str(data)]) == 1
    assert "master-missing" in capsys.readouterr().out
    (data / "A.esp").write_bytes(header(["Skyrim.esm"]))
    assert pc.main(["--plugins-txt", str(root / "plugins.txt"), "--data", str(data)]) == 0
    assert pc.main(["--plugins-txt", str(root / "nope.txt"), "--data", str(data)]) == 2
    assert "not a clean result" in capsys.readouterr().err


def test_master_order_is_judged_in_the_engines_order(game):
    """4.1 review: master-flagged plugins load before the rest whatever plugins.txt says."""
    data = game[1]
    (data / "Flagged.esp").write_bytes(header(["Skyrim.esm"], flags=1))     # ESM-flagged .esp: loads with masters
    (data / "Patch.esp").write_bytes(header(["Flagged.esp"]))
    assert run(game, ["*Patch.esp", "*Flagged.esp"]) == []                  # listed later, still loads first
    (data / "NeedsEsp.esm").write_bytes(header(["Plain.esp"], flags=1))
    (data / "Plain.esp").write_bytes(header())
    assert ("master-order", "NeedsEsp.esm", "Plain.esp") in run(game, ["*Plain.esp", "*NeedsEsp.esm"])
