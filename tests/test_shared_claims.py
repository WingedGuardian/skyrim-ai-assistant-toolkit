"""Shared ownership claims: the release rule, the owner's view, and the ledger bytes other tools read."""
import datetime
import json
import os
import re
import subprocess
import sys

import pytest

from conftest import REPO
sys.path.insert(0, str(REPO / "tools"))
import shared_claims as sc  # noqa: E402

SHARED, LIVE = sc.SLOTS
T0 = datetime.datetime(2026, 11, 15, 6, 0, tzinfo=datetime.timezone.utc)


def at(minutes):
    return T0 + datetime.timedelta(minutes=minutes)


def dead(pid, start):
    return False


def living(pid, start):
    return True


def unreadable(pid, start):
    raise sc.Unknown("access denied")


def nothing_running():
    return []


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    path = tmp_path / "work-status.json"
    path.write_bytes(b'{\n "updated": "2026-11-12T18:56:22-05:00",\n "active_shared_config_owner": null,\n'
                     b' "active_live_test_owner": null,\n "work": [\n  {"id": "S1"}\n ]\n}\n')
    monkeypatch.setenv("SHARED_CLAIMS_LEDGER", str(path))
    monkeypatch.delenv("SHARED_CLAIMS_LOCK", raising=False)
    return path


def session_claim(owner="gateway", minutes=0):
    return sc.claim(SHARED, owner, "install", now=at(minutes))


def verdict_at(minutes, alive=dead, games=nothing_running):
    return {row["slot"]: row for row in sc.status(now=at(minutes), alive=alive, games=games)}[SHARED]


# --------------------------------------------------------------------------- the release rule

def test_free_slot_is_free(ledger):
    assert verdict_at(0)["verdict"] == "free"


def test_a_quiet_session_claim_is_releasable_only_after_thirty_minutes(ledger):
    session_claim()
    assert verdict_at(29)["verdict"] == "live"
    assert verdict_at(31)["verdict"] == "quiet-releasable"


def test_a_running_holder_process_keeps_the_claim_live_however_old(ledger):
    sc.claim(SHARED, "gateway", "install", pid=os.getpid(), now=at(0))
    assert verdict_at(600, alive=living)["verdict"] == "live"
    assert verdict_at(600, alive=dead)["verdict"] == "quiet-releasable"


def test_a_running_game_keeps_the_claim_live(ledger):
    session_claim()
    assert verdict_at(600, games=lambda: ["skyrimvr.exe"])["verdict"] == "live"


def test_an_unreadable_process_is_unknown_never_releasable(ledger):
    sc.claim(SHARED, "gateway", "install", pid=os.getpid(), now=at(0))
    assert verdict_at(600, alive=unreadable)["verdict"] == "unknown"


def test_an_unreadable_process_list_is_unknown_never_releasable(ledger):
    session_claim()

    def failing():
        raise sc.Unknown("tasklist failed")
    assert verdict_at(600, games=failing)["verdict"] == "unknown"


def test_a_name_only_claim_is_legacy_and_never_released_by_age(ledger):
    value = json.loads(ledger.read_text())
    value[SHARED] = "gateway-uat-cutover"
    ledger.write_text(json.dumps(value, indent=1))
    assert verdict_at(10_000)["verdict"] == "legacy-ask-owner"
    with pytest.raises(sc.Refused, match="legacy-ask-owner"):
        sc.release_stale(SHARED, "delirium", now=at(10_000), alive=dead, games=nothing_running)


def test_details_for_a_different_owner_do_not_count(ledger):
    session_claim("gateway")
    value = json.loads(ledger.read_text())
    value[SHARED] = "someone-else"          # an older tool overwrote the slot name only
    ledger.write_text(json.dumps(value, indent=1))
    assert verdict_at(600)["verdict"] == "legacy-ask-owner"


def test_touch_restarts_the_quiet_period(ledger):
    session_claim()
    sc.touch("gateway", now=at(25))
    assert verdict_at(50)["verdict"] == "live"
    assert verdict_at(56)["verdict"] == "quiet-releasable"


# --------------------------------------------------------------------------- release-stale and the owner's view

def test_release_stale_refuses_a_live_claim(ledger):
    session_claim()
    with pytest.raises(sc.Refused, match="live"):
        sc.release_stale(SHARED, "delirium", now=at(10), alive=dead, games=nothing_running)
    assert json.loads(ledger.read_text())[SHARED] == "gateway"


