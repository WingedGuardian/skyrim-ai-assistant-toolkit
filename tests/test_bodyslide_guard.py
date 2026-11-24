"""BodySlide / Outfit Studio writes (4.3): S-BODYSLIDE-BUILD on the shell route, S-MCP-MESH-WRITE on the MCP route.

BodySlide's batch build writes into its settings' output path or the game's Data folder unless --targetdir says
otherwise; Outfit Studio's MCP server has no folder limits. Both are refused when the output lands where the game or
the mod manager loads files (Data, the settings folder, MO2), except BodySlide's own folder (CalienteTools/BodySlide).
Outfit Studio's tool names are not measured (no installed copy): the MCP rule is by path, on any save/export/build/
write/run-steps call of a server named Outfit Studio or BodySlide.
"""
import json
from pathlib import Path
import subprocess
import sys

import pytest

from conftest import REPO
sys.path.insert(0, str(REPO / "tools"))
from skyguard import codex_rules, health, mcp_rules, paths, rules  # noqa: E402
from skyguard.audit import Audit  # noqa: E402

ROOT = REPO.as_posix()
DATA = ROOT + "/Data"


def shell(cmd):
    action = json.dumps({"api_version": 1, "host": "generic", "tool": "Bash", "surface": "shell", "command": cmd})
    r = subprocess.run([sys.executable, "-B", str(REPO / "tools/agent-harness.py"), "check"], input=action,
                       capture_output=True, text=True, cwd=str(REPO), timeout=60)
    return json.loads(r.stdout.strip().splitlines()[-1])["result"]


@pytest.mark.parametrize("cmd", [
    '"BodySlide x64.exe" --groupbuild CBBE --quiet',
    "BodySlide.exe /groupbuild CBBE",
    "BodySlide.exe -f Bikini --quiet",
])
def test_a_batch_build_without_a_target_folder_is_refused(cmd):
    got = shell(cmd)
    assert got["decision"] == "deny" and "no --targetdir" in got["reason"] and "--targetdir" in got["reason"]


@pytest.mark.parametrize("cmd", [
    'BodySlide.exe --build "Outfit A" --targetdir "' + DATA + '/meshes" --quiet',
    'BodySlide.exe --groupbuild CBBE --targetdir="' + DATA + '/meshes"',
    'BodySlide.exe -gbuild CBBE -t=' + DATA + '/meshes',
    'BodySlide.exe -gbuild CBBE -t:' + DATA + '/meshes',
    'BodySlide.exe -gbuild:CBBE -t ' + DATA + '/meshes',
])
def test_a_batch_build_into_data_is_refused(cmd):
    got = shell(cmd)
    assert got["decision"] == "deny" and "meshes" in got["reason"] and "no --targetdir" not in got["reason"]


@pytest.mark.parametrize("cmd", [
    "BodySlide.exe -gbuild CBBE -t C:/Temp/bs-out --quiet",                       # a scratch folder: the safe way
    'BodySlide.exe -gbuild CBBE -t "' + DATA + '/CalienteTools/BodySlide/out"',     # BodySlide's own folder
    "BodySlide.exe --preview x.nif",                                               # not a build
    "echo BodySlide --groupbuild",                                                 # a mention is not a run
])
def test_safe_bodyslide_commands_are_allowed(cmd):
    assert shell(cmd)["decision"] == "allow"


@pytest.fixture
def judge():
    roots = paths.roots(ROOT, ROOT, {})
    audit = Audit(ROOT)
    return lambda name, args: mcp_rules.decide(name, args, audit, roots=roots).decision or "allow"


