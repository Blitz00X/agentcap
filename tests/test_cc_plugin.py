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
    out = cmd("approve")
    assert "loop breaker cleared" in out
    assert _pre() is None


def test_approve_at_full_quota_says_quota_no_longer_stops(env):
    pro(env)
    statusline(sl_payload(1.0)); cmd("quota", "100%")
    out = cmd("approve")
    assert "150%" in out and "no longer stops" in out


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


# ------------------------------- 0.3.0: the model may not approve/raise/clear itself
# Measured with Claude Code 2.1.318 (headless, --plugin-dir): a slash command's
# `!` line runs WITHOUT going through PreToolUse hooks, while the model's own Bash
# call to the same script does hit them.  So hook_pre can deny every model-made
# invocation of the plugin script and /agentcap:approve etc. keep working.
P = "/home/u/.claude/plugins/cache/agentcap/agentcap/0.3.0/scripts/agentcap_cc.py"

MODEL_SELF_CALLS = [
    f"python3 {P} approve --session {SID}",
    f'python3 "${{CLAUDE_PLUGIN_ROOT}}/scripts/agentcap_cc.py" approve --session "{SID}"',
    f"python3 {P} 'approve'",
    f'python3 {P} "approve"',
    f"python3 {P} a''pprove",
    f"python3 {P} \\approve",
    f"python3${{IFS}}{P}${{IFS}}approve",
    f"python3 {P} quota 50%",
    f"python3 {P} quota 100% week --session {SID}",
    f"python3 {P} off --session {SID}",
    f"python3 {P} unsetup",
    f"python3 {P} setup",
    f"echo '{{}}' | python3 {P} statusline",
    f"echo '{{}}' | python3 {P} hook post",
    f"bash -c \"python3 {P} approve\"",
    f"sh -c 'python3 {P} approve'",
    f"cd /home/u/.claude/plugins/cache/agentcap/agentcap/0.3.0/scripts && python3 -m agentcap_cc approve",
    f"cd /x && python3 ./agentcap_cc.py approve",
    f"{P} approve",
    f"env -u CLAUDECODE /usr/bin/python3.12 {P} approve",
    f"A=approve; python3 {P} $A",
    f"python3 {P} $(printf appr; printf ove)",
    "python3 /home/u/.claude/plugins/cache/agentcap/agentcap/0.3.0/scripts/agentcap*.py approve",
    f"python3 -c \"import runpy,sys; sys.argv=['x','approve']; runpy.run_path('{P}')\"",
    f"python3 -c 'import sys; sys.path.insert(0, \"/p/scripts\"); import agentcap_cc; agentcap_cc.main([\"approve\"])'",
    f"sed -i 's/APPROVE_MAX = 2/APPROVE_MAX = 99/' {P}",
    f"cp /tmp/evil.py {P}",
    f"cat {P}\npython3 {P} approve",  # the normaliser joins lines; a newline is still a new command
    f"git -c core.pager='python3 {P} approve' log -1",
    f"cat {P} > /tmp/copy.py",
]


def _deny_reason(d):
    assert d is not None, "expected a deny, got silence"
    hs = d["hookSpecificOutput"]
    assert hs["hookEventName"] == "PreToolUse" and hs["permissionDecision"] == "deny"
    return hs["permissionDecisionReason"]


@pytest.mark.parametrize("command", MODEL_SELF_CALLS)
def test_model_bash_call_to_plugin_script_is_denied(env, command):
    pro(env)
    reason = _deny_reason(_pre(command))
    assert "/agentcap:approve" in reason and "user" in reason


@pytest.mark.parametrize("command", MODEL_SELF_CALLS[:3])
def test_model_self_call_is_denied_on_free_too(env, command):
    reason = _deny_reason(_pre(command))
    assert "user" in reason


def test_model_self_approve_after_pro_loop_stop_is_denied_and_changes_nothing(env):
    pro(env)
    statusline(sl_payload(1.0)); cmd("quota", "5%"); statusline(sl_payload(2.0))
    for _ in range(4):
        d = _pre()
    assert d["continue"] is False  # loop breaker stopped the agent
    before_loop = json.loads(cc._loop_path(SID).read_text())
    before_state = cc.load(SID)
    d = _pre(f"python3 {SCRIPT} approve --session {SID}")
    _deny_reason(d)
    assert json.loads(cc._loop_path(SID).read_text()) == before_loop
    assert cc.load(SID) == before_state and cc.load(SID)["quota"] == 5.0


def test_user_slash_command_path_still_approves(env):
    """/agentcap:approve runs the script from a `!` line, outside PreToolUse."""
    pro(env)
    statusline(sl_payload(1.0)); cmd("quota", "5%"); statusline(sl_payload(2.0))
    for _ in range(4):
        _pre()
    _deny_reason(_pre(f"python3 {SCRIPT} approve --session {SID}"))
    out = cmd("approve")  # what the `!` line executes
    assert "approved (1/2)" in out and "loop breaker cleared" in out
    assert cc.load(SID)["quota"] == 7.5


