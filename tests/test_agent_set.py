"""The agent proposes a limit (`echo agentcap set ...`), the user approves it with one button.

Claude Code: PreToolUse answers "ask" (the permission prompt shows the stop point).
Codex: PreToolUse lets an escalated call through (Codex's approval prompt); a call
without escalation in the default mode is denied with the exact retry.
Both: the limit is set in PostToolUse (after the command ran), and the agent may only
set or tighten a limit, never raise or clear it.
"""

from __future__ import annotations

import importlib.util
import io
import json
import time
from pathlib import Path

import pytest

from agentcap.quota import Reading, agent_set_refusal, parse_agent_set

ROOT = Path(__file__).resolve().parent.parent


def _load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


cx = _load("agentcap_codex")
cc = _load("agentcap_cc")

SID = "0aa1da0b-e835-7cf0-9dcd-5365269e5129"
R5 = 2_000_000_000.0
RW = 2_000_500_000.0


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTCAP_QUOTA_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    monkeypatch.setenv("AGENTCAP_LICENSE_FILE", str(tmp_path / "license.json"))
    (tmp_path / "license.json").write_text(json.dumps(
        {"source": "polar", "valid": True, "tier": "pro", "key": "X", "checked_at": 1, "reason": "ok"}))
    monkeypatch.setattr(cx, "LIVE_READER", lambda: {"5h": Reading(10.0, R5), "week": Reading(40.0, RW)})
    monkeypatch.setattr(cx, "SPAWN", lambda argv, **kw: None)
    return tmp_path


# ------------------------------------------------------------------ shared parsing
@pytest.mark.parametrize("command, want", [
    ("echo agentcap set 50% 5h", (50.0, "5h", False)),
    ("echo 'agentcap set until 70% 5h'", (70.0, "5h", True)),
    ('  echo "agentcap set 5%"  ', (5.0, "week", False)),
    ("echo agentcap set until 30% week", (30.0, "week", True)),
])
def test_parse_agent_set(command, want):
    assert parse_agent_set(command) == want


@pytest.mark.parametrize("command", [
    "echo agentcap set 50% 5h; rm -rf /tmp/x", "echo agentcap set 50% 5h && agentcap off",
    "echo agentcap set $(cat x) 5h", "agentcap set 50% 5h", "echo agentcap off", "echo hello",
    "echo 'agentcap set 50% 5h", None,
])
def test_anything_else_is_not_the_agent_set_command(command):
    assert parse_agent_set(command) is None


def test_bad_arguments_are_an_error():
    with pytest.raises(ValueError):
        parse_agent_set("echo agentcap set off")
    with pytest.raises(ValueError):
        parse_agent_set("echo agentcap set 50% fortnight")


def test_refusal_is_tighten_only():
    none = {}
    assert agent_set_refusal(none, None, 0.0, 90.0, "5h", True) is None          # no limit yet: any
    cur = {"quota": 40.0, "window": "5h", "tracker": {"baseline": 10.0}}       # stops at 50%
    assert agent_set_refusal(cur, 10.0, 0.0, 40.0, "5h", True) is None          # until 40 < 50
    assert agent_set_refusal(cur, 10.0, 0.0, 40.0, "5h", False) is None         # 10+40 = 50
    assert "only lower" in agent_set_refusal(cur, 10.0, 0.0, 60.0, "5h", True)
    assert "only lower" in agent_set_refusal(cur, 20.0, 5.0, 40.0, "5h", False)  # 20+40 = 60 > 55
    assert "remove it" in agent_set_refusal(cur, 10.0, 0.0, 1.0, "week", False)
    unknown = {"quota": 5.0, "window": "5h"}
    assert agent_set_refusal(unknown, None, 0.0, 4.0, "5h", False) is None
    assert agent_set_refusal(unknown, None, 0.0, 6.0, "5h", False)
    assert "not known yet" in agent_set_refusal(unknown, None, 0.0, 30.0, "5h", True)


# ------------------------------------------------------------------ Codex
def cx_hook(event, payload):
    out = io.StringIO()
    cx.main(["hook", event], stdin=io.StringIO(json.dumps(payload)), stdout=out)
    text = out.getvalue().strip()
    return json.loads(text) if text else None