def test_release_stale_is_logged_and_the_old_owner_is_told(ledger):
    session_claim()
    sc.release_stale(SHARED, "delirium", now=at(45), alive=dead, games=nothing_running)
    value = json.loads(ledger.read_text())
    assert value[SHARED] is None and sc.key(SHARED) not in value[sc.DETAILS]
    assert value[sc.RELEASES][sc.key(SHARED)]["prior_owner"] == "gateway"
    log = [json.loads(line) for line in open(str(ledger) + ".claims-log.jsonl", encoding="utf-8")]
    assert log[-1]["event"] == "release-stale" and log[-1]["by"] == "delirium"
    with pytest.raises(sc.Refused, match="released by delirium"):
        sc.check(SHARED, "gateway")
    with pytest.raises(sc.Refused, match="released by delirium"):
        sc.touch("gateway", now=at(46))


def test_release_stale_needs_a_name(ledger):
    session_claim()
    with pytest.raises(sc.Refused):
        sc.release_stale(SHARED, "", now=at(45), alive=dead, games=nothing_running)


# --------------------------------------------------------------------------- claiming and releasing

def test_a_foreign_holder_refuses_the_claim(ledger):
    session_claim("gateway")
    with pytest.raises(sc.Refused, match="held by 'gateway'"):
        sc.claim(SHARED, "delirium", "run", now=at(1))


def test_also_free_refuses_when_the_other_slot_is_foreign(ledger):
    sc.claim(LIVE, "souls", "run", now=at(0))
    with pytest.raises(sc.Refused, match="active_live_test_owner"):
        sc.claim(SHARED, "gateway", "install", also_free=(LIVE,), now=at(1))


def test_reclaiming_your_own_slot_refreshes_activity_and_keeps_the_claim_time(ledger):
    first = session_claim(minutes=0)
    again = session_claim(minutes=20)
    assert again["claimed_at"] == first["claimed_at"] and again["last_activity"] == sc.stamp(at(20))


def test_a_claim_for_a_process_that_is_not_running_refuses(ledger):
    child = subprocess.run([sys.executable, "-c", "import os; print(os.getpid())"], capture_output=True, text=True)
    with pytest.raises(sc.Refused, match="not running"):
        sc.claim(SHARED, "gateway", "install", pid=int(child.stdout), now=at(0))


def test_a_process_claim_records_the_real_start_and_is_alive(ledger):
    row = sc.claim(SHARED, "gateway", "install", pid=os.getpid(), now=at(0))
    assert row["kind"] == "process" and row["pid_start"]
    assert sc.process_alive(os.getpid(), row["pid_start"]) is True


def test_release_by_the_owner_and_refusal_for_anyone_else(ledger):
    session_claim("gateway")
    with pytest.raises(sc.Refused):
        sc.release(SHARED, "delirium")
    assert sc.release(SHARED, "gateway")["released"] is True
    assert sc.release(SHARED, "gateway")["already_free"] is True
    assert json.loads(ledger.read_text())[SHARED] is None


def test_claim_and_release_stamp_the_file_level_updated_field(ledger):
    session_claim(minutes=0)
    assert json.loads(ledger.read_text())["updated"] != "2026-11-12T18:56:22-05:00"


# --------------------------------------------------------------------------- bytes other tools depend on

@pytest.mark.parametrize("bom,newline", [(b"", b"\n"), (b"", b"\r\n"), (b"\xef\xbb\xbf", b"\r\n")])
def test_the_bom_and_newline_style_are_kept(ledger, bom, newline):
    ledger.write_bytes(bom + ledger.read_bytes().replace(b"\n", newline))
    session_claim()
    raw = ledger.read_bytes()
    assert raw.startswith(bom) if bom else not raw.startswith(b"\xef\xbb\xbf")
    lines = raw.count(b"\n")
    assert raw.count(b"\r\n") == (lines if newline == b"\r\n" else 0)


def test_other_content_survives_and_slot_needles_stay_unique(ledger):
    # gateway change_slots edits the file by replacing exactly one '"<slot>": <value>' needle.
    sc.claim(SHARED, "gateway", "install", now=at(0))
    raw = ledger.read_bytes()
    assert json.loads(raw)["work"] == [{"id": "S1"}]
    for slot, value in ((SHARED, '"gateway"'), (LIVE, "null")):
        assert raw.count(('"' + slot + '": ' + value).encode()) == 1
        assert len(re.findall(('"' + slot + '"').encode(), raw)) == 1


def test_the_lock_is_the_cooperating_writers_lock(ledger):
    assert sc.lock_path(ledger) == ledger.with_suffix(".gateway.lock")


