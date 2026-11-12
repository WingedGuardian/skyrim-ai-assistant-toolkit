"""Legacy backup names retain a complete pair in the neutral store."""
import hashlib
import json
from pathlib import Path
import sys

import pytest

from conftest import REPO
sys.path.insert(0, str(REPO / "tools"))
from modding_harness import approval, contracts, dispatch, profiles, workspace
from modding_harness.preservation import SaveStore
from skyguard import portable, save_profile, save_store


def _qualify(monkeypatch, script):
    """Model the reviewed source: add the fixture's hash to the qualified set. Since 783c5b0 every check reads
    QUALIFIED_POLICY_SHA256 (built once at import), so patching TRANSFER_POLICY_SHA256 alone reaches nothing
    (CI run 39145224156)."""
    monkeypatch.setattr(local_policy, "QUALIFIED_POLICY_SHA256",
                        local_policy.QUALIFIED_POLICY_SHA256 | {hashlib.sha256(script.read_bytes()).hexdigest()})


@pytest.mark.parametrize("suffix", [".bak", ".BAK", ".old.1", ".snapshot-1", ".bäckup"])
def test_backup_suffix_stays_attached_to_both_logical_members(tmp_path, suffix):
    ess, skse = tmp_path / ("Source.ess" + suffix), tmp_path / ("Source.skse" + suffix)
    ess.write_bytes(b"ess")
    skse.write_bytes(b"skse")
    profile = save_profile.skyrim()
    assert profile.members(ess) == (ess, skse)
    assert profile.members(skse) == (ess, skse)
    with pytest.raises(ValueError):
        profiles.skyrim().members(ess)


def test_regular_save_membership_is_unchanged_and_unrelated_suffixes_stay_separate(tmp_path):
    ess = tmp_path / "Source.ess"
    ess.write_bytes(b"ess")
    assert save_profile.skyrim().members(ess) == profiles.skyrim().members(ess)
    backup = tmp_path / "Source.ess.bak"
    backup.write_bytes(b"backup")
    other = tmp_path / "Source.skse.other"
    other.write_bytes(b"different backup")
    assert save_profile.skyrim().members(backup) == (backup, tmp_path / "Source.skse.bak")
    with pytest.raises(ValueError):
        save_profile.skyrim().members(tmp_path / "unrelated.txt")

