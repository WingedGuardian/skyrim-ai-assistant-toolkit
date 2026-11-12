"""v4.0.0 release review, 2026-11-02: what the guard counted as a save change or a delete. Each row was allowed at
bc246b8 (reproduced through engine.evaluate_shell), and each control pins the ordinary command that must stay allowed.
Commands are only judged here, never run."""
import sys
import tempfile
from pathlib import Path

import pytest

from conftest import REPO

sys.path.insert(0, str(REPO / "tools"))
from skyguard import audit, codes, engine, paths  # noqa: E402
from skyguard.request import Request  # noqa: E402

EXT, CO = ".ess", ".skse"


@pytest.fixture(scope="module")
def box(tmp_path_factory):
    base = tmp_path_factory.mktemp("gaps").resolve()
    inst, sv = base / "Game", base / "My Games" / "Skyrim VR" / "Saves"
    for d in (inst / "Data", inst / "tools", sv, base / "bk", base / "other"):
        d.mkdir(parents=True)
    for n in "abcd":
        (sv / (n + EXT)).write_bytes(b"x")
        (sv / (n + CO)).write_bytes(b"y")
        (base / "bk" / (n + EXT)).write_bytes(b"z")
    (inst / "Data" / "Skyrim.esm").write_bytes(b"m")
    B = base.as_posix()
    (inst / "skyrim-paths.env").write_text(
        "SKYRIM_GAME_ROOT='{}/Game'\nSKYRIM_CONFIG_DIR='{}/My Games/Skyrim VR'\n".format(B, B), encoding="utf-8")
    env = paths.read_install(inst.as_posix(), {})
    roots = paths.roots(inst.as_posix(), inst.as_posix(), env)

    def judge(cmd, shell="bash", cwd=None):
        text = cmd.replace("$S", '"{}/My Games/Skyrim VR/Saves"'.format(B)).replace("$B", B).replace("$E", EXT)
        req = Request(agent="claude", event="pre_tool", kind="shell", shell=shell, command=text,
                      cwd=cwd or inst.as_posix(), session_id="s", tool_name="Bash")
        return engine.evaluate_shell(req, roots, audit.Audit(inst.as_posix()))
    yield judge


def _pairs(v):
    return len(saves._existing(saves.count(v.saves.change))) if v.saves is not None else 0


# ---- piped input and member calls reach what the earlier stage names
@pytest.mark.parametrize("cmd", [
    "Get-ChildItem $S | Clear-Content",
    "Get-ChildItem $S | Set-Content -Value x",
    "Get-ChildItem $S | Add-Content -Value x",
    "Get-ChildItem $S | Rename-Item -NewName x",
    "Get-ChildItem $S | % Delete",
    "Get-ChildItem $S | ForEach-Object Delete",
    "Get-ChildItem $S | ForEach-Object -MemberName MoveTo x",
    "Get-ChildItem $B/Game/Data -Recurse | % Delete",
    "Get-ChildItem $B/Game/Data | Rename-Item -NewName x",
    "(Get-Item $S/a$E).MoveTo('x')",
    "(Get-Item $B/Game/Data).MoveTo('x')",
], ids=lambda x: x[:42])
def test_piped_and_member_changes_are_refused(box, cmd):
    assert box(cmd, "powershell").decision == "deny"


# ---- literal interpreter moves and copies
@pytest.mark.parametrize("code", [
    "import shutil; shutil.move('$B/Game/Data', '$B/x')",
    "import os; os.rename('$B/Game/Data', '$B/x')",
    "import pathlib; pathlib.Path('$B/Game/Data').rename('$B/x')",
], ids=lambda x: x[:42])
def test_literal_python_moves_of_data_are_refused(box, code):
    assert box('python -c "{}"'.format(code)).decision == "deny"


# ---- same-command functions (bash)
G = "$B/Game"


@pytest.mark.parametrize("cmd", [
    "f() { cd " + G + "; }; f; f() { cd /tmp; }; rm -rf Data",
    "f() ( rm -rf Data ); cd " + G + "; f",
    "g() { shift; cd \"$1\"; }; g x " + G + "; rm -rf Data",
    "g() { set -- " + G + "; cd \"$1\"; }; g; rm -rf Data",
    "g() { shift 2; cd \"$1\"; }; g x y " + G + "; rm -rf Data",
    "f() { cd " + G + "; }; \"f\"; rm -rf Data",
], ids=["redefined-after-call", "paren-body", "shift", "set-args", "shift-count", "quoted-call"])
def test_function_forms_cannot_hide_a_delete_in_the_install(box, cmd):
    assert box(cmd, cwd=str(Path(tempfile.gettempdir()).as_posix())).decision == "deny"


def test_a_helper_that_shifts_still_parses():
    """Priced 2026-11-02: refusing every shift made 39 real helper functions unparsable."""
    from skyguard import shellparse
    assert shellparse.parse('w() { local f="$1"; shift; printf x > "$f"; }; w a b').ok


