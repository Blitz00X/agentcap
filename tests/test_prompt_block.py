"""A reached quota stops the conversation, not only the tool calls (0.5.2).

``PreToolUse`` denies tool calls and ends the turn, but the user could type another
prompt and the model would answer in text, spending quota.  Both plugins now check
``UserPromptSubmit`` against the same reading ``PreToolUse`` uses; ``/agentcap:``
commands (Claude Code) and typed ``agentcap ...`` commands (Codex) always pass,
because they are how the user raises or clears the limit.
"""

from __future__ import annotations

import importlib.util
import io
import json
from pathlib import Path

import pytest

from agentcap.quota import Reading, parse_agent_set, parse_limit_words, parse_quota

ROOT = Path(__file__).resolve().parent.parent


def _load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


cc = _load("agentcap_cc")
cx = _load("agentcap_codex")

CC_SID = "4c2b1a00-1111-2222-3333-444455556666"
CX_SID = "01a1da0b-e835-7cf0-9dcd-5365269e5129"
RESETS = 2_000_000_000.0
R5 = 2_000_000_000.0
RW = 2_000_500_000.0


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTCAP_QUOTA_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    monkeypatch.setenv("AGENTCAP_LICENSE_FILE", str(tmp_path / "license.json"))
    monkeypatch.setattr(cx, "LIVE_READER", lambda: {"5h": Reading(10.0, R5), "week": Reading(40.0, RW)})
    monkeypatch.setattr(cx, "SPAWN", lambda argv, **kw: None)
    return tmp_path


def pro(tmp_path):
    (tmp_path / "license.json").write_text(json.dumps(
        {"source": "polar", "valid": True, "tier": "pro", "key": "X", "checked_at": 1, "reason": "ok"}))


# ------------------------------------------------------------------ Claude Code
def cc_hook(event, payload):
    out = io.StringIO()
    cc.main(["hook", event], stdin=io.StringIO(json.dumps(payload)), stdout=out)
    text = out.getvalue().strip()
    return json.loads(text) if text else None


def cc_prompt(text):
    return cc_hook("prompt", {"session_id": CC_SID, "cwd": "/w", "hook_event_name": "UserPromptSubmit",
                              "prompt": text})


def cc_statusline(week, five=10.0):
    payload = {"hook_event_name": "Status", "session_id": CC_SID, "cwd": "/w",
               "rate_limits": {"five_hour": {"used_percentage": five, "resets_at": RESETS - 3600},
                               "seven_day": {"used_percentage": week, "resets_at": RESETS}}}
    cc.main(["statusline"], stdin=io.StringIO(json.dumps(payload)), stdout=io.StringIO())


def cc_cmd(*args):
    out = io.StringIO()
    cc.main(list(args) + ["--session", CC_SID], stdout=out)
    return out.getvalue()


def cc_at_limit(tmp_path):
    """Pro, weekly quota 5 points from 40%, now at 45%: exactly at the stop point."""

    pro(tmp_path)
    cc_statusline(40.0)
    cc_cmd("quota", "5%")
    cc_statusline(45.0)


def test_claude_blocks_a_new_prompt_at_the_limit(env):
    cc_at_limit(env)
    out = cc_prompt("carry on with the task")
    assert out["decision"] == "block"
    assert out["reason"].startswith("agentcap: limit reached — weekly quota at 45%, stop point 45%.")
    assert "/agentcap:approve (+50%)" in out["reason"]
    assert "/agentcap:quota <N%> 5h" in out["reason"] and "/agentcap:off" in out["reason"]


def test_claude_slash_commands_pass_a_reached_limit(env):
    cc_at_limit(env)
    for command in ("/agentcap:off", "/agentcap:approve", "/agentcap:quota 5%", "/agentcap:status"):
        assert cc_prompt(command) is None


def test_claude_under_the_limit_passes(env):
    pro(env)
    cc_statusline(40.0)
    cc_cmd("quota", "10%")
    cc_statusline(44.0)
    assert cc_prompt("carry on") is None


def test_claude_free_tier_never_blocks_a_prompt(env):
    cc_statusline(40.0)                      # no licence file: Free
    cc_cmd("quota", "2%")
    cc_statusline(45.0)
    assert cc_prompt("carry on") is None


def test_claude_without_a_quota_passes(env):
    pro(env)
    cc_statusline(45.0)
    assert cc_prompt("carry on") is None


# ------------------------------------------------------------------ Codex
def cx_hook(event, payload):
    out = io.StringIO()
    cx.main(["hook", event], stdin=io.StringIO(json.dumps(payload)), stdout=out)
    text = out.getvalue().strip()
    return json.loads(text) if text else None