@pytest.mark.parametrize("name,args", [
    ("mcp__outfit-studio__save_project", {"outputPath": DATA + "/meshes/armor/x.nif"}),
    ("mcp__outfit-studio__export_nif", {"file": "../../meshes/x.nif"}),
    ("mcp__outfit-studio__run_steps", {"steps": [{"type": "Export: File", "path": DATA + "/meshes/y.nif"}]}),
    ("mcp__Body_Slide__build", {"targetdir": DATA + "/textures"}),
    ("mcp__outfit-studio__run_automation",                                         # an inline script
     {"script": '<Automation>\n<Step type="Export: File" path="' + DATA + '/meshes/x.nif"/>\n</Automation>'}),
    ("mcp__outfit-studio__run_steps", {"steps": '[{"path":"' + DATA + '/meshes/x.nif"}]'}),   # JSON in a string
    ("mcp__outfit-studio__save_project", {"outputPath": " " + DATA + "/meshes/x.nif"}),       # a leading space
    ("mcp__outfit-studio__save_project", {"outputPath": "file:///" + DATA + "/meshes/x.nif"}),
    ("mcp__outfit__studio__export", {"file": DATA + "/meshes/x.nif"}),                        # separators in the name
    # review 4.3 I1: a path list on one line, a protected path behind a scratch one
    ("mcp__outfit-studio__run_automation", {"script": "load C:/Temp/a.nif export " + DATA + "/meshes/x.nif"}),
    ("mcp__outfit-studio__run_automation", {"script": "C:/Temp/a.nif;" + DATA + "/meshes/b.nif"}),
    ("mcp__outfit-studio__run_automation", {"script": "/tmp/a.nif " + DATA + "/meshes/b.nif"}),
    ("mcp__outfit-studio__build", {"cmd": "targetdir " + DATA + " then quit"}),             # a path then words
    # review 4.3 I2: a relative `..` out of BodySlide's folder inside a script
    ("mcp__outfit-studio__run_automation", {"script": '<Step type="Export: File" path="../../meshes/x.nif"/>'}),
    # review 4.3 I4: a tool name without a known write word is judged too (names are unmeasured)
    ("mcp__outfit-studio__run_step", {"path": DATA + "/meshes/x.nif"}),
    ("mcp__outfit-studio__convert", {"to": DATA + "/meshes/x.nif"}),
    ("mcp__outfit-studio__set_output_path", {"path": DATA + "/meshes"}),
    ("mcp__outfit-studio__load_and_export", {"out": DATA + "/meshes/x.nif"}),      # a read verb with a write word
    # confirmation review N1: a read word in a type/command never switches the rule off for the rest of the call
    ("mcp__outfit-studio__run_automation", {"command": "load C:/Temp/p.osp; export " + DATA + "/meshes/x.nif"}),
    ("mcp__outfit-studio__run_steps", {"steps": [{"type": "Load: Outfit", "path": "C:/Temp/a.nif",
                                                  "outputPath": DATA + "/meshes/x.nif"}]}),
    ("mcp__outfit-studio__run_steps", {"steps": [{"action": "open-and-export", "path": DATA + "/meshes/x.nif"}]}),
    ("mcp__outfit-studio__save_project", {"type": "Load: Outfit", "path": DATA + "/meshes/x.nif"}),   # not a step
    ("mcp__outfit-studio__run_automation",
     {"script": '<Step type="Load: Outfit" path="C:/Temp/a.nif" output="' + DATA + '/meshes/x.nif"/>'}),
    # N2: a `|` between paths
    ("mcp__outfit-studio__run_automation", {"script": "C:/Temp/a.nif|" + DATA + "/meshes/x.nif"}),
    # N4: a tool that takes its source away has its inputs judged
    ("mcp__outfit-studio__move_file", {"source": DATA + "/meshes/x.nif", "destination": "C:/Temp/"}),
])
def test_an_outfit_studio_write_into_loaded_folders_is_refused(judge, name, args):
    assert judge(name, args) == "deny"


@pytest.mark.parametrize("name,args", [
    ("mcp__outfit-studio__save_project", {"outputPath": "C:/Temp/os/x.osp"}),
    ("mcp__outfit-studio__save_project", {"outputPath": DATA + "/CalienteTools/BodySlide/SliderSets/x.osp"}),
    ("mcp__outfit-studio__load_project", {"path": DATA + "/meshes/x.nif"}),         # a read
    ("mcp__OutfitStudio__render_view", {"out": DATA + "/x.png"}),                   # not a write tool by name
    ("mcp__other__save_project", {"outputPath": DATA + "/meshes/x.nif"}),          # not Outfit Studio
    ("mcp__outfit-studio__save_project", {"outputPath": "SliderSets/../ShapeData/x.nif"}),   # stays in BodySlide's
    ("mcp__outfit-studio__get_shape_info", {"path": DATA + "/meshes/x.nif"}),       # read verb, no write word
    ("mcp__outfit-studio__import_nif", {"path": DATA + "/meshes/armor/x.nif"}),     # N3: an import only reads
    ("mcp__outfit-studio__validate_mesh", {"path": DATA + "/meshes/armor/x.nif"}),
    # review 4.3 I3: an INPUT from Data or a mod with the output in scratch is ordinary work
    ("mcp__outfit-studio__run_steps", {"steps": [{"type": "Load: Reference", "path": DATA + "/meshes/x.nif"},
                                                 {"type": "Export: File", "path": "C:/Temp/x.nif"}]}),
    ("mcp__outfit-studio__export_nif", {"inputPath": DATA + "/meshes/x.nif", "outputPath": "C:/Temp/x.nif"}),
    ("mcp__outfit-studio__run_automation",
     {"script": '<Step type="Load: Outfit" path="' + DATA + '/meshes/x.nif"/><Step type="Export: File" path="C:/Temp/x.nif"/>'}),
    ("mcp__outfit-studio__run_steps",                                               # the same as JSON in a string
     {"steps": '[{"type": "Load: Reference", "path": "' + DATA + '/meshes/x.nif"}, {"type": "Export", "path": "C:/Temp/x.nif"}]'}),
])
def test_outfit_studio_reads_and_safe_writes_are_allowed(judge, name, args):
    assert judge(name, args) == "allow"


