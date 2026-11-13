"""User-flow contract tests: five stages, four states, approve and tiers."""
from __future__ import annotations

import importlib.util
import json
import os
import time
from pathlib import Path

from agentcap import quota as q
from agentcap.license import features

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "agentcap_cc.py"
spec = importlib.util.spec_from_file_location("agentcap_cc_user_flow", SCRIPT)
cc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cc)


# ------------------------------------------------------------ four states
def test_protection_state_table():
    assert q.protection_state(has_reading=False, started_age_s=10)[1] == (
        "waiting for the first reading")
    assert q.protection_state(has_reading=False, started_age_s=120)[1] == (
        "data stale (protection not verified)")
    assert q.protection_state(has_reading=True, reading_age_s=5, enforce=True)[1] == (
        "protection active")
    assert q.protection_state(has_reading=True, reading_age_s=5, enforce=False)[1] == (
        "watch only (Pro stops at the limit)")
    assert q.protection_state(has_reading=True, reading_age_s=61, max_age_s=60)[1] == (
        "data stale (protection not verified)")
    assert q.protection_state(has_reading=True, stopped=True)[1] == "stopped"


def test_protection_state_never_claims_protection_without_reading_or_enforcement():
    state, text = q.protection_state(has_reading=False, started_age_s=0)
    assert state == q.STATE_STARTING
    assert "protection active" not in text
    state, text = q.protection_state(has_reading=True, reading_age_s=0, enforce=False)
    assert state == q.STATE_PROTECTED
    assert text == "watch only (Pro stops at the limit)"


# ------------------------------------------------------------ plugin status
def test_plugin_share_line_carries_the_state(monkeypatch):
    now = time.time()
    state = {"quota": 5.0, "window": "week", "used": 40.0, "share": 4.2,
             "readings_ts": now}
    monkeypatch.setattr(cc, "is_pro", lambda: True)
    line = cc.share_line(state)
    assert "session 4.2% / 5%" in line and "protection active" in line
    monkeypatch.setattr(cc, "is_pro", lambda: False)
    assert "watch only (Pro stops at the limit)" in cc.share_line(state)
    stale = dict(state, readings_ts=now - 120)
    assert "data stale (protection not verified)" in cc.share_line(stale)
    waiting = {"quota": 5.0, "window": "week", "used": None}
    assert "waiting for the first reading" in cc.share_line(waiting)


# ------------------------------------------------------------ approve
def _job(tmp_path: Path, *, enforce: bool = True, stopped: bool = False,
         quota: float = 5.0) -> Path:
    root = tmp_path / "state"
    job = root / "jobs" / "job1"
    job.mkdir(parents=True)
    (job / "job.json").write_text(json.dumps({
        "quota": quota, "window": "week", "enforce": enforce,
        "pid": os.getpid(), "started_at": time.time(), "stopped": stopped,
    }))
    return root


def test_approve_semantics_for_claude_and_codex(tmp_path):
    free = q.approve_job(_job(tmp_path, enforce=False))
    assert "observe-only (Free)" in free and "Pro feature" in free

    stopped = q.approve_job(_job(tmp_path / "stopped", stopped=True))
    assert "already stopped (SIGINT)" in stopped
    assert "Start it again with a higher --quota" in stopped

    live = q.approve_job(_job(tmp_path / "live"))
    assert "approved (1/2)" in live and "7.5%" in live and "run continues" in live


def test_approve_is_capped_at_two(tmp_path):
    root = _job(tmp_path)
    assert "approved (1/2)" in q.approve_job(root)
    assert "approved (2/2)" in q.approve_job(root)
    third = q.approve_job(root)
    assert "already approved twice" in third and "start a new job" in third


# ------------------------------------------------------------ tiers
def test_quota_mode_is_pro_only():
    assert "quota_mode" in features("pro")
    assert "quota_mode" not in features("free")


def test_user_flow_document_has_five_stages_and_state_texts():
    text = (ROOT / "docs" / "USER_FLOW_0.4.md").read_text(encoding="utf-8")
    for heading in ("## 1. Install", "## 2. Start", "## 3. Status",
                    "## 4. Stop", "## 5. Next step"):
        assert heading in text
    for phrase in ("waiting for the first reading", "protection active",
                   "watch only (Pro stops at the limit)",
                   "data stale (protection not verified)", "stopped"):
        assert phrase in text