def cx_pre(command, *, escalated=False, mode="default"):
    tool_input = {"cmd": command}
    if escalated:
        tool_input.update({"sandbox_permissions": "require_escalated", "justification": "AgentCap: limit"})
    return cx_hook("PreToolUse", {"session_id": SID, "turn_id": "t", "cwd": "/w", "transcript_path": None,
                                  "hook_event_name": "PreToolUse", "permission_mode": mode,
                                  "tool_name": "exec_command", "tool_input": tool_input, "tool_use_id": "c1"})


def cx_post(command, response="Exit code: 0\nagentcap set 50% 5h"):
    return cx_hook("PostToolUse", {"session_id": SID, "turn_id": "t", "cwd": "/w", "transcript_path": None,
                                   "hook_event_name": "PostToolUse", "permission_mode": "default",
                                   "tool_name": "exec_command", "tool_input": {"cmd": command},
                                   "tool_response": response, "tool_use_id": "c1"})


def cx_prompt(text):
    return cx_hook("UserPromptSubmit", {"session_id": SID, "turn_id": "t", "cwd": "/w", "transcript_path": None,
                                        "hook_event_name": "UserPromptSubmit", "permission_mode": "default",
                                        "prompt": text})["reason"]


def deny_reason(out):
    hso = (out or {}).get("hookSpecificOutput", {})
    return hso.get("permissionDecisionReason", "") if hso.get("permissionDecision") == "deny" else None


def test_codex_preview_text_says_where_it_would_stop():
    got = cx.preview_text(60.0, "5h", True, Reading(10.0, R5))
    assert got == "stop this session when the 5-hour quota reaches 60%: 5-hour quota at 10% now → stops at 60%"
    assert "right away" in cx.preview_text(5.0, "5h", True, Reading(10.0, R5))


def test_codex_escalated_call_goes_to_the_users_approval_and_sets_the_limit_after_it_ran():
    cx.read_live(SID, time.time())
    assert cx_pre("echo agentcap set until 60% 5h", escalated=True) is None    # Codex asks the user
    assert cx.load(SID).get("quota") is None                                  # nothing set before it ran
    msg = cx_post("echo agentcap set until 60% 5h")["systemMessage"]
    assert "you approved" in msg and "stops at 60%" in msg and "agentcap off" in msg
    st = cx.load(SID)
    assert st["quota"] == 50.0 and st["source"] == "agent" and st["window"] == "5h"


def test_codex_hooks_see_only_the_command_so_any_mode_passes_to_codex():
    # live 16 Nov 2026 (Codex 0.169): PreToolUse tool_input is {"command": ...} even for an
    # escalated call, so the hook cannot require escalation; Codex's own prompt is the button
    assert cx_pre("echo agentcap set 5% 5h") is None
    assert cx_pre("echo agentcap set 5% 5h", mode="bypassPermissions") is None


def test_codex_declined_or_failed_command_sets_nothing():
    cx_post("echo agentcap set 5% 5h", response={"exit_code": 1, "output": "denied"})
    assert cx.load(SID).get("quota") is None


def test_codex_agent_may_tighten_but_not_loosen_the_users_limit():
    cx_prompt("agentcap until 30% 5h")                                 # stops at 30%
    assert "only lower the stop point" in deny_reason(cx_pre("echo agentcap set until 50% 5h", escalated=True))
    assert "only lower the stop point" in deny_reason(cx_pre("echo agentcap set 25% 5h", escalated=True))
    assert "remove it" in deny_reason(cx_pre("echo agentcap set 1% week", escalated=True))
    assert cx_pre("echo agentcap set until 20% 5h", escalated=True) is None
    # loosened between the prompt and the run: still refused at apply time
    assert "limit not changed" in cx_post("echo agentcap set until 50% 5h")["systemMessage"]
    assert cx.load(SID)["quota"] == 20.0
    cx_post("echo agentcap set until 20% 5h")
    assert cx.load(SID)["quota"] == 10.0


def test_codex_bad_arguments_are_denied_with_the_format():
    reason = deny_reason(cx_pre("echo agentcap set lots 5h", escalated=True))
    assert "echo agentcap set until 70% 5h" in reason