def test_absolute_paths_inside_text_are_found_on_every_os_spelling():
    for path in ("/home/u/Skyrim/Data/meshes/x.nif", "D:/Games/Skyrim/Data/meshes/x.nif", "/d/Games/Data/x.nif"):
        text = '<Step type="Export: File" path="' + path + '"/>'
        assert path in list(mcp_rules._absolute_candidates(text)), path
    assert list(mcp_rules._absolute_candidates('<Step path="SliderSets/a/b.osp"/>')) == []   # relative: not absolute
    assert "C:/Program Files (x86)/Steam/x.nif" in list(mcp_rules._absolute_candidates("C:/Program Files (x86)/Steam/x.nif"))


def test_paths_after_any_separator_or_a_short_flag_are_found():
    """Confirmation review N2: a drive path starts anywhere not glued to a letter or digit, and after a short flag."""
    for text, path in (("C:/Temp/a.nif|D:/Games/Data/x.nif", "D:/Games/Data/x.nif"),
                       ("-oD:/Games/Data/x.nif", "D:/Games/Data/x.nif"),
                       ("(D:/Games/Data/x.nif)", "D:/Games/Data/x.nif"),
                       ("cmd `/home/u/Data/x.nif`", "/home/u/Data/x.nif"),
                       ("a.nif|/home/u/Data/x.nif", "/home/u/Data/x.nif"),
                       ("(targetdir D:/Games/Data)", "D:/Games/Data"),
                       ("D:/Mods/Armor (v2)/x.nif", "D:/Mods/Armor (v2)/x.nif")):    # a matched bracket stays
        assert path in list(mcp_rules._absolute_candidates(text)), text


def test_a_folder_name_with_a_space_is_not_cut_into_a_protected_one():
    """Confirmation review N5: `<root>/Data Exports/x.nif` is not `<root>/Data`; `<root>/Data then quit` is."""
    assert "D:/Games/Data" not in list(mcp_rules._absolute_candidates("D:/Games/Data Exports/x.nif"))
    assert "D:/Games/Data" in list(mcp_rules._absolute_candidates("targetdir D:/Games/Data then quit"))


def test_the_whole_of_a_long_string_is_scanned():
    roots = paths.roots(ROOT, ROOT, {})
    script = "x" * 70000 + " export " + DATA + "/meshes/x.nif"
    assert mcp_rules.mesh_write_target("run_automation", {"script": script}, roots)


def test_a_relative_path_out_of_bodyslides_folder_is_refused_even_without_roots():
    audit = Audit(ROOT)
    v = mcp_rules.decide("mcp__outfit-studio__export_nif", {"file": "../../meshes/x.nif"}, audit, roots=None)
    assert v.decision == "deny"


def test_the_outfit_studio_server_is_judged_on_every_host():
    assert mcp_rules.judged_server("mcp__outfit-studio__save_project")
    assert not mcp_rules.judged_server("mcp__other__save_project")
    for name in ("mcp__outfit-studio__save_project", "mcp__OutfitStudio__export", "mcp__body_slide__build",
                 "mcp__outfit__studio__save_project", "mcp__Outfit - Studio__save"):
        assert health.CLAUDE_COVERED_MCP.fullmatch(name), name
    settings = json.loads((REPO / "agent/targets/claude/settings.json").read_text(encoding="utf-8"))
    assert any(h.get("matcher") == health.CLAUDE_COVERED_MCP.pattern for h in settings["hooks"]["PreToolUse"])
    js = (REPO / "agent/targets/opencode/plugins/skyguard.js").read_text(encoding="utf-8")
    assert '["outfitstudio", "bodyslide"]' in js


def test_both_rules_are_refusals_and_classified_for_codex():
    for rid in ("S-BODYSLIDE-BUILD", "S-MCP-MESH-WRITE"):
        assert rules.RULES[rid]["decision"] == "deny"
    assert "shell.S-BODYSLIDE-BUILD" in codex_rules.CLASSIFICATION
    assert "mcp.S-MCP-MESH-WRITE" in codex_rules.CLASSIFICATION
