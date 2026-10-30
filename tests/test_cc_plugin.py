"""Claude Code plugin (scripts/agentcap_cc.py) with realistic hook/statusline JSON."""
from __future__ import annotations

import importlib.util
import io
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "agentcap_cc.py"
spec = importlib.util.spec_from_file_location("agentcap_cc", SCRIPT)
cc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cc)

SID = "6f1d2c3b-aaaa-bbbb-cccc-0123456789ab"


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTCAP_QUOTA_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    monkeypatch.setenv("AGENTCAP_LICENSE_FILE", str(tmp_path / "license.json"))
    return tmp_path


def pro(tmp_path):
    (tmp_path / "license.json").write_text(json.dumps(
        {"source": "polar", "valid": True, "tier": "pro", "key": "X", "checked_at": 1, "reason": "ok"}))


def sl_payload(week, five=10.0, resets=2_000_000_000):
    return {
        "hook_event_name": "Status", "session_id": SID, "cwd": "/w",
        "model": {"id": "claude-opus-4-1", "display_name": "Opus"},
        "rate_limits": {"five_hour": {"used_percentage": five, "resets_at": resets - 3600},
                        "seven_day": {"used_percentage": week, "resets_at": resets}},
    }


def statusline(payload):
    out = io.StringIO()
    cc.main(["statusline"], stdin=io.StringIO(json.dumps(payload)), stdout=out)
    return out.getvalue()


def hook(event, payload):
    out = io.StringIO()
    cc.main(["hook", event], stdin=io.StringIO(json.dumps(payload)), stdout=out)
    text = out.getvalue().strip()
    return json.loads(text) if text else None


