"""Google Antigravity support: what its docs say it reads, kept true by tests (docs/antigravity.md)."""
import json
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import REPO

CUT = 24000      # antigravity.google/docs/rules: each instruction file is truncated at 24,000 bytes
CUT_HEADING = b"## Core Modding Principle: Vanilla Game as Frame of Reference"
MUST_ARRIVE = [b"## Safety Rules", b"### DENIED", b"### ASKS THE USER", b"### Saves:", b"### The file lock",
               b"### General rules", b"## Confidence Levels"]


def _load_install():
    import importlib.util
    spec = importlib.util.spec_from_file_location("ag_install", REPO / "tools/adapters/antigravity/install.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_everything_that_protects_the_install_is_inside_antigravitys_cut():
    agents = (REPO / "AGENTS.md").read_bytes()
    cut_at = agents.find(CUT_HEADING)
    assert 0 < cut_at < CUT, "the section .agents/rules/antigravity.md names as the cut moved past 24,000 bytes"
    for heading in MUST_ARRIVE:
        at = agents.find(heading)
        assert 0 < at < cut_at, heading
    rules = (REPO / ".agents/rules/antigravity.md").read_text(encoding="utf-8")
    assert rules.startswith("---\ntrigger: always_on\n---\n")        # without a valid trigger it is discarded
    assert CUT_HEADING.decode()[len("## "):] in rules.replace("\n  ", " ")


def test_install_merges_and_never_overwrites_an_unreadable_file(tmp_path):
    m = _load_install()
    (tmp_path / ".agents").mkdir()
    hooks = tmp_path / ".agents/hooks.json"
    hooks.write_text(json.dumps({"mine": {"Stop": []}}), encoding="utf-8")
    _, merged = m.plan(tmp_path, "C:/Py/python.exe")
    assert merged["mine"] == {"Stop": []}
    hook = merged[m.ENTRY]["PreToolUse"][0]
    assert hook["matcher"] == "^(run_command|write_to_file|replace_file_content|multi_replace_file_content)$"
    assert hook["hooks"][0]["timeout"] > 25 and " " not in hook["hooks"][0]["command"].split(" -B ")[0]
    hooks.write_text("{not json", encoding="utf-8")
    with pytest.raises(ValueError):
        m.plan(tmp_path, "C:/Py/python.exe")


def test_the_adapter_passes_conformance():
    out = subprocess.run([sys.executable, str(REPO / "tools/adapter_conformance.py"), "run",
                          "--adapter-dir", str(REPO / "tools/adapters/antigravity"),
                          "--profile", str(REPO / "tools/adapters/antigravity/profile.json"),
                          "--cases", str(REPO / "tools/adapters/antigravity/cases.json")],
                         capture_output=True, text=True, timeout=900,
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    assert "CONFORMANCE PASS" in out.stdout, out.stdout[-3000:] + out.stderr[-2000:]