def test_a_held_lock_refuses_after_its_timeout(ledger, monkeypatch):
    monkeypatch.setattr(sc, "LOCK_TIMEOUT", 0.3)
    with sc._Lock(sc.lock_path(ledger), timeout=5):
        code = ("import sys; sys.path.insert(0, sys.argv[1]); import shared_claims as sc; sc.LOCK_TIMEOUT = 0.3\n"
                "try:\n sc.claim(sc.SLOTS[0], 'other', 'x')\nexcept sc.Refused as e:\n print('REFUSED', e)")
        child = subprocess.run([sys.executable, "-c", code, str(REPO / "tools")], capture_output=True, text=True,
                               env=dict(os.environ), timeout=60)
    assert "REFUSED" in child.stdout and "lock is busy" in child.stdout, child.stdout + child.stderr


def test_cli_exit_codes(ledger, capsys):
    assert sc.main(["claim", SHARED, "--owner", "gateway", "--reason", "x", "--session-claim"]) == 0
    assert sc.main(["claim", SHARED, "--owner", "delirium", "--reason", "x", "--session-claim"]) == 3
    assert json.loads(capsys.readouterr().out.strip().splitlines()[-1])["refused"]
    assert sc.main(["status"]) == 0


# --------------------------------------------------------------------------- review fixes (2026-11-15)

def test_a_foreign_rewrite_makes_details_untrusted_until_the_owner_touches(ledger):
    # gateway change_slots releases and re-claims by editing only the slot and `updated`: old details must not count.
    session_claim("gateway", minutes=0)
    value = json.loads(ledger.read_text())
    value["updated"] = "2026-11-15T03:00:00-05:00"
    ledger.write_text(json.dumps(value, indent=1))
    assert verdict_at(120)["verdict"] == "legacy-ask-owner"
    with pytest.raises(sc.Refused, match="legacy-ask-owner"):
        sc.release_stale(SHARED, "delirium", now=at(120), alive=dead, games=nothing_running)
    sc.touch("gateway", now=at(121))
    assert verdict_at(125)["verdict"] == "live"
    assert verdict_at(152)["verdict"] == "quiet-releasable"


def test_a_released_slot_keeps_no_details(ledger):
    session_claim("gateway")
    sc.release(SHARED, "gateway")
    assert sc.key(SHARED) not in json.loads(ledger.read_text())[sc.DETAILS]


def test_a_new_ledger_has_both_slots_for_older_tools(tmp_path, monkeypatch):
    path = tmp_path / "fresh.json"
    monkeypatch.setenv("SHARED_CLAIMS_LEDGER", str(path))
    sc.claim(SHARED, "gateway", "x", now=at(0))
    value = json.loads(path.read_text())
    assert value[LIVE] is None and value[SHARED] == "gateway" and value["updated"]


def test_a_new_claim_clears_the_old_release_notice(ledger):
    session_claim("gateway")
    sc.release_stale(SHARED, "delirium", now=at(45), alive=dead, games=nothing_running)
    session_claim("gateway", minutes=50)
    sc.release(SHARED, "gateway")
    with pytest.raises(sc.Refused) as refused:
        sc.check(SHARED, "gateway")
    assert "delirium" not in str(refused.value)


def test_owner_names_must_be_printable_ascii(ledger):
    for bad in ("", "gätewäy", "a\nb"):
        with pytest.raises(sc.Refused):
            sc.claim(SHARED, bad, "x", now=at(0))


def test_a_start_recorded_on_another_platform_is_unknown(monkeypatch):
    monkeypatch.setattr(sc, "process_start", lambda pid: "ticks:123")
    with pytest.raises(sc.Unknown):
        sc.process_alive(1, "2026-11-15T06:00:00+00:00")


def test_game_processes_are_unknown_off_windows(monkeypatch):
    monkeypatch.setattr(sc.sys, "platform", "linux")
    with pytest.raises(sc.Unknown):
        sc.running_games()


def test_the_conf_file_names_the_ledger(tmp_path, monkeypatch):
    monkeypatch.delenv("SHARED_CLAIMS_LEDGER", raising=False)
    conf = tmp_path / "shared-claims.conf"
    monkeypatch.setattr(sc, "CONF", conf)
    monkeypatch.setattr(sc, "TOOLKIT_ROOT", tmp_path)
    assert sc.resolve_ledger() == (sc.DEFAULT_LEDGER, False)
    conf.write_text("# machine-local\nledger = _CLAUDE_OUTPUT/work-status.json\n")
    assert sc.resolve_ledger() == (tmp_path / "_CLAUDE_OUTPUT/work-status.json", True)
    conf.write_text("nothing here\n")
    with pytest.raises(ValueError):
        sc.resolve_ledger()


def test_unreadable_detail_tables_are_errors_not_tracebacks(ledger, capsys):
    value = json.loads(ledger.read_text())
    value[sc.DETAILS] = ["not", "an", "object"]
    ledger.write_text(json.dumps(value))
    assert sc.main(["status"]) == 2