def test_state_changing_slash_commands_run_from_bang_lines():
    for name in ("approve", "quota", "off", "setup", "unsetup"):
        text = (ROOT / "commands" / f"{name}.md").read_text()
        bang = [ln for ln in text.splitlines() if ln.startswith("!`") and "agentcap_cc.py" in ln]
        assert len(bang) == 1 and f"agentcap_cc.py\" {name}" in bang[0], name
        assert "disable-model-invocation: true" in text


def test_hook_subprocess_denies_self_approve(env):
    """Exactly how hooks.json runs it: `python3 <root>/scripts/agentcap_cc.py hook pre`."""
    pro(env)
    payload = dict(PRE, tool_input={"command": f"python3 {SCRIPT} approve --session {SID}"})
    res = subprocess.run([sys.executable, str(SCRIPT), "hook", "pre"], input=json.dumps(payload),
                         capture_output=True, text=True, timeout=30)
    assert res.returncode == 0
    assert json.loads(res.stdout)["hookSpecificOutput"]["permissionDecision"] == "deny"


@pytest.mark.parametrize("tool", ["Monitor", "PowerShell", "mcp__shell__run"])
def test_any_tool_with_a_command_field_is_checked(env, tool):
    _deny_reason(hook("pre", dict(PRE, tool_name=tool, tool_input={"command": f"python3 {P} approve"})))


def test_bash_tampering_with_state_dir_is_denied(env):
    state = cc.state_root()
    for command in (
        f"rm {state}/cc/loops/{SID}.json",
        f"echo '{{}}' > {cc.cc_dir()}/sessions/{SID}.json",
        f"sed -i s/approvals/x/ {cc.session_path(SID)}",
        "rm -rf ~/.local/state/agentcap/quota/cc",
        "rm -rf $HOME/.local/state/agentcap/quota/cc/loops",
        "python3 -c \"import os; os.remove(os.environ['AGENTCAP_QUOTA_DIR'] + '/cc/loops/x.json')\"",
        "rm -rf $AGENTCAP_QUOTA_DIR/cc",
    ):
        _deny_reason(_pre(command))


@pytest.mark.parametrize("tool,key", [("Write", "file_path"), ("Edit", "file_path"),
                                      ("MultiEdit", "file_path"), ("NotebookEdit", "notebook_path")])
def test_file_tools_on_state_dir_are_denied(env, tool, key):
    pro(env)
    for target in (str(cc.session_path(SID)), str(cc._loop_path(SID)), str(cc.state_root() / "claude_last.json")):
        _deny_reason(hook("pre", dict(PRE, tool_name=tool, tool_input={key: target, "content": "{}"})))
    rel = dict(PRE, cwd=str(cc.state_root()), tool_name=tool, tool_input={key: "cc/sessions/x.json"})
    _deny_reason(hook("pre", rel))


def test_file_tools_on_installed_plugin_code_are_denied(env):
    for name in ("scripts/agentcap_cc.py", "hooks/hooks.json", "commands/approve.md"):
        _deny_reason(hook("pre", dict(PRE, tool_name="Edit", tool_input={"file_path": str(ROOT / name)})))


ALLOWED = [
    "pytest -q",
    "git commit -m 'approve the quota change'",
    "agentcap status",
    f"cat {SCRIPT}",
    f"grep -n hook_pre {SCRIPT} | head -5",
    "ls ~/.claude/plugins",
    f"ls {P.rsplit('/', 1)[0]}",
    "cat ${HOME}/.local/state/agentcap/quota/cc/sessions/x.json | jq .quota",
    "git add scripts/agentcap_cc.py tests/test_cc_plugin.py && git diff --cached --stat",
]


@pytest.mark.parametrize("command", ALLOWED)
def test_ordinary_and_read_only_commands_pass(env, command):
    pro(env)
    assert _pre(command) is None


def test_ordinary_file_edits_pass_and_plugin_dev_checkout_is_editable(env):
    assert hook("pre", dict(PRE, tool_name="Write", tool_input={"file_path": "/w/notes.md", "content": "x"})) is None
    assert hook("pre", dict(PRE, tool_name="Read", tool_input={"file_path": str(cc.session_path(SID))})) is None
    dev = dict(PRE, cwd=str(ROOT), tool_name="Edit", tool_input={"file_path": str(ROOT / "scripts" / "agentcap_cc.py")})
    assert hook("pre", dev) is None  # working on the plugin itself, from its checkout


# ------------------------------------------------- /usage (live CLI tests C1-C3)
from agentcap.quota import Reading  # noqa: E402

RESETS = 2_000_000_000


def _live(monkeypatch, value, calls=None):
    monkeypatch.setattr(cc, "live_enabled", lambda: True)

    def reader(agent):
        if calls is not None:
            calls.append(agent)
        return {"week": Reading(value, RESETS - 30), "5h": Reading(10.0, RESETS - 3600)}
    monkeypatch.setattr(cc, "live_readings", reader)