PRE = {"session_id": SID, "transcript_path": "/t.jsonl", "cwd": "/w", "permission_mode": "default",
       "hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {"command": "pytest"}, "tool_use_id": "t1"}


def cmd(*args):
    out = io.StringIO()
    cc.main(list(args) + ["--session", SID], stdout=out)
    return out.getvalue()


def test_pro_denies_at_limit_with_exact_baseline(env):
    pro(env)
    statusline(sl_payload(40.0))
    assert "exact" in cmd("quota", "5%")
    assert hook("pre", dict(PRE, tool_input={"command": "step1"})) is None
    statusline(sl_payload(44.2))
    warn = hook("pre", dict(PRE, tool_input={"command": "step2"}))
    assert "4.2%" in warn["systemMessage"] and warn["hookSpecificOutput"]["additionalContext"]
    assert hook("pre", dict(PRE, tool_input={"command": "step3"})) is None  # warn once
    line = statusline(sl_payload(45.1))
    assert "session 5.1% / 5%" in line
    deny = hook("pre", dict(PRE, tool_input={"command": "step4"}))
    assert deny["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "/agentcap:off" in deny["hookSpecificOutput"]["permissionDecisionReason"]


def test_free_only_nudges_once(env):
    statusline(sl_payload(10.0))
    assert "Pro" in cmd("quota", "2")
    statusline(sl_payload(12.5))
    msg = hook("pre", dict(PRE, tool_input={"command": "step5"}))
    assert "hookSpecificOutput" not in msg and "Pro would stop" in msg["systemMessage"]
    assert hook("pre", dict(PRE, tool_input={"command": "step6"})) is None


def test_5h_window_and_reset_carry(env):
    pro(env)
    statusline(sl_payload(10.0, five=90.0))
    cmd("quota", "5%", "5h")
    statusline(sl_payload(10.0, five=93.0))
    # 5h window resets: used drops, share carries 3%
    statusline(sl_payload(10.0, five=2.5, resets=2_000_020_000))
    st = json.loads(cmd("status") and cc.session_path(SID).read_text())
    assert st["share"] == pytest.approx(5.5)
    assert hook("pre", PRE)["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_no_quota_or_no_readings_is_silent(env):
    pro(env)
    assert hook("pre", PRE) is None
    cmd("quota", "5%")  # no reading yet
    assert hook("pre", PRE) is None
    assert "waiting" in cmd("status")


def test_off_clears(env):
    pro(env)
    statusline(sl_payload(1.0)); cmd("quota", "1%"); statusline(sl_payload(3.0))
    cmd("off")
    assert hook("pre", PRE) is None


def test_stop_receipt(env):
    pro(env)
    statusline(sl_payload(1.0)); cmd("quota", "5%"); statusline(sl_payload(2.0))
    stop = {"session_id": SID, "hook_event_name": "Stop", "stop_hook_active": False, "cwd": "/w"}
    assert "1.0% / 5%" in hook("stop", stop)["systemMessage"]
    assert hook("stop", stop) is None
    rec = (cc.cc_dir() / "receipts.jsonl").read_text().splitlines()
    assert json.loads(rec[0])["share"] == 1.0


def test_session_fallback_from_sessionstart(env, monkeypatch):
    hook("start", {"session_id": SID, "hook_event_name": "SessionStart", "source": "startup", "cwd": str(env)})
    monkeypatch.chdir(env)
    out = io.StringIO()
    cc.main(["quota", "3%", "--session", "${CLAUDE_SESSION_ID}"], stdout=out)
    assert json.loads(cc.session_path(SID).read_text())["quota"] == 3


def test_setup_chains_and_unsetup_restores(env):
    settings = env / "claude" / "settings.json"
    settings.parent.mkdir()
    prev = {"type": "command", "command": "echo MYLINE"}
    settings.write_text(json.dumps({"model": "opus", "statusLine": prev}))
    o = io.StringIO()
    cc.main(["setup"], stdout=o)
    assert "previous statusLine is still shown" in o.getvalue()
    data = json.loads(settings.read_text())
    assert cc.MARK in data["statusLine"]["command"] and data["model"] == "opus"
    # launcher runs end to end and keeps the user's line
    r = subprocess.run(data["statusLine"]["command"], shell=True, input=json.dumps(sl_payload(5.0)),
                       capture_output=True, text=True, env={**__import__("os").environ})
    assert "MYLINE" in r.stdout
    cc.main(["unsetup"], stdout=io.StringIO())
    assert json.loads(settings.read_text())["statusLine"] == prev


def test_hook_garbage_fails_open(env):
    out = io.StringIO()
    assert cc.main(["hook", "pre"], stdin=io.StringIO("not json"), stdout=out) == 0
    assert out.getvalue() == ""


def test_manifests_valid_json():
    plugin = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text())
    market = json.loads((ROOT / ".claude-plugin" / "marketplace.json").read_text())
    assert plugin["name"] == market["plugins"][0]["name"] == "agentcap"
    hooks = json.loads((ROOT / "hooks" / "hooks.json").read_text())["hooks"]
    assert set(hooks) == {"SessionStart", "PreToolUse", "PostToolUse", "PostToolUseFailure", "Stop"}


# ------------------------------------------------------------- loop breaker
def _pre(cmdline="pytest", agent=None):
    p = dict(PRE, tool_input={"command": cmdline})
    if agent:
        p["agent_id"] = agent
    return hook("pre", p)


def test_loop_pro_denies_fourth_identical_call(env):
    pro(env)
    assert _pre() is None and _pre("ls") is None  # a different call resets the streak
    for _ in range(3):
        assert _pre() is None
    d = _pre()
    assert d["continue"] is False
    assert d["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "same tool call 4 times in a row (Bash)" in d["hookSpecificOutput"]["permissionDecisionReason"]
    assert "pytest" not in json.dumps(d)  # tool input never echoed


def test_loop_free_message_and_nudge_once(env):
    for _ in range(3):
        assert _pre() is None
    d = _pre()
    assert "hookSpecificOutput" not in d and "continue" not in d
    assert "Pro would stop" in d["systemMessage"]
    assert _pre() is None  # one nudge per streak


def test_loop_tracked_per_subagent(env):
    pro(env)
    for _ in range(3):
        assert _pre(agent="a") is None
    assert _pre(agent="b") is None


def test_error_streak_pro_stops_after_eight_failures(env):
    pro(env)
    fail = dict(PRE, hook_event_name="PostToolUseFailure", error="REDACTED")
    ok = dict(PRE, hook_event_name="PostToolUse", tool_response={"stdout": "REDACTED"})
    for _ in range(7):
        assert hook("postfail", fail) is None
    assert hook("post", ok) is None  # success resets
    for _ in range(7):
        assert hook("postfail", fail) is None
    d = hook("postfail", fail)
    assert d["continue"] is False and "8 failing tool calls in a row" in d["stopReason"]
    assert "REDACTED" not in json.dumps(d)


def test_error_streak_free_nudges(env):
    fail = dict(PRE, hook_event_name="PostToolUseFailure", error="x")
    for _ in range(7):
        hook("postfail", fail)
    d = hook("postfail", fail)
    assert "continue" not in d and "Pro would stop" in d["systemMessage"]


def test_hooks_json_registers_post_hooks():
    h = json.loads((Path(SCRIPT).parent.parent / "hooks" / "hooks.json").read_text())["hooks"]
    assert "PostToolUse" in h and "PostToolUseFailure" in h


def test_session_start_prunes_old_loop_state(env):
    import os as _os
    import time as _t
    loops = cc.cc_dir() / "loops"
    loops.mkdir(parents=True)
    old, fresh, other = loops / "old.json", loops / "fresh.json", loops / "keep.txt"
    for f in (old, fresh, other):
        f.write_text("{}")
    week = 8 * 86400
    _os.utime(old, (_t.time() - week, _t.time() - week))
    _os.utime(other, (_t.time() - week, _t.time() - week))
    hook("start", {"session_id": SID, "cwd": "/w", "hook_event_name": "SessionStart"})
    assert not old.exists() and fresh.exists() and other.exists()


# ------------------------------------------- 0.2.1: end turn, approve, last tool
def _at_limit(env):
    pro(env)
    statusline(sl_payload(40.0)); cmd("quota", "4%"); statusline(sl_payload(44.5))


def test_quota_deny_ends_the_turn(env):
    _at_limit(env)
    d = hook("pre", PRE)
    assert d["continue"] is False and d["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "/agentcap:approve" in d["stopReason"]


def test_approve_raises_by_half_twice(env):
    _at_limit(env)
    assert hook("pre", PRE)["continue"] is False
    out = cmd("approve")
    assert "6%" in out and "1/2" in out
    assert hook("pre", dict(PRE, tool_input={"command": "next"})) is None
    assert "9%" in cmd("approve")
    assert "already approved twice" in cmd("approve")
    assert cc.load(SID)["quota"] == 9.0


def test_approve_is_pro_only(env):
    statusline(sl_payload(40.0)); cmd("quota", "4%")
    assert "Pro" in cmd("approve")
    assert cc.load(SID)["quota"] == 4.0


def test_approve_resets_loop_streak(env):
    pro(env)
    for _ in range(4):
        d = _pre()
    assert d["continue"] is False
    statusline(sl_payload(1.0)); cmd("quota", "50%")
    cmd("approve")
    assert _pre() is None


def test_stop_receipt_has_last_tool(env):
    pro(env)
    statusline(sl_payload(1.0)); cmd("quota", "5%"); statusline(sl_payload(2.0))
    hook("pre", dict(PRE, tool_name="Edit", tool_input={"file_path": "/secret"}))
    hook("stop", {"session_id": SID, "hook_event_name": "Stop", "cwd": "/w"})
    rec = json.loads((cc.cc_dir() / "receipts.jsonl").read_text().splitlines()[-1])
    assert rec["last_tool"] == "Edit" and "/secret" not in json.dumps(rec)


def test_approve_command_file_exists():
    text = (ROOT / "commands" / "approve.md").read_text()
    assert "approve" in text and "disable-model-invocation: true" in text
