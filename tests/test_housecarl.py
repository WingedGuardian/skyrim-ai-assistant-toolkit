"""Behavioral controls for authoritative inputs and the publication boundary; no real game access."""
import asyncio
import json
import os
from pathlib import Path
import struct
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from housecarl_integration import Refusal
from housecarl_integration import files, gateway, order


def plugin(path, flags=0, masters=(), version=None, onam=(), records=()):
    body = b"" if version is None else b"HEDR" + struct.pack("<HfiI", 12, version, len(records), 0x800)
    for name in masters:
        value = name.encode("cp1252") + b"\0"
        body += b"MAST" + struct.pack("<H", len(value)) + value
    if onam:
        body += b"ONAM" + struct.pack("<H", 4 * len(onam)) + b"".join(struct.pack("<I", v) for v in onam)
    tail = b"".join(sig + struct.pack("<III", 0, 0, formid) + b"\0" * 8 for sig, formid in records)
    path.write_bytes(b"TES4" + struct.pack("<II", len(body), flags) + b"\0" * 12 + body + tail)


class _Source:
    """A stand-in for the urlopen response: `chunks` are returned in turn; an exception instance is raised."""
    def __init__(self, *chunks):
        self.chunks = list(chunks)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self, n=-1):
        if not self.chunks:
            return b""
        c = self.chunks.pop(0)
        if isinstance(c, BaseException):
            raise c
        return c


def test_an_interrupted_download_never_leaves_a_file_at_the_archive_name(tmp_path, monkeypatch):
    """Review L5 F6: a partial zip at the final name blocked every later setup with a hash error."""
    from housecarl_integration import setup as hc_setup
    archive = tmp_path / "houseCARL-2.1.2.zip"
    with pytest.raises(OSError):
        hc_setup.download_archive(archive, opener=lambda *a, **k: _Source(b"PK partial", OSError("connection reset")))
    assert list(tmp_path.iterdir()) == []
    with pytest.raises(Refusal, match="does not match pinned"):
        hc_setup.download_archive(archive, opener=lambda *a, **k: _Source(b"PK the wrong bytes"))
    assert not archive.exists() and [p.name.split(".rejected-")[0] for p in tmp_path.iterdir()] == [archive.name]
    good = b"PK the pinned bytes"
    import hashlib
    monkeypatch.setattr(hc_setup, "ARCHIVE_SHA256", hashlib.sha256(good).hexdigest())
    hc_setup.download_archive(archive, opener=lambda *a, **k: _Source(good))    # twin: a good download lands
    assert archive.read_bytes() == good


def test_a_staged_patch_name_with_brackets_is_a_name_not_a_glob(tmp_path):
    """Review L5 F3: `glob("*/Foo [Fix].esp")` matched `Foo F.esp` and not the literal file."""
    for folder, name in (("abc", "Foo [Fix].esp"), ("def", "Foo F.esp")):
        (tmp_path / "patches" / folder).mkdir(parents=True)
        plugin(tmp_path / "patches" / folder / name)
    assert gateway.staged_patches(tmp_path, "Foo [Fix].esp") == [tmp_path / "patches" / "abc" / "Foo [Fix].esp"]
    assert gateway.staged_patches(tmp_path, "foo f.ESP") == [tmp_path / "patches" / "def" / "Foo F.esp"]
    assert gateway.staged_patches(tmp_path, "Foo [Gix].esp") == []
    assert gateway.staged_patches(tmp_path / "none", "Foo F.esp") == []


@pytest.fixture
def install(tmp_path):
    data = tmp_path / "Data"
    data.mkdir()
    ini = tmp_path / "ini"
    ini.mkdir()
    plugins = tmp_path / "plugins.txt"
    plugins.write_text("*Sword.esp\n*LateMaster.esp\nDisabled.esp\n", encoding="cp1252")
    plugin(data / "Skyrim.esm", 1)
    plugin(data / "LateMaster.esp", 1, ["Skyrim.esm"])
    plugin(data / "Sword.esp", 0, ["Skyrim.esm"])
    config = {"root": str(tmp_path), "state_dir": str(tmp_path / ".skyguard/housecarl"),
              "data_dir": str(data), "plugins_txt": str(plugins), "edition": "SE", "config_dir": str(ini),
              "authority": "wrye-bash", "asset_provider": "deployed-data"}
    return config


def test_wrye_order_does_not_consult_stale_loadorder(install):
    p = Path(install["plugins_txt"])
    p.with_name("loadorder.txt").write_text("NotInstalled.esp\n")
    before = p.read_bytes()
    snap = order.capture(install)
    assert snap.names == ["Skyrim.esm", "LateMaster.esp", "Sword.esp"]
    assert snap.inactive == ["Disabled.esp"]
    assert p.read_bytes() == before


def test_membership_not_in_old_loadorder_is_kept(install):
    p = Path(install["plugins_txt"])
    plugin(Path(install["data_dir"]) / "New.esp")
    with p.open("a") as stream:
        stream.write("*New.esp\n")
    assert "New.esp" in order.capture(install).names


@pytest.mark.parametrize("text,reason", [("*Missing.esp\n", "missing"),
                                        ("*Sword.esp\n*Sword.esp\n", "Duplicate"),
                                        ("*../Other.esp\n", "basename")])
def test_invalid_authority_refuses_instead_of_dropping(install, text, reason):
    Path(install["plugins_txt"]).write_text(text)
    with pytest.raises(Refusal, match=reason):
        order.capture(install)


def test_missing_dependency_does_not_auto_activate_it(install):
    plugin(Path(install["data_dir"]) / "Sword.esp", masters=["Missing.esm"])
    with pytest.raises(Refusal, match="inactive/missing"):
        order.capture(install)


def test_extended_header_payload_is_read_without_truncation(tmp_path):
    p = tmp_path / "Extended.esp"
    payload = b"z" * 70000
    body = b"XXXX" + struct.pack("<HI", 4, len(payload)) + b"ONAM\0\0" + payload
    p.write_bytes(b"TES4" + struct.pack("<II", len(body), 0) + b"\0" * 12 + body)
    assert order.plugin_header(p)["masters"] == []


def test_cp1252_and_ghosted_names(install):
    plugin(Path(install["data_dir"]) / "Café.esp")
    Path(install["plugins_txt"]).write_bytes("*Café.esp.ghost\n".encode("cp1252"))
    assert order.capture(install).names[-1] == "Café.esp"


def test_vr_requires_esl_support(install):
    install["edition"] = "VR"
    plugin(Path(install["data_dir"]) / "Sword.esp", flags=0x200)
    with pytest.raises(Refusal, match="ESL Support"):
        order.capture(install)


def test_fingerprint_changes_for_order_or_plugin_changes(install):
    a = order.capture(install).fingerprint
    Path(install["plugins_txt"]).write_text("*LateMaster.esp\n*Sword.esp\n")
    b = order.capture(install).fingerprint
    assert a != b
    plugin(Path(install["data_dir"]) / "Sword.esp", masters=["LateMaster.esp"])
    assert order.capture(install).fingerprint != b