def test_shift_moves_the_arguments_for_the_text_after_it_only():
    from skyguard.shellparse import _call_body
    assert _call_body('echo "$1"; shift; echo "$1"', ("a", "b"), (False, False)) == 'echo "a"; shift; echo "b"'
    assert _call_body('shift 2; echo "$1"', ("a", "b", "c"), (False,) * 3) == 'shift 2; echo "c"'
    from skyguard.shellparse import ParseError
    for body in ('[ -n x ] && shift; echo "$1"', 'set -- z; echo "$1"', 'shift $2; echo "$1"', '( shift ); echo "$1"',
                 'true | shift; echo "$1"'):
        with pytest.raises(ParseError):
            _call_body(body, ("a", "b"), (False,) * 2)


def test_a_quoted_brace_stays_one_word():
    from skyguard import shellparse
    assert shellparse.parse("rm '{a,b}.txt'").execs[0].argv == ("{a,b}.txt",)
    assert shellparse.parse("rm {a,b}.txt").execs[0].argv == ("a.txt", "b.txt")
    assert shellparse.parse("rm \"${x}\"/{a,b}").execs[0].argv[-1].endswith("/b")


def test_saves_under_a_moved_documents_folder_are_protected_with_mo2_profile_inis(tmp_path):
    inst, profile, docs = tmp_path / "Game", tmp_path / "MO2/profiles/P", tmp_path / "D/Dokumente/My Stuff/Skyrim VR"
    for d in (inst, profile, docs / "Saves"):
        d.mkdir(parents=True)
    (inst / "skyrim-paths.env").write_text(
        "SKYRIM_GAME_ROOT='{}'\nSKYRIM_CONFIG_DIR='{}'\nSKYRIM_DOCUMENTS_DIR='{}'\n".format(
            inst.as_posix(), profile.as_posix(), docs.as_posix()), encoding="utf-8")
    env = paths.read_install(inst.as_posix(), {})
    roots = paths.roots(inst.as_posix(), inst.as_posix(), env)
    from skyguard import writes
    assert writes.is_save((docs / "Saves" / ("a" + EXT)).as_posix().lower(), roots)


def test_setup_records_the_documents_folder():
    text = (REPO / "setup.sh").read_text(encoding="utf-8")
    assert "printf 'SKYRIM_DOCUMENTS_DIR=%s" in text
    assert 'DOCUMENTS_GAME_DIR="$CONFIG_DIR"' in text


# ---- review round 2: spellings the first fixes lost or missed
@pytest.mark.parametrize("cmd", [
    "f() { [ -n \"$2\" ] && shift; rm -rf \"$1\"; }; f Data",
    "f() { if [ -n \"$2\" ]; then shift; fi; rm -rf \"$1\"; }; f Data",
    "f() { set -- \"$@\"; rm -rf \"$1\"; }; f Data",
    "f() { echo shift; rm -rf \"$1\"; }; f Data",
    "f() { echo shift; rm -rf \"$@\"; }; f Data",
    "f() { shift $2; rm -rf \"$1\"; }; f x Data",
    "f() { ( shift ); rm -rf \"$1\"; }; f Data",
    "f() { true | shift; rm -rf \"$1\"; }; f Data",
], ids=["and-shift", "if-shift", "set-args", "echo-shift", "echo-shift-all", "shift-var", "subshell", "pipe"])
def test_round_two_function_forms_stay_refused_in_the_install(box, cmd):
    assert box(cmd).decision == "deny"           # the fixture's default cwd is the install


def test_a_brace_list_past_the_cap_keeps_its_last_member(box):
    cmd = "rm -rf {" + ",".join("a%02d" % i for i in range(65)) + ",$B/Game/Data}"
    assert box(cmd, cwd=Path(tempfile.gettempdir()).as_posix()).decision == "deny"


@pytest.mark.parametrize("cmd", [
    "$d='$B/other/x.txt'; Set-Content $d,$B/Game/Data/Skyrim.esm x",
    "$d='$B/other/x.txt'; Move-Item $d,$B/Game/Data/Skyrim.esm $B/bk",
], ids=["set-content", "move-item"])
def test_a_comma_list_keeps_its_group_through_a_variable(box, cmd):
    assert box(cmd, "powershell").decision == "deny"


def test_a_linear_shift_is_still_read_precisely():
    from skyguard.shellparse import _call_body
    assert _call_body('echo shift; echo "$1"', ("a",), (False,)) == 'echo shift; echo "a"'


@pytest.mark.parametrize("code", [
    "from pathlib import Path as P; P('{B}/Game/Data/X.esp').write_bytes(b'')",
    "import pathlib as pl; pl.Path('{B}/Game/Data/X.esp').write_bytes(b'')",
    "from pathlib import *; Path('{B}/Game/Data/X.esp').write_bytes(b'')",
], ids=["path-alias", "module-alias", "star-import"])
def test_literal_python_spellings_of_a_plugin_write_are_seen(box, code):
    assert box('python -c "{}"'.format(code.format(B="$B"))).decision == "deny"


@pytest.mark.parametrize("code", [
    "from shutil import copy; copy('{B}/x', '{B}/Game/Data/X.esp')",
    "import shutil; shutil.copy(src='{B}/x', dst='{B}/Game/Data/X.esp')",
], ids=["from-import", "keywords"])
def test_literal_python_copies_of_a_plugin_are_read_as_cp_is(box, code):
    """Seen in every spelling, and judged as `cp` into Data is: advice, because copying a built plugin in is routine."""
    v = box('python -c "{}"'.format(code.format(B="$B")))
    assert v.decision == "allow" and "X.esp" in v.context, v
