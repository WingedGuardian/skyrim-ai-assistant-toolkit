"""missing-textures: texture paths parsed from nif_inspect, absence taken from asset_status, and an incomplete read
never reported as a clean or a missing answer. House Carl itself is replaced by canned answers."""
import importlib.util
import json
import sys

from conftest import REPO

sys.path.insert(0, str(REPO / "tools"))
spec = importlib.util.spec_from_file_location("missing_textures", REPO / "tools" / "missing-textures.py")
mt = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mt)

NIF = "\n".join([
    "nif inspect — profile 'profile'  (2 meshes)", "",
    "meshes\\weapons\\a.nif", "  read from: \"Data\" (loose)", "  version: Gamebryo File Format  [Skyrim SE]",
    "--- paths (embedded texture-set slots) ---",
    "  'Blade':", "    tex[0] (Diffuse): textures\\weapons\\a.dds", "    tex[1] (Normal): textures\\weapons\\a_n.dds",
    "  'Gem':", "    tex[0] (Diffuse): weapons\\gem.dds", "",
    "meshes\\weapons\\gone.nif", "  ABSENT — no active mod or BSA provides this mesh path."])


def asset(present, incomplete=False):
    def answer(paths):
        return json.dumps({"read_incomplete": incomplete, "bsa_failures": [],
                           "results": [{"path": p, "exists": p.lower() in present} for p in paths]})
    return answer


def fake(present, incomplete=False):
    status = asset({p.lower() for p in present}, incomplete)

    def call(tool, arguments):
        if tool == "housecarl_nif_inspect":
            return NIF
        return status(arguments["asset_paths"])
    return call


def test_slots_shapes_and_unread_meshes_are_parsed():
    uses, unread, unanswered = mt.textures_of(NIF, ["meshes/weapons/a.nif", "meshes\\weapons\\gone.nif"])
    assert unanswered == []
    assert set(uses) == {"textures\\weapons\\a.dds", "textures\\weapons\\a_n.dds", "textures\\weapons\\gem.dds"}
    assert uses["textures\\weapons\\gem.dds"] == [("meshes/weapons/a.nif", "Gem")]       # prefix added, shape kept
    assert unread == ["meshes\\weapons\\gone.nif"]


def test_a_texture_nothing_provides_is_reported_with_its_users(monkeypatch, capsys):
    monkeypatch.setattr(mt, "call", fake({"textures\\weapons\\a.dds", "textures\\weapons\\gem.dds"}))
    assert mt.main(["meshes\\weapons\\a.nif", "meshes\\weapons\\gone.nif"]) == 1
    out = capsys.readouterr().out
    assert "textures\\weapons\\a_n.dds" in out and "(Blade)" in out and "mesh not found: meshes\\weapons\\gone.nif" in out


def test_all_present_is_clean(monkeypatch):
    monkeypatch.setattr(mt, "call", fake({"textures\\weapons\\a.dds", "textures\\weapons\\a_n.dds", "textures\\weapons\\gem.dds"}))
    assert mt.main(["meshes\\weapons\\a.nif"]) == 0


def test_an_incomplete_archive_read_is_could_not_check(monkeypatch, capsys):
    monkeypatch.setattr(mt, "call", fake(set(), incomplete=True))
    assert mt.main(["meshes\\weapons\\a.nif"]) == 2
    assert "COULD NOT CHECK" in capsys.readouterr().err


def test_a_gateway_failure_is_could_not_check(monkeypatch, capsys):
    def broken(tool, arguments):
        raise RuntimeError("housecarl_nif_inspect failed: gateway not set up")
    monkeypatch.setattr(mt, "call", broken)
    assert mt.main(["meshes\\weapons\\a.nif"]) == 2
    assert "gateway not set up" in capsys.readouterr().err


def test_a_mesh_house_carl_gave_no_answer_for_is_could_not_check(monkeypatch, capsys):
    """4.1 review: a mesh missing from the output, or named without being read, must never count as clean."""
    present = {"textures\\weapons\\a.dds", "textures\\weapons\\a_n.dds", "textures\\weapons\\gem.dds"}
    monkeypatch.setattr(mt, "call", fake(present))
    assert mt.main(["meshes\\weapons\\a.nif", "meshes\\weapons\\never-mentioned.nif"]) == 2
    assert "no answer" in capsys.readouterr().err
    _, _, unanswered = mt.textures_of("meshes\\x.nif\n  could not parse: bad header", ["meshes/x.nif"])
    assert unanswered == ["meshes/x.nif"]


def test_a_forward_slash_header_is_matched():
    text = NIF.replace("meshes\\weapons\\a.nif", "meshes/weapons/a.nif")
    uses, _, unanswered = mt.textures_of(text, ["meshes\\weapons\\a.nif"])
    assert uses and unanswered == []


def test_a_texture_asset_status_did_not_answer_for_is_could_not_check(monkeypatch, capsys):
    def call(tool, arguments):
        if tool == "housecarl_nif_inspect":
            return NIF
        return json.dumps({"read_incomplete": False, "bsa_failures": [], "results": []})
    monkeypatch.setattr(mt, "call", call)
    assert mt.main(["meshes\\weapons\\a.nif"]) == 2
    assert "no answer" in capsys.readouterr().err


def test_an_absent_mesh_alone_is_not_clean(monkeypatch):
    monkeypatch.setattr(mt, "call", fake(set()))
    assert mt.main(["meshes\\weapons\\gone.nif"]) == 1