def test_fingerprint_changes_for_equal_size_body_change_with_preserved_mtime(install):
    target = Path(install["data_dir"]) / "Sword.esp"
    with target.open("ab") as stream:
        stream.write(b"synthetic record body A")
    before = order.capture(install).fingerprint
    stamp = target.stat()
    with target.open("r+b") as stream:
        stream.seek(-1, 2)
        stream.write(b"B")
    os.utime(target, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
    assert order.capture(install).fingerprint != before


def test_new_publication_does_not_clobber_a_concurrently_created_target(tmp_path, monkeypatch):
    candidate, target = tmp_path / "candidate.esp", tmp_path / "new.esp"
    candidate.write_bytes(b"prepared")
    original = {name: getattr(files.os, name) for name in ("replace", "rename", "link")}

    def racing(name):
        def invoke(source, destination, *args, **kwargs):
            if Path(destination) == target:
                target.write_bytes(b"concurrent unrelated content")
            return original[name](source, destination, *args, **kwargs)
        return invoke

    for name in original:
        monkeypatch.setattr(files.os, name, racing(name))
    with pytest.raises((Refusal, FileExistsError)):
        files.publish(candidate, target, None)
    assert target.read_bytes() == b"concurrent unrelated content"
    assert candidate.read_bytes() == b"prepared"


def test_private_profile_does_not_rewrite_source_and_carries_vr_archive(install):
    install["edition"] = "VR"
    source = Path(install["plugins_txt"])
    before = source.read_bytes()
    folder = Path(install["state_dir"]) / "profile"
    order.write_profile(order.capture(install), folder, install)
    assert source.read_bytes() == before
    assert "Skyrim_VR - Main.bsa" in (folder / "Skyrim.ini").read_text()


def test_backup_is_independent_and_verified(tmp_path):
    target = tmp_path / "A.esp"
    target.write_bytes(b"old bytes")
    saved = files.backup(target, tmp_path / "backups")
    target.write_bytes(b"new bytes")
    assert Path(saved["backup"]).read_bytes() == b"old bytes"
    assert files.sha(saved["backup"]) == saved["sha256"]


def test_backup_copy_corruption_refuses(tmp_path, monkeypatch):
    target = tmp_path / "A.esp"
    target.write_bytes(b"old bytes")
    original = files.durable.digest
    def corrupt_readback(path, deadline=None):
        if Path(path) != target:
            Path(path).write_bytes(b"corrupt")
        return original(path, deadline)
    monkeypatch.setattr(files.durable, "digest", corrupt_readback)
    with pytest.raises(Refusal, match="hash verification"):
        files.backup(target, tmp_path / "backups")
    assert target.read_bytes() == b"old bytes"


def test_publication_breaks_hardlink_without_editing_source(tmp_path):
    source, deployed, candidate = [tmp_path / n for n in ("source.esp", "deployed.esp", "candidate.esp")]
    source.write_bytes(b"old")
    os.link(source, deployed)
    candidate.write_bytes(b"new")
    files.publish(candidate, deployed, files.sha(deployed))
    assert source.read_bytes() == b"old"
    assert deployed.read_bytes() == b"new"


def test_stale_target_and_commit_failure_keep_original(tmp_path, monkeypatch):
    target, candidate = tmp_path / "A.esp", tmp_path / "candidate.esp"
    target.write_bytes(b"old")
    before = files.sha(target)
    candidate.write_bytes(b"new")
    target.write_bytes(b"user edit")
    with pytest.raises(Refusal, match="changed"):
        files.publish(candidate, target, before)
    assert target.read_bytes() == b"user edit"
    monkeypatch.setattr(files.os, "replace", lambda *a: (_ for _ in ()).throw(OSError("locked")))
    with pytest.raises(OSError, match="locked"):
        files.publish(candidate, target, files.sha(target))
    assert target.read_bytes() == b"user edit"


def test_concurrent_transaction_refused(tmp_path):
    with files.exclusive(tmp_path / "lock"):
        with pytest.raises(Refusal, match="Another"):
            with files.exclusive(tmp_path / "lock"):
                pytest.fail("second writer entered")


@pytest.mark.parametrize("tool,args", [("housecarl_compact_plugin", {}),
                                      ("housecarl_check", {"findings": ["facegen"]}),
                                      ("housecarl_skse", {"findings": "pairing"}),
                                      ("housecarl_records", {"formids": ["FE001234"]}),
                                      ("housecarl_records", {"to_file": "C:/outside.jsonl"})])
def test_unqualified_operations_cannot_bypass_gateway(install, tool, args):
    with pytest.raises(Refusal):
        gateway.check_arguments(tool, args, install)


def test_status_is_explicit_about_unqualified_runtime(install):
    obj = asyncio.run(gateway.Gateway(install).call("toolkit_housecarl_status", {}))
    assert obj["context"]["authority"] == "wrye-bash"
    assert obj["context"]["capabilities"]["runtime_addresses"] is False
    assert obj["active_plugins"] == ["Skyrim.esm", "LateMaster.esp", "Sword.esp"]
    filtered = asyncio.run(gateway.Gateway(install).call("housecarl_load_order_status", {"filter": "sword.esp"}))
    assert filtered["active_plugins"] == ["Sword.esp"]
    with pytest.raises(Refusal, match="alternate-profile"):
        asyncio.run(gateway.Gateway(install).call("housecarl_load_order_status", {"profile": "Unrelated"}))


def test_restore_refuses_concurrent_change_and_keeps_permanent_backups(install):
    state = Path(install["state_dir"])
    target = Path(install["data_dir"]) / "Sword.esp"
    saved = files.backup(target, state / "backups")
    original = target.read_bytes()
    target.write_bytes(b"published")
    ident = "a" * 32
    receipt = {"state": "published", "target": str(target), "backup": saved,
               "after_sha256": files.sha(target)}
    files.json_write(state / "transactions" / ident / "receipt.json", receipt)
    target.write_bytes(b"changed since")
    with pytest.raises(Refusal, match="no longer matches"):
        gateway.Gateway(install).restore(ident)
    target.write_bytes(b"published")
    gateway.Gateway(install).restore(ident)
    assert target.read_bytes() == original
    assert Path(saved["backup"]).is_file()
    assert len(list((state / "backups").glob("*/manifest.json"))) == 2


def test_mo2_uses_checked_membership_and_highest_supplier(install):
    root = Path(install["root"])
    mods = root / "mods"
    for name in ("High", "Low"):
        (mods / name).mkdir(parents=True)
        plugin(mods / name / "Sword.esp", masters=["Skyrim.esm"])
    profile = Path(install["plugins_txt"]).parent
    (profile / "modlist.txt").write_text("+High\n+Low\n")
    (profile / "loadorder.txt").write_text("Skyrim.esm\nLateMaster.esp\nSword.esp\nDisabled.esp\n")
    install.update(asset_provider="mo2", authority="mo2-profile", mods_dir=str(mods))
    snapshot = order.capture(install)
    assert snapshot.paths["Sword.esp"] == mods / "High/Sword.esp"
    assert "Disabled.esp" not in snapshot.names
    (profile / "loadorder.txt").write_text("Skyrim.esm\n")
    with pytest.raises(Refusal, match="omits checked"):
        order.capture(install)


def test_dependency_after_dependent_refuses_instead_of_sorting(install):
    data = Path(install["data_dir"])
    plugin(data / "Sword.esp", masters=["After.esp"])
    plugin(data / "After.esp")
    Path(install["plugins_txt"]).write_text("*Sword.esp\n*After.esp\n")
    with pytest.raises(Refusal, match="before one of its masters"):
        order.capture(install)


def test_manifest_expansion_is_in_receipt_edit_set(install):
    request = Path(install["root"]) / "request.json"
    request.write_text(json.dumps([{"target": "012EB7:Skyrim.esm", "from": "012EB8:Skyrim.esm"}]))
    args = gateway.check_arguments("housecarl_apply", {"assignments": "@" + str(request)}, install)
    assert gateway.allowed_records(args) == {"12eb7:skyrim.esm"}


@pytest.mark.parametrize("stage", ["backup", "baseline", "backend", "candidate", "stale", "publication"])
def test_transaction_failures_never_publish(install, monkeypatch, stage):
    from types import SimpleNamespace
    target = Path(install["data_dir"]) / "Sword.esp"
    original = target.read_bytes()
    calls = []

    def fail(*_args, **_kwargs):
        raise Refusal("injected " + stage)

    def check(_config, _candidate, _snapshot, before=None, allowed=None):
        if stage == "baseline" or (stage == "candidate" and before is not None):
            fail()
        return {"records": {}, "errors": [], "complete": True}

    class FakeBackend:
        def __init__(self, config, snapshot, work):
            self.work = work

        async def __aenter__(self):
            (self.work / "profile").mkdir()
            (self.work / "profile/modlist.txt").write_text("")
            return self

        async def __aexit__(self, *_exc):
            return False

        async def call(self, _name, _args):
            calls.append("writer")
            if stage == "backend":
                fail()
            # The original's own header, so each stage is refused by ITS check, not by the header-retention check.
            plugin(self.work / "mods/Target/Sword.esp", flags=0, masters=["Skyrim.esm"])
            if stage == "stale":
                Path(install["plugins_txt"]).write_text("*Sword.esp\n")
            return SimpleNamespace(content=[SimpleNamespace(type="text", text=json.dumps({"ok": True, "verify_ran": True}))])

    monkeypatch.setattr(gateway, "Backend", FakeBackend)
    monkeypatch.setattr(gateway, "independent_check", check)
    if stage == "backup":
        monkeypatch.setattr(gateway, "backup", fail)
    if stage == "publication":
        monkeypatch.setattr(gateway, "publish", fail)
    with pytest.raises(Refusal):
        asyncio.run(gateway.Gateway(install).call("housecarl_apply", {
            "in_place": "Sword.esp", "ops": [{"formid": "000800:Sword.esp", "field_path": "Name", "value": "new"}]}))
    assert target.read_bytes() == original
    if stage in ("backup", "baseline"):
        assert calls == []


def test_independent_checker_rejects_unrequested_new_records(install, monkeypatch):
    from types import SimpleNamespace
    target = Path(install["data_dir"]) / "Sword.esp"

    def inspect(command, **_kwargs):
        Path(command[-1]).write_text(json.dumps({"complete": True, "records": {"800:sword.esp": "unexpected"}, "errors": []}))
        return SimpleNamespace(stdout="RESULT: OK", stderr="")

    monkeypatch.setattr(gateway.subprocess, "run", inspect)
    with pytest.raises(Refusal, match="outside requested records"):
        gateway.independent_check(install, target, order.capture(install), {"records": {}, "errors": []}, set())


def test_optional_doctor_does_not_claim_host_discovery(install, monkeypatch):
    from skyguard.doctor import _housecarl
    from housecarl_integration import runtime
    state = Path(install["state_dir"])
    files.json_write(state / "config.json", install)
    monkeypatch.setattr(runtime, "verify_installation", lambda _config: 4)
    row, = _housecarl(install["root"])
    assert row.status == "unknown" and not row.counted
    assert "host discovery" in row.reason


@pytest.mark.parametrize("text", ["resolver: 2 plugins resolved to real files", "missing resolver report",
                                  "resolver: 3 plugins resolved to real files\n[!] EXCLUDED plugins (1)"])
def test_partial_backend_index_is_never_accepted(text):
    from types import SimpleNamespace
    from housecarl_integration.runtime import check_index
    result = SimpleNamespace(content=[SimpleNamespace(text=text)])
    with pytest.raises(Refusal, match="complete captured order"):
        check_index(result, 3)
    check_index(SimpleNamespace(content=[SimpleNamespace(text="resolver: 3 plugins resolved to real files")]), 3)


def test_backend_cleanup_keeps_tool_refusal_as_a_refusal(install):
    from housecarl_integration.runtime import Backend
    calls = []

    class Resources:
        async def aclose(self):
            calls.append("closed")

    async def run():
        backend = Backend(install, order.capture(install))
        backend.stack = Resources()
        # Exercise exit directly without opening a process or importing the optional SDK.
        error = Refusal("bad request")
        assert await backend.__aexit__(Refusal, error, None) is False

    asyncio.run(run())
    assert calls == ["closed"]


def test_xedit_error_reports_preserve_itm_warnings_and_block_references():
    import shutil
    import subprocess
    if not shutil.which("node"):
        pytest.skip("Node not installed")
    verifier = Path(__file__).resolve().parents[1] / "tools/housecarl-verify.js"
    code = """
const assert = require('assert');
const {parseErrorReports: parse} = require(process.argv[1]);
assert.deepEqual(parse(['{"errors":[]}']), {errors:[], warnings:[]});
const result = parse([JSON.stringify({errors:[{group:0},{group:1},{group:4},{group:999}]})]);
assert.equal(result.warnings.length, 2);
assert.equal(result.errors.length, 2);
assert.throws(() => parse(['{}']));
assert.throws(() => parse(['broken']));
"""
    subprocess.run(["node", "-e", code, str(verifier)], check=True, capture_output=True)


# --- Issue 11: archive lists the backend receives ------------------------------------------------------------------
# Every case is a synthetic tmp_path layout; no real game folder, INI or backend is read or launched.

def _archive_names(ini):
    """Port of the pinned backend's ParseResourceArchiveList (houseCARL 4e28505, ArchiveDiscovery.cs): every entry of
    sResourceArchiveList and sResourceArchiveList2 in an [Archive] section, in file order."""
    names, inside = [], False
    for line in Path(ini).read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line[0] == ";":
            continue
        if line[0] == "[":
            inside = line.casefold() == "[archive]"
            continue
        key, eq, value = line.partition("=")
        if inside and eq and key.strip().casefold() in ("sresourcearchivelist", "sresourcearchivelist2"):
            names += [p.strip() for p in value.split(",") if p.strip()]
    return names


def _backend_base_archives(profile, game_root):
    """The pinned backend's ReadBaseArchiveNames: the profile Skyrim.ini, and the game dir's only when the profile's
    lists none. This is what decides which base BSAs an asset answer can see."""
    for ini in (Path(profile) / "Skyrim.ini", Path(game_root) / "Skyrim.ini"):
        if ini.is_file() and _archive_names(ini):
            return _archive_names(ini)
    return []


def _ini(path, **keys):
    path.write_text("[Archive]\n" + "".join(k + "=" + v + "\n" for k, v in keys.items())
                    + "[General]\nsLanguage=ENGLISH\n", encoding="utf-8")
    return path


def _view(install):
    snap = order.capture(install)
    folder = Path(install["state_dir"]) / "views" / snap.fingerprint / "profile"
    order.write_profile(snap, folder, install)
    return snap, _backend_base_archives(folder, Path(install["data_dir"]).parent)


@pytest.mark.parametrize("edition", ["SE", "VR"])
def test_root_ini_base_archives_reach_the_backend(install, edition):
    install["edition"] = edition
    root, user = Path(install["root"]), Path(install["config_dir"])
    main = user / order.MAIN_INI[edition]
    _ini(main, bInvalidateOlderFiles="1")
    _ini(root / "Skyrim.ini", sResourceArchiveList="Skyrim - Meshes0.bsa, Skyrim - Meshes1.bsa",
         sResourceArchiveList2="Skyrim - Textures0.bsa")
    before = {p: p.read_bytes() for p in (main, root / "Skyrim.ini")}
    snap, names = _view(install)
    assert names[:3] == ["Skyrim - Meshes0.bsa", "Skyrim - Meshes1.bsa", "Skyrim - Textures0.bsa"]
    assert snap.archive_coverage["complete"] is True      # one whole file: the backend's rule and the layering agree
    assert {p: p.read_bytes() for p in before} == before
    # The main INI's own sResourceArchiveList2 overrides the root's for that key only, and that mix is not "complete":
    # the backend reads one whole file and Wrye never reads the root, so neither qualifies the layering.
    _ini(main, bInvalidateOlderFiles="1", sResourceArchiveList2="Mod - Extra.bsa")
    snap, names = _view(install)
    assert "Skyrim - Meshes1.bsa" in names and "Mod - Extra.bsa" in names
    assert "Skyrim - Textures0.bsa" not in names
    assert snap.archive_coverage["complete"] is False
    assert any("more than one INI" in u for u in snap.archive_coverage["uncertain"])
    # Both keys from the main INI: one file again, complete.
    _ini(main, sResourceArchiveList="Mod - A.bsa", sResourceArchiveList2="Mod - Extra.bsa")
    snap, names = _view(install)
    assert names[:2] == ["Mod - A.bsa", "Mod - Extra.bsa"] and snap.archive_coverage["complete"] is True
    # Contrast: the same archive named nowhere is absent, and that absence is not called complete.
    (root / "Skyrim.ini").write_text("[General]\n", encoding="utf-8")
    _ini(main, bInvalidateOlderFiles="1")
    snap, names = _view(install)
    assert "Skyrim - Meshes1.bsa" not in names
    assert snap.archive_coverage["complete"] is False


def test_user_ini_precedence_and_explicit_override(install):
    install["edition"] = "VR"
    root, user = Path(install["root"]), Path(install["config_dir"])
    _ini(root / "Skyrim.ini", sResourceArchiveList="Root - A.bsa", sResourceArchiveList2="Root - B.bsa")
    _ini(user / "Skyrim.ini", sResourceArchiveList="User - A.bsa")
    _ini(user / "SkyrimVR.ini", sResourceArchiveList="VRIni - A.bsa")
    snap, names = _view(install)
    assert names[:2] == ["VRIni - A.bsa", "Root - B.bsa"]
    lists = snap.archive_coverage["lists"]
    assert lists["sResourceArchiveList"]["source"].endswith("SkyrimVR.ini")
    assert lists["sResourceArchiveList2"]["source"] == str(root / "Skyrim.ini")
    assert snap.archive_coverage["complete"] is False      # a Documents Skyrim.ini on VR, and a mixed result
    # An explicitly empty user value: the lower value is kept, and the engine's reading of it is marked unknown.
    _ini(user / "SkyrimVR.ini", sResourceArchiveList="")
    _ini(user / "Skyrim.ini")
    snap, names = _view(install)
    assert "Root - A.bsa" in names
    assert snap.archive_coverage["complete"] is False
    assert any("empty" in u for u in snap.archive_coverage["uncertain"])


@pytest.mark.parametrize("edition", ["SE", "VR"])
def test_only_the_editions_main_user_ini_is_applied(install, edition):
    """Review2 M1: a stale Documents Skyrim.ini on VR (whose reading by the VR engine is unknown), a SkyrimVR.ini on SE
    and SkyrimPrefs.ini (Wrye reads no archives from it) are fingerprinted, never applied, and make coverage
    incomplete. Before the fix the VR case dropped the base archives and still said complete."""
    install["edition"] = edition
    root, user = Path(install["root"]), Path(install["config_dir"])
    _ini(root / "Skyrim.ini", sResourceArchiveList="Skyrim - Meshes0.bsa, Skyrim - Meshes1.bsa",
         sResourceArchiveList2="Skyrim - Textures0.bsa")
    other = {"VR": "Skyrim.ini", "SE": "SkyrimVR.ini"}[edition]
    for stale in (other, "SkyrimPrefs.ini"):
        _ini(user / stale, sResourceArchiveList="Stale.bsa", sResourceArchiveList2="Stale2.bsa")
        snap, names = _view(install)
        assert "Stale.bsa" not in names and "Stale2.bsa" not in names
        assert names[:3] == ["Skyrim - Meshes0.bsa", "Skyrim - Meshes1.bsa", "Skyrim - Textures0.bsa"]
        assert snap.archive_coverage["complete"] is False
        assert any(stale in u and "not applied" in u for u in snap.archive_coverage["uncertain"])
        assert [s["applied"] for s in snap.archive_coverage["sources"] if s["path"] == str(user / stale)] == [False]
        _ini(user / stale)                               # the same INI naming no archive is harmless
        assert order.capture(install).archive_coverage["complete"] is True


def test_vr_supplemental_list_and_unqualified_vr_keys(install):
    install["edition"] = "VR"
    root, user = Path(install["root"]), Path(install["config_dir"])
    _ini(root / "Skyrim.ini", sResourceArchiveList="Skyrim - Meshes0.bsa")
    snap, names = _view(install)
    assert names[-1] == "Skyrim_VR - Main.bsa"           # engine fallback when the VR key loads nothing
    _ini(user / "SkyrimVR.ini", sVrResourceArchiveList="Custom_VR.bsa")
    snap, names = _view(install)
    assert "Custom_VR.bsa" in names and "Skyrim_VR - Main.bsa" not in names
    assert snap.archive_coverage["complete"] is True
    _ini(user / "SkyrimVR.ini", sVrResourceArchiveList="Custom_VR.bsa", sVrResourceArchiveList2="Other_VR.bsa")
    snap, names = _view(install)
    assert "Other_VR.bsa" not in names and snap.archive_coverage["complete"] is False
    # SE has no supplemental list: nothing is folded in.
    install["edition"] = "SE"
    _snap, names = _view(install)
    assert "Custom_VR.bsa" not in names and "Skyrim_VR - Main.bsa" not in names


def test_unqualified_ini_sources_are_marked_never_applied(install):
    root, user, data = Path(install["root"]), Path(install["config_dir"]), Path(install["data_dir"])
    _ini(root / "Skyrim.ini", sResourceArchiveList="Skyrim - Meshes0.bsa")
    assert order.capture(install).archive_coverage["complete"] is True
    _ini(user / "SkyrimCustom.ini", sResourceArchiveList2="Custom.bsa")
    snap, names = _view(install)
    assert "Custom.bsa" not in names and snap.archive_coverage["complete"] is False
    (user / "SkyrimCustom.ini").write_text("[Display]\nfGamma=1\n", encoding="utf-8")
    assert order.capture(install).archive_coverage["complete"] is True
    _ini(data / "Sword.ini", sResourceArchiveList2="PluginIni.bsa")
    snap, names = _view(install)
    assert "PluginIni.bsa" not in names and snap.archive_coverage["complete"] is False
    assert any("Sword.ini" in u for u in snap.archive_coverage["uncertain"])


def test_mo2_profile_inis_and_game_root_list(install):
    root = Path(install["root"])
    mods = root / "mods"
    (mods / "High").mkdir(parents=True)
    profile = root / "profiles/Default"             # apart from the game root, as in a real MO2 instance
    profile.mkdir(parents=True)
    (profile / "plugins.txt").write_bytes(Path(install["plugins_txt"]).read_bytes())
    install["plugins_txt"] = str(profile / "plugins.txt")
    (profile / "modlist.txt").write_text("+High\n")
    (profile / "loadorder.txt").write_text("Skyrim.esm\nLateMaster.esp\nSword.esp\nDisabled.esp\n")
    install.update(asset_provider="mo2", authority="mo2-profile", mods_dir=str(mods), config_dir=str(profile))
    _ini(root / "Skyrim.ini", sResourceArchiveList="Skyrim - Meshes1.bsa", sResourceArchiveList2="Root2.bsa")
    _ini(profile / "Skyrim.ini", sResourceArchiveList="Profile1.bsa", sResourceArchiveList2="Profile2.bsa")
    (profile / "settings.ini").write_text("[General]\nLocalSettings=true\n", encoding="utf-8")
    snap, names = _view(install)
    assert names == ["Profile1.bsa", "Profile2.bsa"]
    assert snap.archive_coverage["complete"] is True
    _ini(profile / "Skyrim.ini", sResourceArchiveList2="Profile2.bsa")     # a profile key over the root's: mixed
    snap, names = _view(install)
    assert names == ["Skyrim - Meshes1.bsa", "Profile2.bsa"]
    assert snap.archive_coverage["complete"] is False
    # A profile without local INIs means the game reads Documents INIs this integration never captured.
    (profile / "settings.ini").write_text("[General]\nLocalSettings=false\n", encoding="utf-8")
    snap = order.capture(install)
    assert snap.archive_coverage["complete"] is False
    assert any("LocalSettings" in u for u in snap.archive_coverage["uncertain"])
    # A plugin INI in the winning MO2 layer is seen as well.
    (profile / "settings.ini").write_text("[General]\nLocalSettings=true\n", encoding="utf-8")
    _ini(mods / "High/Sword.ini", sResourceArchiveList2="X.bsa")
    assert order.capture(install).archive_coverage["complete"] is False


@pytest.mark.parametrize("change", ["root-edit", "root-create", "custom-create", "plugin-ini", "vr-ini",
                                    "documents-skyrim-ini", "prefs-ini"])
def test_every_archive_input_is_fingerprinted(install, change):
    install["edition"] = "VR"
    root, user, data = Path(install["root"]), Path(install["config_dir"]), Path(install["data_dir"])
    if change != "root-create":
        _ini(root / "Skyrim.ini", sResourceArchiveList="A.bsa")
    before = order.capture(install).fingerprint
    target = {"root-edit": root / "Skyrim.ini", "root-create": root / "Skyrim.ini",
              "custom-create": user / "SkyrimCustom.ini", "plugin-ini": data / "Sword.ini",
              "vr-ini": user / "SkyrimVR.ini", "documents-skyrim-ini": user / "Skyrim.ini",
              "prefs-ini": user / "SkyrimPrefs.ini"}[change]
    _ini(target, sResourceArchiveList="B.bsa")
    assert order.capture(install).fingerprint != before


def test_mo2_settings_ini_is_fingerprinted(install):
    mods = Path(install["root"]) / "mods"
    mods.mkdir()
    profile = Path(install["plugins_txt"]).parent
    (profile / "modlist.txt").write_text("")
    (profile / "loadorder.txt").write_text("Skyrim.esm\nLateMaster.esp\nSword.esp\n")
    install.update(asset_provider="mo2", authority="mo2-profile", mods_dir=str(mods), config_dir=str(profile))
    before = order.capture(install).fingerprint
    (profile / "settings.ini").write_text("[General]\nLocalSettings=true\n", encoding="utf-8")
    assert order.capture(install).fingerprint != before


def _fake_read_backend(monkeypatch, during=None):
    class Result:
        def model_dump(self, **_kwargs):
            return {"content": [{"type": "text", "text": json.dumps({"exists": False, "read_incomplete": False})}]}

    class FakeBackend:
        def __init__(self, config, snapshot, work=None):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return False

        async def call(self, _name, _args):
            if during:
                during()
            return Result()

    monkeypatch.setattr(gateway, "Backend", FakeBackend)


def test_asset_answers_carry_machine_readable_archive_coverage(install, monkeypatch):
    root = Path(install["root"])
    _fake_read_backend(monkeypatch)
    args = {"paths": ["meshes/weapons/shiv/shiv.nif"]}
    obj = asyncio.run(gateway.Gateway(install).call("housecarl_asset_status", args))
    assert obj["absence_conclusive"] is False
    assert obj["context"]["archive_coverage"]["complete"] is False
    assert obj["context"]["archive_coverage"]["uncertain"]
    _ini(root / "Skyrim.ini", sResourceArchiveList="Skyrim - Meshes1.bsa")
    obj = asyncio.run(gateway.Gateway(install).call("housecarl_asset_status", args))
    assert obj["absence_conclusive"] is True
    assert obj["context"]["archive_coverage"]["lists"]["sResourceArchiveList"]["value"] == "Skyrim - Meshes1.bsa"


def test_a_root_ini_changed_during_a_read_rejects_the_answer(install, monkeypatch):
    root = Path(install["root"])
    _ini(root / "Skyrim.ini", sResourceArchiveList="A.bsa")
    _fake_read_backend(monkeypatch, during=lambda: _ini(root / "Skyrim.ini", sResourceArchiveList="B.bsa"))
    with pytest.raises(Refusal, match="Inputs changed"):
        asyncio.run(gateway.Gateway(install).call("housecarl_asset_status", {"paths": ["a.nif"]}))


def test_a_backend_set_up_for_another_version_is_named_not_reported_as_the_pin(tmp_path):
    """A toolkit update moves the pin, not the installed backend: doctor would have named the new version over the old files."""
    from housecarl_integration import UPSTREAM_VERSION, runtime
    for recorded in ({}, {"upstream_version": "2.0.5"}):
        with pytest.raises(Refusal, match="Rerun tools/housecarl.py setup"):
            runtime.verify_installation({"server": str(tmp_path / "housecarl-mcp.exe"), "server_hashes": {}, **recorded})
    with pytest.raises(Refusal, match="Incomplete installed-backend"):      # the current pin goes on to the next check
        runtime.verify_installation({"server": str(tmp_path / "housecarl-mcp.exe"), "server_hashes": {},
                                     "upstream_version": UPSTREAM_VERSION})


# ---- issue 20: @file FormID lists on write tools follow House Carl's own grammar ------------------------------

_ARTIFACT = ('{"housecarl_artifact":1,"tool":"housecarl_records","identity":"formid","epoch":"e1","row_count":3}\n'
             '{"formid":"012EB7:Skyrim.esm","name":"a"}\n{"error":"no such record","input":"FFFFFF:Skyrim.esm"}\n'
             '\n{"formid":"000800:Sword.esp"}\n')


@pytest.mark.parametrize("name,content,expected", [
    ("ids.txt", "012EB7:Skyrim.esm\r\n000800:Sword.esp\n\n", ["012EB7:Skyrim.esm", "000800:Sword.esp"]),
    ("ids.csv", "012EB7:Skyrim.esm, 000800:Sword.esp", ["012EB7:Skyrim.esm", "000800:Sword.esp"]),
    ("ids.json", '["012EB7:Skyrim.esm", "000800:Sword.esp"]', ["012EB7:Skyrim.esm", "000800:Sword.esp"]),
    ("result.jsonl", _ARTIFACT, ["012EB7:Skyrim.esm", "000800:Sword.esp"]),
    ("bom.txt", "\ufeff012EB7:Skyrim.esm\n", ["012EB7:Skyrim.esm"]),
])
@pytest.mark.parametrize("spelling", ["list", "string"])
def test_formid_files_are_frozen_and_name_the_edit_set(install, name, content, expected, spelling):
    source = Path(install["root"]) / name
    source.write_bytes(content.encode("utf-8"))
    token = "@" + str(source)
    args = gateway.check_arguments("housecarl_forward", {"formids": [token] if spelling == "list" else token}, install)
    frozen = Path(args["formids"][0][1:])
    assert len(args["formids"]) == 1 and args["formids"][0].startswith("@")
    assert frozen.parent == Path(install["state_dir"]) / "inputs" and frozen.read_bytes() == source.read_bytes()
    assert args[gateway.FROZEN_FORMIDS] == expected
    assert gateway.allowed_records(args) == {t.casefold().lstrip("0") for t in expected}
    source.write_text("FFFFFF:Skyrim.esm\n")            # a later edit of the caller's file cannot reach the transaction
    assert frozen.read_bytes() == content.encode("utf-8")


@pytest.mark.parametrize("content,match", [
    ("", "is empty"),
    ('{"housecarl_artifact":1,"identity":"path"}\n{"path":"meshes/a.nif"}\n', "'path' identities"),
    ('{"housecarl_artifact":1,"identity":null}\n{"count":3}\n', "None identities"),
    ('{"housecarl_artifact":1,"identity":"formid"}\n{"error":"x"}\n', "no record rows"),
    ('{"housecarl_artifact":1,"identity":"formid"}\nnot json\n', "line 2 is not a JSON row"),
    ('{"housecarl_artifact":1,"identity":"formid"}\n{"name":"x"}\n', "carries no 'formid' value"),
])
def test_unusable_formid_files_get_a_gateway_refusal(install, content, match):
    source = Path(install["root"]) / "bad.jsonl"
    source.write_text(content, encoding="utf-8")
    with pytest.raises(Refusal, match=match) as caught:
        gateway.check_arguments("housecarl_forward", {"formids": ["@" + str(source)]}, install)
    assert "Accepted list files" in str(caught.value)


def test_formid_file_spelling_errors_are_refusals(install):
    source = Path(install["root"]) / "ids.txt"
    source.write_text("012EB7:Skyrim.esm\n")
    for formids, match in ((["@" + str(source), "000800:Sword.esp"], "mixes an '@file' entry"),
                           (["@" + str(source), "@" + str(source)], "mixes an '@file' entry"),
                           (["@ids.txt"], "absolute path"), (["@"], "names no file"),
                           (["@" + str(source) + ".missing"], "could not be read")):
        with pytest.raises(Refusal, match=match):
            gateway.check_arguments("housecarl_remove", {"formids": formids}, install)


@pytest.mark.parametrize("key", ["ops", "assignments", "records"])
def test_non_json_manifest_files_are_refused_by_name_not_as_a_decode_error(install, key):
    source = Path(install["root"]) / "ops.txt"
    source.write_text("012EB7:Skyrim.esm\n")
    with pytest.raises(Refusal, match=key + r"=@.* must be a JSON document"):
        gateway.check_arguments("housecarl_apply", {key: "@" + str(source)}, install)


def test_reads_pass_formid_files_through_untouched(install):
    args = gateway.check_arguments("housecarl_records", {"formids": ["@C:/anywhere/ids.txt"]}, install)
    assert args["formids"] == ["@C:/anywhere/ids.txt"] and gateway.FROZEN_FORMIDS not in args


def test_the_backend_receives_the_frozen_file_not_the_gateway_key(install, monkeypatch):
    from types import SimpleNamespace
    source = Path(install["root"]) / "result.jsonl"
    source.write_text(_ARTIFACT, encoding="utf-8")
    seen = []

    class FakeBackend:
        def __init__(self, config, snapshot, work):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return False

        async def call(self, name, args):
            seen.append((name, args))
            return SimpleNamespace(content=[SimpleNamespace(type="text", text=json.dumps({"ok": True}))])

    monkeypatch.setattr(gateway, "Backend", FakeBackend)
    out = asyncio.run(gateway.Gateway(install).call("housecarl_forward", {
        "patch": "Fwd.esp", "source": "Sword.esp", "formids": ["@" + str(source)], "dry_run": True}))
    assert out["dry_run"] is True
    (name, args), = seen
    assert gateway.FROZEN_FORMIDS not in args
    frozen = Path(args["formids"][0][1:])
    assert frozen.parent == Path(install["state_dir"]) / "inputs" and frozen.read_text(encoding="utf-8") == _ARTIFACT


# Install repair 2026-10-29 (762f1773), ported: a forward INTO an already-active plugin must not enable a writable
# Target copy over its own read provider. The backend creates and owns a private working folder; the detached copy is
# imported there and the forward goes through the ordinary `into` lane.
def _detached_forward_backend(seen, placeholder_body=b"", meta_extra=""):
    from types import SimpleNamespace

    class FakeBackend:
        def __init__(self, config, snapshot, work):
            self.work = work

        async def __aenter__(self):
            (self.work / "profile").mkdir(parents=True, exist_ok=True)
            (self.work / "profile/modlist.txt").write_text("+Other\n", encoding="utf-8")
            return self

        async def __aexit__(self, *_exc):
            return False

        async def call(self, name, args):
            seen.append((name, dict(args)))
            if name == "housecarl_create_plugin":
                folder = self.work / "mods" / ("Owned " + args["patch"])
                folder.mkdir(parents=True)
                body = placeholder_body
                (folder / (args["patch"] + ".esp")).write_bytes(
                    b"TES4" + struct.pack("<II", len(body), 0) + b"\0" * 12 + body)
                (folder / "meta.ini").write_text("[General]\ngenerated=true\nplugin=%s.esp\n%s" % (args["patch"], meta_extra),
                                                 encoding="utf-8")
                return SimpleNamespace(content=[SimpleNamespace(type="text", text="created")])
            return SimpleNamespace(content=[SimpleNamespace(type="text", text=json.dumps({"ok": True}))])
    return FakeBackend


def test_a_forward_into_an_active_plugin_writes_through_a_private_folder(install, monkeypatch):
    seen = []
    monkeypatch.setattr(gateway, "Backend", _detached_forward_backend(seen))
    monkeypatch.setattr(gateway, "independent_check", lambda *a, **k: {})
    out = asyncio.run(gateway.Gateway(install).call("housecarl_forward", {
        "into": "Sword.esp", "source": "LateMaster.esp", "formids": ["000800:LateMaster.esp"], "dry_run": True}))
    assert out["dry_run"] is True
    (init, init_args), (fwd, fwd_args) = seen
    assert init == "housecarl_create_plugin" and init_args["patch"].startswith("ToolkitTransaction")
    assert fwd == "housecarl_forward" and fwd_args["into"] == "Owned " + init_args["patch"]
    assert "in_place" not in fwd_args and "acknowledge" not in fwd_args
    work = next((Path(install["state_dir"]) / "transactions").iterdir())
    assert (work / "mods" / fwd_args["into"] / "Sword.esp").is_file()                     # the detached copy, imported
    assert "plugin=Sword.esp" in (work / "mods" / fwd_args["into"] / "meta.ini").read_text(encoding="utf-8")
    assert "+Target" not in (work / "profile/modlist.txt").read_text(encoding="utf-8")   # never a read provider


def test_other_writes_into_an_active_plugin_keep_the_target_lane(install, monkeypatch):
    seen = []
    monkeypatch.setattr(gateway, "Backend", _detached_forward_backend(seen))
    monkeypatch.setattr(gateway, "independent_check", lambda *a, **k: {})
    asyncio.run(gateway.Gateway(install).call("housecarl_apply", {
        "in_place": "Sword.esp", "ops": [{"formid": "000800:Sword.esp", "set": {}}], "dry_run": True}))
    (name, args), = seen
    assert name == "housecarl_apply" and args["in_place"] == "Sword.esp"
    work = next((Path(install["state_dir"]) / "transactions").iterdir())
    assert (work / "profile/modlist.txt").read_text(encoding="utf-8").startswith("+Target\n")


@pytest.mark.parametrize("body, meta, why", [
    (b"MAST" + struct.pack("<H", 11) + b"Skyrim.esm\0", "", "not empty"),          # the initializer has a master
    (b"", "plugin=ToolkitTransactionX.esp\n", None),     # a line naming another plugin is not this one's: no effect
])
def test_an_unexpected_private_folder_publishes_nothing(install, monkeypatch, body, meta, why):
    seen = []
    monkeypatch.setattr(gateway, "Backend", _detached_forward_backend(seen, body, meta))
    monkeypatch.setattr(gateway, "independent_check", lambda *a, **k: {})
    run = lambda: asyncio.run(gateway.Gateway(install).call("housecarl_forward", {
        "into": "Sword.esp", "source": "LateMaster.esp", "formids": ["000800:LateMaster.esp"], "dry_run": True}))
    if why:
        with pytest.raises(Refusal, match=why):
            run()
        assert len(seen) == 1                                    # the forward itself never ran
    else:
        run()                                                    # an unrelated extra metadata line changes nothing
        assert [n for n, _ in seen] == ["housecarl_create_plugin", "housecarl_forward"]


@pytest.mark.parametrize("meta", [
    "[General]\nplugin={p}.esp\n",                                   # generated=true gone
    "[General]\ngenerated=true\nmasterplugin={p}.esp\n",              # only a longer key contains plugin=
    "[General]\nnotgenerated=true\nplugin={p}.esp\n",                 # only a longer key contains generated=true
], ids=["generated-missing", "plugin-only-as-substring", "generated-only-as-substring"])
def test_a_private_folder_without_ownership_metadata_publishes_nothing(install, monkeypatch, meta):
    seen = []
    backend = _detached_forward_backend(seen)
    original_call = backend.call

    async def call(self, name, args):
        out = await original_call(self, name, args)
        if name == "housecarl_create_plugin":
            target = next((self.work / "mods").rglob("meta.ini"))
            target.write_text(meta.format(p=args["patch"]), encoding="utf-8")
        return out
    backend.call = call
    monkeypatch.setattr(gateway, "Backend", backend)
    monkeypatch.setattr(gateway, "independent_check", lambda *a, **k: {})
    with pytest.raises(Refusal, match="ownership metadata"):
        asyncio.run(gateway.Gateway(install).call("housecarl_forward", {
            "into": "Sword.esp", "source": "LateMaster.esp", "formids": ["000800:LateMaster.esp"], "dry_run": True}))
    assert len(seen) == 1


# Install repair b1c79507 (2026-10-28), the retention half, ported: an edit of an existing plugin that loses a master or
# changes the ESM/ESL flags is never published.
@pytest.mark.parametrize("flags, masters", [(0x200, ["Skyrim.esm"]), (0, []), (1, ["Skyrim.esm"])],
                         ids=["esl-flag-added", "master-dropped", "esm-flag-added"])
def test_an_edit_that_changes_the_header_is_never_published(install, monkeypatch, flags, masters):
    from types import SimpleNamespace
    target = Path(install["data_dir"]) / "Sword.esp"
    original = target.read_bytes()

    class FakeBackend:
        def __init__(self, config, snapshot, work):
            self.work = work

        async def __aenter__(self):
            (self.work / "profile").mkdir()
            (self.work / "profile/modlist.txt").write_text("")
            return self

        async def __aexit__(self, *_exc):
            return False

        async def call(self, _name, _args):
            plugin(self.work / "mods/Target/Sword.esp", flags=flags, masters=masters)
            return SimpleNamespace(content=[SimpleNamespace(type="text", text=json.dumps({"ok": True, "verify_ran": True}))])

    monkeypatch.setattr(gateway, "Backend", FakeBackend)
    monkeypatch.setattr(gateway, "independent_check", lambda *a, **k: {"records": {}, "errors": [], "complete": True})
    # A restore that reports success but leaves the master out: the check after it still refuses.
    monkeypatch.setattr(gateway, "retain_header", lambda *a, **k: {"complete": True})
    with pytest.raises(Refusal, match="header flags"):
        asyncio.run(gateway.Gateway(install).call("housecarl_apply", {
            "in_place": "Sword.esp", "ops": [{"formid": "000800:Sword.esp", "field_path": "Name", "value": "new"}]}))
    assert target.read_bytes() == original


def _master_dropping_backend():
    from types import SimpleNamespace

    class FakeBackend:
        def __init__(self, config, snapshot, work):
            self.work = work

        async def __aenter__(self):
            (self.work / "profile").mkdir()
            (self.work / "profile/modlist.txt").write_text("")
            return self

        async def __aexit__(self, *_exc):
            return False

        async def call(self, _name, _args):
            plugin(self.work / "mods/Target/Sword.esp", flags=0, masters=[])   # the lean collector dropped Skyrim.esm
            return SimpleNamespace(content=[SimpleNamespace(type="text", text=json.dumps({"ok": True, "verify_ran": True}))])
    return FakeBackend


def _fake_preserve(seen, restore=True, ok=True):
    """Stands in for node + tools/housecarl-preserve.js; records what the linked view held while it ran."""
    def run(argv, **_kw):
        from types import SimpleNamespace
        assert Path(argv[1]).name == "housecarl-preserve.js"
        spec = json.loads(Path(argv[2]).read_text(encoding="utf-8"))
        data = Path(spec["game"]) / "Data"
        seen.append({"data": data, "links": os.stat(data / "Skyrim.esm").st_nlink,
                     "detached": os.stat(data / "Sword.esp").st_nlink})
        if not ok:
            return SimpleNamespace(stdout="", stderr="Error: Header readback mismatch")
        if restore:
            plugin(Path(spec["candidate"]), flags=0, masters=["Skyrim.esm"])
        Path(argv[3]).write_text(json.dumps({"complete": True, "added_masters": ["Skyrim.esm"]}), encoding="utf-8")
        return SimpleNamespace(stdout="RESULT: OK", stderr="")
    return run


def test_a_dropped_master_is_restored_before_publication(install, monkeypatch, tmp_path):
    seen = []
    (tmp_path / "temp").mkdir()
    monkeypatch.setattr(gateway.tempfile, "tempdir", str(tmp_path / "temp"))   # never the real %TEMP%
    monkeypatch.setattr(gateway, "Backend", _master_dropping_backend())
    monkeypatch.setattr(gateway, "independent_check", lambda *a, **k: {"records": {}, "errors": [], "complete": True})
    monkeypatch.setattr(gateway.subprocess, "run", _fake_preserve(seen))
    asyncio.run(gateway.Gateway(install).call("housecarl_apply", {
        "in_place": "Sword.esp", "ops": [{"formid": "000800:Sword.esp", "field_path": "Name", "value": "new"}]}))
    target = Path(install["data_dir"]) / "Sword.esp"
    assert order.plugin_header(target)["masters"] == ["Skyrim.esm"]
    assert seen[0]["links"] >= 2 and seen[0]["detached"] == 1      # masters linked, the candidate a detached copy
    assert not seen[0]["data"].parent.exists()                     # the linked view is gone after the call
    receipts = list((Path(install["state_dir"]) / "transactions").glob("*/receipt.json"))
    assert json.loads(receipts[0].read_text(encoding="utf-8"))["retained_header"]["added_masters"] == ["Skyrim.esm"]


def test_a_failed_restore_publishes_nothing_and_removes_its_links(install, monkeypatch, tmp_path):
    seen = []
    (tmp_path / "temp").mkdir()
    monkeypatch.setattr(gateway.tempfile, "tempdir", str(tmp_path / "temp"))   # never the real %TEMP%
    target = Path(install["data_dir"]) / "Sword.esp"
    original = target.read_bytes()
    monkeypatch.setattr(gateway, "Backend", _master_dropping_backend())
    monkeypatch.setattr(gateway, "independent_check", lambda *a, **k: {"records": {}, "errors": [], "complete": True})
    monkeypatch.setattr(gateway.subprocess, "run", _fake_preserve(seen, ok=False))
    with pytest.raises(Refusal, match="Restoring the original's masters failed"):
        asyncio.run(gateway.Gateway(install).call("housecarl_apply", {
            "in_place": "Sword.esp", "ops": [{"formid": "000800:Sword.esp", "field_path": "Name", "value": "new"}]}))
    assert target.read_bytes() == original
    assert not seen[0]["data"].exists()                            # links removed even though the step failed


def test_restore_refuses_a_flag_change_before_running_xedit(install, monkeypatch, tmp_path):
    candidate = tmp_path / "cand" / "Sword.esp"
    candidate.parent.mkdir()
    plugin(candidate, flags=0x200, masters=[])
    (tmp_path / "temp").mkdir()
    monkeypatch.setattr(gateway.tempfile, "tempdir", str(tmp_path / "temp"))   # a mutant must not reach real %TEMP%
    monkeypatch.setattr(gateway.subprocess, "run", lambda *a, **k: pytest.fail("xEdit must not run"))
    with pytest.raises(Refusal, match="changed the original's header flags"):
        gateway.retain_header(install, candidate, order.capture(install), Path(install["data_dir"]) / "Sword.esp")


def test_verifier_controls_on_synthetic_plugins(tmp_path):
    import shutil
    import subprocess
    if not shutil.which("node"):
        pytest.skip("Node not installed")
    here = Path(__file__).resolve().parent
    result = subprocess.run(["node", str(here / "housecarl-verify-controls.js"), str(here.parent / "tools"), str(tmp_path)],
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr + result.stdout
    assert "Passed 12 verifier controls" in result.stdout


# --- Review 2026-11-08: header version/ONAM, link-removal ordering, timeouts -------------------------------------

def _rewriting_backend(writer):
    """A fake backend whose single call writes the candidate with `writer(path)`."""
    from types import SimpleNamespace

    class FakeBackend:
        def __init__(self, config, snapshot, work):
            self.work = work

        async def __aenter__(self):
            (self.work / "profile").mkdir()
            (self.work / "profile/modlist.txt").write_text("")
            return self

        async def __aexit__(self, *_exc):
            return False

        async def call(self, _name, _args):
            writer(self.work / "mods/Target/Sword.esp")
            return SimpleNamespace(content=[SimpleNamespace(type="text", text=json.dumps({"ok": True, "verify_ran": True}))])
    return FakeBackend


def _apply(install):
    asyncio.run(gateway.Gateway(install).call("housecarl_apply", {
        "in_place": "Sword.esp", "ops": [{"formid": "000800:Sword.esp", "field_path": "Name", "value": "new"}]}))


SWORD_RECORDS = [(b"WEAP", 0x00012EB7), (b"WEAP", 0x01000800)]   # an override of Skyrim.esm, and its own record


@pytest.mark.parametrize("case", ["version-drift-unrestored", "onam-dropped-record-kept"])
def test_an_edit_that_loses_header_version_or_onam_is_never_published(install, monkeypatch, case):
    target = Path(install["data_dir"]) / "Sword.esp"
    plugin(target, 1, ["Skyrim.esm"], version=1.7, onam=[0x00012EB7], records=SWORD_RECORDS)
    original = target.read_bytes()
    def writer(path):
        if case == "version-drift-unrestored":
            plugin(path, 1, ["Skyrim.esm"], version=1.71, onam=[0x00012EB7], records=SWORD_RECORDS)
        else:
            plugin(path, 1, ["Skyrim.esm"], version=1.7, onam=[], records=SWORD_RECORDS)
    monkeypatch.setattr(gateway, "Backend", _rewriting_backend(writer))
    monkeypatch.setattr(gateway, "independent_check", lambda *a, **k: {"records": {}, "errors": [], "complete": True})
    monkeypatch.setattr(gateway, "retain_header", lambda *a, **k: {"complete": True})   # a restore that fixes nothing
    with pytest.raises(Refusal, match="header version" if case.startswith("version") else "ONAM"):
        _apply(install)
    assert target.read_bytes() == original


def test_an_onam_entry_may_go_with_its_removed_record(install, monkeypatch):
    target = Path(install["data_dir"]) / "Sword.esp"
    plugin(target, 1, ["Skyrim.esm"], version=1.7, onam=[0x00012EB7], records=SWORD_RECORDS)
    monkeypatch.setattr(gateway, "Backend", _rewriting_backend(
        lambda path: plugin(path, 1, ["Skyrim.esm"], version=1.7, onam=[], records=SWORD_RECORDS[1:])))
    monkeypatch.setattr(gateway, "independent_check", lambda *a, **k: {"records": {}, "errors": [], "complete": True})
    _apply(install)
    assert gateway.header_fields(target)["onam"] == set()


@pytest.mark.parametrize("candidate_version, triggered", [(1.69, True), (1.7, False), (1.71, "refused")], ids=["lowered", "same", "raised"])
def test_a_version_only_drift_triggers_the_restore(install, monkeypatch, tmp_path, candidate_version, triggered):
    from types import SimpleNamespace
    original = Path(install["data_dir"]) / "Sword.esp"
    plugin(original, 0, ["Skyrim.esm"], version=1.7)
    candidate = tmp_path / "cand" / "Sword.esp"
    candidate.parent.mkdir()
    plugin(candidate, 0, ["Skyrim.esm"], version=candidate_version)   # every master present
    (tmp_path / "temp").mkdir()
    monkeypatch.setattr(gateway.tempfile, "tempdir", str(tmp_path / "temp"))
    calls = []
    def step(work, masters, cand, run):
        calls.append(cand)
        return SimpleNamespace(stdout="", stderr="authored: xEdit not run in this test")
    monkeypatch.setattr(gateway, "_with_linked_masters", step)
    if triggered == "refused":
        with pytest.raises(Refusal, match="raised the plugin's header version"):
            gateway.retain_header(install, candidate, order.capture(install), original)
        assert calls == [], "a raised version is refused before any xEdit step"
    elif triggered:
        with pytest.raises(Refusal, match="Restoring the original's masters failed"):
            gateway.retain_header(install, candidate, order.capture(install), original)
        assert calls == [candidate]
    else:
        assert gateway.retain_header(install, candidate, order.capture(install), original) is None
        assert calls == []


def test_a_failed_link_removal_still_compares_every_master_first(tmp_path, monkeypatch):
    master = tmp_path / "masters" / "Skyrim.esm"
    master.parent.mkdir()
    plugin(master, 1)
    candidate = tmp_path / "cand" / "Sword.esp"
    candidate.parent.mkdir()
    plugin(candidate, 0, ["Skyrim.esm"])
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.setattr(gateway.shutil, "rmtree", lambda *a, **k: None)   # e.g. antivirus holding the links
    monkeypatch.setattr(gateway.time, "sleep", lambda s: None)
    def step():
        os.utime(master, ns=(0, 10 ** 15))                                # the master changed during the step
        raise RuntimeError("authored step failure")
    with pytest.raises(Refusal) as caught:
        gateway._with_linked_masters(work, {"Skyrim.esm": master}, candidate, step)
    assert "Skyrim.esm changed" in str(caught.value) and "could not remove its links" in str(caught.value)
    assert isinstance(caught.value.__context__, RuntimeError), "the step's own error stays attached"
    for link in (work / "Data").iterdir():                                # test cleanup of the deliberately kept view
        link.unlink()


def test_an_xedit_timeout_is_a_refusal(tmp_path, monkeypatch):
    import subprocess
    def slow(*a, **k):
        raise subprocess.TimeoutExpired(cmd="node", timeout=120)
    monkeypatch.setattr(gateway.subprocess, "run", slow)
    with pytest.raises(Refusal, match="did not finish within 120 s"):
        gateway._node(["node"], {}, tmp_path, "independent xEdit check")


def test_a_master_that_vanishes_during_the_step_is_reported_as_changed(tmp_path, monkeypatch):
    master = tmp_path / "masters" / "Skyrim.esm"
    master.parent.mkdir()
    plugin(master, 1)
    candidate = tmp_path / "cand" / "Sword.esp"
    candidate.parent.mkdir()
    plugin(candidate, 0, ["Skyrim.esm"])
    work = tmp_path / "work"
    work.mkdir()
    with pytest.raises(Refusal, match=r"Skyrim\.esm \(missing or unreadable\) changed"):
        gateway._with_linked_masters(work, {"Skyrim.esm": master}, candidate, lambda: master.unlink())


def test_record_keys_refuses_an_out_of_range_master_index(tmp_path):
    path = tmp_path / "Bad.esp"
    plugin(path, 0, ["Skyrim.esm"], records=[(b"WEAP", 0x05000800)])
    with pytest.raises(Refusal, match="out-of-range master index"):
        gateway.record_keys(path)
