"""postmortem: one report from the last session's Papyrus log, crash dump and SKSE log; each part can go red."""
import importlib.util
import os
import sys
import time

from conftest import REPO

sys.path.insert(0, str(REPO / "tools"))
spec = importlib.util.spec_from_file_location("postmortem", REPO / "tools" / "postmortem.py")
pm = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pm)

PAPYRUS = "\n".join([
    "[11/07/2026 - 08:52:43PM] Papyrus log opened (PC)",
    "[11/07/2026 - 08:52:44PM] error: Failed to find variable maxHP used in a_Script.Stage_1()",
    "stack:",
    "\t[a_Quest (0F001234)].a_Script.Stage_1() - \"a_Script.psc\" Line ?",
    "[11/07/2026 - 08:52:45PM] error: Failed to find variable maxHP used in a_Script.Stage_1()",
    "[11/07/2026 - 08:53:00PM] warning: Replacing native function GetModName on unlinked object Game.",
    "",
])
CRASH = "\n".join([
    "Skyrim VR v1.4.15", "CrashLoggerSSE v1-0-0", "",
    'Unhandled exception "EXCEPTION_ACCESS_VIOLATION" at 0x7FF6A10B84F05 SkyrimVR.exe+0B84F05\tmov rax, [rcx] |  x)',
    "", "PROBABLE CALL STACK:", "\t[0]\t0x7FF6A10B84F05 SkyrimVR.exe+0B84F05", "", "REGISTERS:", "\tRAX 0x0", ""])
SKSE = "\n".join([
    "checking plugin C:\\g\\Data\\SKSE\\Plugins\\Good.dll",
    "plugin C:\\g\\Data\\SKSE\\Plugins\\Good.dll (00000001 Good 00000102) loaded correctly (handle 1)",
    "checking plugin C:\\g\\Data\\SKSE\\Plugins\\SEOnly.dll",
    "plugin C:\\g\\Data\\SKSE\\Plugins\\SEOnly.dll (00000001 SE Only 00000003) reported as incompatible during query (handle 2)",
    ""])


def folders(tmp_path, crash_age_s=None):
    papyrus, skse = tmp_path / "Logs" / "Script", tmp_path / "SKSE"
    papyrus.mkdir(parents=True)
    skse.mkdir()
    (papyrus / "Papyrus.0.log").write_text(PAPYRUS, encoding="utf-8")
    (skse / "sksevr.log").write_text(SKSE, encoding="utf-8")
    if crash_age_s is not None:
        dump = skse / "crash-2026-11-07-20-53-10.log"
        dump.write_text(CRASH, encoding="utf-8")
        when = time.mktime((2026, 11, 7, 20, 53, 10, 0, 0, -1)) - crash_age_s
        os.utime(dump, (when, when))
    return papyrus, skse


def test_every_part_of_the_report(tmp_path, monkeypatch):
    papyrus, skse = folders(tmp_path, crash_age_s=0)
    monkeypatch.setenv("SKSE_LOG_DIR", str(skse))
    out = pm.report(papyrus, skse, top=5)
    assert out["session"]["opened"] == "2026-11-07 20:52:43" and out["session"]["last_entry"] == "2026-11-07 20:53:00"
    assert out["crash"]["this_session"] is True
    assert out["crash"]["signature"] == "SkyrimVR.exe+0B84F05"
    assert out["crash"]["known"] != "UNKNOWN"                                    # the KB ruling on +0B84F05 is shown
    assert out["papyrus"]["error"]["count"] == 2 and out["papyrus"]["error"]["top"][0]["count"] == 2
    assert out["papyrus"]["warning"]["count"] == 1
    assert [r["dll"] for r in out["skse"]["not_loaded"]] == ["SEOnly.dll"]


def test_a_dump_from_before_the_session_is_marked_older(tmp_path, monkeypatch):
    papyrus, skse = folders(tmp_path, crash_age_s=3 * 86400)
    monkeypatch.setenv("SKSE_LOG_DIR", str(skse))
    assert pm.report(papyrus, skse, top=5)["crash"]["this_session"] is False


def test_no_dump_and_no_papyrus_log(tmp_path, monkeypatch, capsys):
    papyrus, skse = folders(tmp_path)
    monkeypatch.setenv("SKSE_LOG_DIR", str(skse))
    assert pm.report(papyrus, skse, top=5)["crash"] is None
    (papyrus / "Papyrus.0.log").unlink()
    monkeypatch.setenv("PAPYRUS_LOG_DIR", str(papyrus))
    monkeypatch.setenv("SKSE_CRASH_DIR", str(skse))
    assert pm.main([]) == 2
    assert "COULD NOT REPORT" in capsys.readouterr().err