def test_quota_baseline_comes_from_usage(env, monkeypatch):
    pro(env)
    _live(monkeypatch, 40.0)
    out = cmd("quota", "2%")
    assert "baseline 40% (exact, from /usage)" in out
    statusline(sl_payload(39.0))          # stale, lower statusline value: ignored
    assert json.loads(cc.session_path(SID).read_text())["share"] == 0.0
    assert hook("pre", dict(PRE, tool_input={"command": "a"})) is None


def test_usage_share_denies_when_the_statusline_lags(env, monkeypatch):
    pro(env)
    _live(monkeypatch, 40.0)
    cmd("quota", "2%")
    statusline(sl_payload(40.0))
    # the background probe saw 42%, the statusline still says 40%
    cc._atomic_write(cc.usage_path(SID), {"ts": 0, "readings": {"week": {"used": 42.0, "resets_at": RESETS}}})
    monkeypatch.setattr(cc, "maybe_probe_usage", lambda *a, **k: False)
    deny = hook("pre", dict(PRE, tool_input={"command": "b"}))
    assert deny["hookSpecificOutput"]["permissionDecision"] == "deny" and "2.0%" in deny["stopReason"]
    assert "session 2.0% / 2%" in statusline(sl_payload(40.0))
    stop = {"session_id": SID, "hook_event_name": "Stop", "stop_hook_active": False, "cwd": "/w"}
    hook("stop", stop)
    rec = json.loads((cc.cc_dir() / "receipts.jsonl").read_text().splitlines()[-1])
    assert rec["share"] == 2.0 and rec["measured_by"] == "usage"


def test_without_usage_another_sessions_reading_is_not_the_baseline(env):
    pro(env)
    cc._atomic_write(cc.state_root() / "claude_last.json",
                     {"ts": __import__("time").time() - 60, "readings": {"week": {"used": 4.0, "resets_at": RESETS}}})
    out = cmd("quota", "2%")
    assert "taken at the next statusline refresh" in out


def test_probe_starts_at_most_once_a_minute(env, monkeypatch):
    pro(env)
    _live(monkeypatch, 40.0)
    cmd("quota", "2%")
    statusline(sl_payload(40.0))
    started = []
    monkeypatch.setattr(cc.subprocess, "Popen", lambda argv, **kw: started.append(argv))
    state = json.loads(cc.session_path(SID).read_text())
    now = __import__("time").time()
    assert cc.maybe_probe_usage(SID, state, now) is False           # /usage read just now
    assert cc.maybe_probe_usage(SID, state, now + 61) is True
    assert cc.maybe_probe_usage(SID, state, now + 62) is False      # one already running
    assert len(started) == 1 and started[0][-3:] == ["usage-probe", "--session", SID]


def test_usage_probe_writes_the_reading_and_clears_its_lock(env, monkeypatch):
    _live(monkeypatch, 41.0)
    lock = cc.usage_path(SID).with_suffix(".probe")
    cc._atomic_write(lock, {"ts": 1})
    out = io.StringIO()
    cc.main(["usage-probe", "--session", SID], stdout=out)
    assert json.loads(cc.usage_path(SID).read_text())["readings"]["week"]["used"] == 41.0
    assert not lock.exists() and out.getvalue() == ""


def test_own_denials_do_not_feed_the_plugin_error_breaker(env):
    pro(env)
    fail = {"session_id": SID, "hook_event_name": "PostToolUseFailure", "tool_name": "Read",
            "error": "PreToolUse:Read hook error: agentcap: this session used 2.0% of the weekly quota"}
    for _ in range(10):
        assert hook("postfail", fail) is None


def test_self_guard_denies_the_cli_approve(env):
    # live C6: `agentcap approve` from the model ran; only the CLI's env check refused it
    for command in ("agentcap approve", "env -u CLAUDECODE agentcap approve",
                    "AGENTCAP_GUARDED= agentcap  approve", "python3 -m agentcap.cli approve"):
        out = hook("pre", dict(PRE, tool_input={"command": command}))
        assert out and out["hookSpecificOutput"]["permissionDecision"] == "deny", command
    assert hook("pre", dict(PRE, tool_input={"command": "agentcap quota"})) is None


def test_plugin_waits_trip_neither_breaker(env):
    # live gate test 13 Nov: `sleep 4; ls done.flag` until a file appears is waiting, not a loop
    pro(env)
    wait = dict(PRE, tool_input={"command": "sleep 4; ls done.flag"})
    fail = {"session_id": SID, "hook_event_name": "PostToolUseFailure", "tool_name": "Bash",
            "tool_input": {"command": "sleep 4; ls done.flag"}, "error": "ls: cannot access 'done.flag'"}
    for _ in range(10):
        assert hook("pre", wait) is None
        assert hook("postfail", fail) is None
    for _ in range(3):
        assert hook("pre", dict(PRE, tool_input={"command": "pwd"})) is None
    assert hook("pre", dict(PRE, tool_input={"command": "pwd"}))["hookSpecificOutput"]["permissionDecision"] == "deny"