def cx_prompt(text):
    return cx_hook("UserPromptSubmit", {"session_id": CX_SID, "turn_id": "t", "cwd": "/w", "transcript_path": None,
                                         "hook_event_name": "UserPromptSubmit", "model": "gpt-6-astra",
                                         "permission_mode": "default", "prompt": text})


def cx_at_limit(tmp_path):
    """Pro, 2 points of the 5-hour quota from 10%, the server now reports 13%."""

    pro(tmp_path)
    assert cx_prompt("agentcap 2% 5h")["decision"] == "block"
    cx._atomic_write(cx.live_path(CX_SID), {"ts": 9e12, "readings": {"5h": {"used": 13.0, "resets_at": R5}}})


def test_codex_blocks_a_new_prompt_at_the_limit(env):
    cx_at_limit(env)
    out = cx_prompt("carry on with the task")
    assert out["decision"] == "block"
    assert "agentcap: limit reached — 5-hour quota at 13%, stop point 12%" in out["reason"]
    assert "`agentcap approve` (+50%)" in out["reason"]
    assert "`agentcap <N%> 5h`" in out["reason"] and "`agentcap off`" in out["reason"]


def test_codex_typed_commands_still_work_over_the_limit(env):
    cx_at_limit(env)
    assert "agentcap commands" in cx_prompt("agentcap help")["reason"]
    assert "stops at 12%" in cx_prompt("agentcap status")["reason"]
    cleared = cx_prompt("agentcap off")
    assert cleared["decision"] == "block" and "cleared" in cleared["reason"]
    assert cx_prompt("carry on") is None      # the limit is gone


def test_codex_under_the_limit_passes(env):
    pro(env)
    cx_prompt("agentcap 5% 5h")
    cx._atomic_write(cx.live_path(CX_SID), {"ts": 9e12, "readings": {"5h": {"used": 13.0, "resets_at": R5}}})
    assert cx_prompt("carry on") is None


def test_codex_free_tier_never_blocks_a_prompt(env):
    cx_prompt("agentcap 2% 5h")
    cx._atomic_write(cx.live_path(CX_SID), {"ts": 9e12, "readings": {"5h": {"used": 13.0, "resets_at": R5}}})
    assert cx_prompt("carry on") is None


def test_codex_without_a_quota_passes(env):
    pro(env)
    assert cx_prompt("carry on") is None


# ------------------------------------------------------------------ %1 / until %70
def test_a_leading_percent_is_the_same_limit():
    assert parse_quota("%1") == parse_quota("1%") == 1.0
    assert parse_quota(" %50 ") == 50.0
    assert parse_limit_words(["%1", "5h"]) == parse_limit_words(["1%", "5h"]) == (1.0, "5h", False)
    assert parse_limit_words(["until", "%70", "week"]) == \
        parse_limit_words(["until", "70%", "week"]) == (70.0, "week", True)
    assert parse_limit_words(["%50", "week"]) == parse_limit_words(["50%", "week"]) == (50.0, "week", False)
    assert parse_agent_set("echo agentcap set %50 5h") == parse_agent_set("echo agentcap set 50% 5h")


def test_leading_percent_works_in_the_commands(env):
    pro(env)
    cc_statusline(40.0)
    assert "5-hour" in cc_cmd("quota", "%2", "5h")
    assert json.loads(cc.session_path(CC_SID).read_text())["quota"] == 2.0
    assert "stops at 70% of the weekly quota" in cc_cmd("quota", "until", "%70", "week")

    assert "2 more points of the 5-hour quota" in cx_prompt("agentcap %2 5h")["reason"]
    assert "stops at 70% of the weekly quota" in cx_prompt("agentcap until %70 week")["reason"]


def test_a_bad_limit_message_is_whole(env):
    with pytest.raises(ValueError) as excinfo:
        parse_limit_words(["lots", "5h"])
    assert str(excinfo.value) == "a limit looks like 50% or until 70% (got 'lots')"
    assert "a limit looks like 50% or until 70% (got 'lots')" in cc_cmd("quota", "lots")
    assert "a limit looks like 50% or until 70% (got 'lots')" in cx_prompt("agentcap lots")["reason"]


# ------------------------------------------------------------------ wiring
def test_claude_hooks_json_registers_the_prompt_hook():
    hooks = json.loads((ROOT / "hooks" / "hooks.json").read_text())["hooks"]
    command = hooks["UserPromptSubmit"][0]["hooks"][0]["command"]
    assert command.endswith('agentcap_cc.py" hook prompt')