def test_codex_approve_after_two_approvals_points_to_off():
    cx_prompt("agentcap 2% 5h")
    cx_prompt("agentcap approve")
    cx_prompt("agentcap approve")
    out = cx_prompt("agentcap approve")
    assert "already approved twice" in out and "agentcap off" in out


def test_codex_skill_documents_the_command():
    text = (ROOT / "codex" / "skills" / "agentcap" / "SKILL.md").read_text()
    assert "echo agentcap set until 70% 5h" in text and "require_escalated" in text


# ------------------------------------------------------------------ Claude Code
def cc_hook(event, payload):
    out = io.StringIO()
    cc.main(["hook", event], stdin=io.StringIO(json.dumps(payload)), stdout=out)
    text = out.getvalue().strip()
    return json.loads(text) if text else None


def cc_payload(command, event):
    return {"session_id": SID, "transcript_path": "/t.jsonl", "cwd": "/w", "permission_mode": "default",
            "hook_event_name": event, "tool_name": "Bash", "tool_input": {"command": command},
            "tool_use_id": "t1"}


def cc_statusline(week=40.0, five=10.0):
    payload = {"hook_event_name": "Status", "session_id": SID, "cwd": "/w",
               "rate_limits": {"five_hour": {"used_percentage": five, "resets_at": R5 - 3600},
                               "seven_day": {"used_percentage": week, "resets_at": R5}}}
    cc.main(["statusline"], stdin=io.StringIO(json.dumps(payload)), stdout=io.StringIO())


def cc_cmd(*args):
    out = io.StringIO()
    cc.main(list(args) + ["--session", SID], stdout=out)
    return out.getvalue()


def test_claude_asks_the_user_with_the_stop_point():
    cc_statusline()
    out = cc_hook("pre", cc_payload("echo agentcap set until 30% 5h", "PreToolUse"))
    hso = out["hookSpecificOutput"]
    assert hso["permissionDecision"] == "ask"
    assert "5-hour quota at 10% now → stops at 30%" in hso["permissionDecisionReason"]
    assert cc.load(SID).get("quota") is None


def test_claude_sets_the_limit_after_the_approved_command_ran():
    cc_statusline()
    out = cc_hook("post", cc_payload("echo agentcap set until 30% 5h", "PostToolUse"))
    assert "you approved" in out["systemMessage"] and "stops at 30%" in out["systemMessage"]
    assert out["hookSpecificOutput"]["additionalContext"] == out["systemMessage"]
    st = cc.load(SID)
    assert st["quota"] == 20.0 and st["source"] == "agent" and st["used"] == 10.0


def test_claude_failed_command_sets_nothing():
    cc_statusline()
    cc_hook("postfail", cc_payload("echo agentcap set 5% 5h", "PostToolUseFailure"))
    assert cc.load(SID).get("quota") is None


def test_claude_agent_may_tighten_but_not_loosen():
    cc_statusline(week=40.0)
    cc_cmd("quota", "2%")                                                      # weekly, stops at 42%
    out = cc_hook("pre", cc_payload("echo agentcap set 5% week", "PreToolUse"))
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "only lower" in out["hookSpecificOutput"]["permissionDecisionReason"]
    out = cc_hook("pre", cc_payload("echo agentcap set until 41% week", "PreToolUse"))
    assert out["hookSpecificOutput"]["permissionDecision"] == "ask"


def test_claude_marker_is_not_mistaken_for_a_self_call():
    assert not cc.self_guard(cc_payload("echo agentcap set 50% 5h", "PreToolUse"))


def test_claude_approve_after_two_approvals_points_to_off():
    cc_statusline()
    cc_cmd("quota", "2%")
    cc_cmd("approve")
    cc_cmd("approve")
    out = cc_cmd("approve")
    assert "already approved twice" in out and "/agentcap:off" in out


def test_claude_plugin_ships_the_skill():
    text = (ROOT / "plugin" / "skills" / "agentcap" / "SKILL.md").read_text()
    assert "echo agentcap set until 70% 5h" in text
    assert text == (ROOT / "skills" / "agentcap" / "SKILL.md").read_text()
