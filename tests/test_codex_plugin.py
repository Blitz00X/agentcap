"""Codex plugin (scripts/agentcap_codex.py) with hook payloads shaped like Codex 0.167.1's.

Payload shapes come from the live spike (14 Nov 2026, BULGULAR.md): `exec_command` reaches
hooks as tool_name "Bash" with tool_input {"command": ...}; UserPromptSubmit carries
"prompt"; transcript_path is the session rollout (may be null).
"""
from __future__ import annotations

import importlib.util
import io
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from agentcap.quota import Reading

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "agentcap_codex.py"
spec = importlib.util.spec_from_file_location("agentcap_codex", SCRIPT)
cx = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cx)

SID = "01a1da0b-e835-7cf0-9dcd-5365269e5129"
R5 = 2_000_000_000.0
RW = 2_000_500_000.0


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTCAP_QUOTA_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("AGENTCAP_LICENSE_FILE", str(tmp_path / "license.json"))
    live = {"value": {"5h": Reading(10.0, R5), "week": Reading(40.0, RW)}}
    monkeypatch.setattr(cx, "LIVE_READER", lambda: live["value"])
    spawned: list[list[str]] = []
    monkeypatch.setattr(cx, "SPAWN", lambda argv, **kw: spawned.append(argv))
    monkeypatch.setattr(cx, "_spawned", spawned, raising=False)
    monkeypatch.setattr(cx, "_live", live, raising=False)
    return tmp_path


def pro(tmp_path):
    (tmp_path / "license.json").write_text(json.dumps(
        {"source": "polar", "valid": True, "tier": "pro", "key": "X", "checked_at": 1, "reason": "ok"}))


def set_live(five=None, week=None):
    cur = dict(cx._live["value"])
    if five is not None:
        cur["5h"] = Reading(five, R5)
    if week is not None:
        cur["week"] = Reading(week, RW)
    cx._live["value"] = cur


def hook(event, payload):
    out = io.StringIO()
    cx.main(["hook", event], stdin=io.StringIO(json.dumps(payload)), stdout=out)
    text = out.getvalue().strip()
    return json.loads(text) if text else None


def prompt(text, sid=SID):
    return hook("UserPromptSubmit", {"session_id": sid, "turn_id": "t", "cwd": "/w", "transcript_path": None,
                                     "hook_event_name": "UserPromptSubmit", "model": "gpt-6-astra",
                                     "permission_mode": "default", "prompt": text})


def pre(command="pwd", sid=SID, transcript=None, tool="Bash"):
    return hook("PreToolUse", {"session_id": sid, "turn_id": "t", "transcript_path": transcript, "cwd": "/w",
                               "hook_event_name": "PreToolUse", "model": "gpt-6-astra",
                               "permission_mode": "default", "tool_name": tool,
                               "tool_input": {"command": command}, "tool_use_id": "c1"})


def post(command="pwd", response="Exit code: 0\nok", sid=SID):
    return hook("PostToolUse", {"session_id": sid, "turn_id": "t", "cwd": "/w", "hook_event_name": "PostToolUse",
                                "tool_name": "Bash", "tool_input": {"command": command},
                                "tool_response": response, "tool_use_id": "c1"})


def stop(sid=SID, transcript=None):
    return hook("Stop", {"session_id": sid, "turn_id": "t", "cwd": "/w", "transcript_path": transcript,
                         "hook_event_name": "Stop"})


def denied(out):
    return bool(out) and out.get("hookSpecificOutput", {}).get("permissionDecision") == "deny"


# ---------------------------------------------------------------- commands
def test_agentcap_prompt_sets_the_limit_and_never_reaches_the_model(env):
    out = prompt("agentcap 2% 5h")
    assert out["decision"] == "block"
    assert "may use 2% of the 5-hour quota" in out["reason"] and "baseline 10%" in out["reason"]
    st = json.loads(cx.session_path(SID).read_text())
    assert st["quota"] == 2.0 and st["window"] == "5h" and st["tracker"]["baseline"] == 10.0


def test_ordinary_prompts_pass_through(env):
    assert prompt("fix the failing tests") is None
    assert prompt("please explain agentcap to me") is None
    assert prompt("agentcap 5%\nand then fix the tests") is None   # multi-line is a real prompt


def test_bad_command_explains_itself(env):
    out = prompt("agentcap lots")
    assert out["decision"] == "block" and "agentcap 10% 5h" in out["reason"]
    assert prompt("agentcap 5% fortnight")["decision"] == "block"


def test_status_off_and_help(env):
    assert "no limit" in prompt("agentcap status")["reason"]
    prompt("agentcap 5% week")
    assert "session 0.0% / 5% (weekly)" in prompt("agentcap status")["reason"]
    assert "cleared" in prompt("agentcap off")["reason"]
    assert pre() is None
    assert "agentcap default 10% 5h" in prompt("agentcap help")["reason"]


# ---------------------------------------------------------------- quota stop
def test_pro_denies_at_the_limit_from_the_live_reading(env):
    pro(env)
    prompt("agentcap 2% 5h")
    assert pre() is None
    cx._atomic_write(cx.live_path(SID), {"ts": 9e12, "readings": {"5h": {"used": 12.0, "resets_at": R5}}})
    out = pre("ls")
    assert denied(out) and "used 2.0% of the 5-hour quota (limit 2%)" in out["hookSpecificOutput"]["permissionDecisionReason"]
    assert "agentcap approve" in out["hookSpecificOutput"]["permissionDecisionReason"]


def test_rollout_reading_counts_and_minute_rounded_resets_match(env, tmp_path):
    pro(env)
    prompt("agentcap 3% 5h")
    rollout = tmp_path / "rollout.jsonl"
    line = {"timestamp": "t", "type": "event_msg", "payload": {"type": "token_count", "rate_limits": {
        "primary": {"used_percent": 13.0, "window_minutes": 300, "resets_at": R5 + 37},
        "secondary": {"used_percent": 40.0, "window_minutes": 10080, "resets_at": RW}}}}
    rollout.write_text(json.dumps(line) + "\n")
    out = pre("ls", transcript=str(rollout))
    assert denied(out)
    st = json.loads(cx.session_path(SID).read_text())
    assert st["tracker"]["resets_seen"] == 0 and st["share"] == 3.0


def test_free_gets_one_notice_and_no_deny(env):
    prompt("agentcap 2% 5h")
    cx._atomic_write(cx.live_path(SID), {"ts": 9e12, "readings": {"5h": {"used": 13.0, "resets_at": R5}}})
    out = pre("ls")
    assert not denied(out) and "Pro would stop" in out["systemMessage"]
    assert pre("ls -la") is None


def test_warns_once_at_80_percent(env):
    pro(env)
    prompt("agentcap 5% 5h")
    cx._atomic_write(cx.live_path(SID), {"ts": 9e12, "readings": {"5h": {"used": 14.0, "resets_at": R5}}})
    assert "session at 4.0%" in pre("ls")["systemMessage"]
    assert pre("ls -a") is None


def test_background_probe_is_throttled(env):
    prompt("agentcap 5% 5h")       # live read at set time
    pre("ls")
    assert cx._spawned == []        # fresh enough
    cx._atomic_write(cx.live_path(SID), {"ts": 1.0, "readings": {}})
    pre("ls -a")
    pre("ls -l")
    assert len(cx._spawned) == 1 and cx._spawned[0][-3:] == ["probe", "--session", SID]
    assert {"-B", "-E", "-P"} <= set(cx._spawned[0])


def test_no_live_answer_takes_the_baseline_from_the_next_reading(env):
    pro(env)
    cx._live["value"] = None
    assert "not available yet" in prompt("agentcap 2% 5h")["reason"]
    assert pre() is None
    cx._atomic_write(cx.live_path(SID), {"ts": 9e12, "readings": {"5h": {"used": 20.0, "resets_at": R5}}})
    assert pre("ls") is None        # baseline 20 now
    cx._atomic_write(cx.live_path(SID), {"ts": 9e12 + 1, "readings": {"5h": {"used": 22.0, "resets_at": R5}}})
    assert denied(pre("ls -a"))


# ---------------------------------------------------------------- breakers
def test_loop_breaker_denies_the_4th_identical_call(env):
    pro(env)
    for _ in range(3):
        assert pre("pwd") is None
    out = pre("pwd")
    assert denied(out) and "same tool call 4 times" in out["hookSpecificOutput"]["permissionDecisionReason"]
    assert denied(pre("ls"))        # stays paused until approve
    assert "breaker cleared" in prompt("agentcap approve")["reason"]
    assert pre("ls") is None


def test_waits_are_not_loops_or_errors(env):
    pro(env)
    for _ in range(10):
        assert pre("sleep 4; ls done.flag") is None
        post("sleep 4; ls done.flag", "Exit code: 2\nls: cannot access 'done.flag'")
    assert not cx.load(SID).get("loop", {}).get("tripped")


def test_error_breaker_after_8_failures(env):
    pro(env)
    for i in range(8):
        assert pre(f"cat /yok-{i}") is None
        post(f"cat /yok-{i}", {"exit_code": 1, "output": "No such file"})
    out = pre("cat /yok-9")
    assert denied(out) and "8 failing tool calls" in out["hookSpecificOutput"]["permissionDecisionReason"]


def test_free_loop_gets_one_notice(env):
    for _ in range(3):
        pre("pwd")
    assert "Pro would stop" in pre("pwd")["systemMessage"]
    assert pre("pwd") is None


@pytest.mark.parametrize("resp,failed", [
    ("Exit code: 1\nboom", True), ("Exit code: 0\nok", False), ({"exit_code": 2}, True),
    ({"exitCode": 0, "stdout": "x"}, False), ({"isError": True, "content": []}, True),
    ({"content": [{"type": "text", "text": "ok"}]}, False), ("Process exited with code 127", True),
])
def test_tool_failure_shapes(resp, failed):
    assert cx.tool_failed(resp) is failed


# ---------------------------------------------------------------- approve
def test_approve_raises_50_percent_at_most_twice(env):
    pro(env)
    prompt("agentcap 2% 5h")
    assert "raised to 3%" in prompt("agentcap approve")["reason"]
    assert "raised to 4.5%" in prompt("agentcap approve")["reason"]
    assert "already approved twice" in prompt("agentcap approve")["reason"]


def test_approve_warns_when_the_session_is_already_past_the_new_limit(env):
    pro(env)
    prompt("agentcap 2% 5h")
    cx._atomic_write(cx.live_path(SID), {"ts": 9e12, "readings": {"5h": {"used": 14.0, "resets_at": R5}}})
    pre("ls")
    assert "already at 4.0%" in prompt("agentcap approve")["reason"]


def test_free_approve_is_refused(env):
    prompt("agentcap 2% 5h")
    assert "approve is Pro" in prompt("agentcap approve")["reason"]


# ---------------------------------------------------------------- default (headless codex exec)
def test_default_applies_to_new_sessions(env):
    pro(env)
    assert "every new Codex session" in prompt("agentcap default 3% 5h")["reason"]
    assert "default for new sessions: 3% 5h" in prompt("agentcap status")["reason"]
    out = hook("SessionStart", {"session_id": "new-1", "cwd": "/w", "hook_event_name": "SessionStart",
                                "source": "startup", "transcript_path": None})
    assert "default limit 3% of the 5-hour quota" in out["systemMessage"]
    st = json.loads(cx.session_path("new-1").read_text())
    assert st["quota"] == 3.0 and st["source"] == "default" and st["tracker"]["baseline"] == 10.0
    prompt("agentcap 9% week", sid="new-1")
    assert hook("SessionStart", {"session_id": "new-1", "source": "resume"}) is None   # resume keeps its own
    assert json.loads(cx.session_path("new-1").read_text())["quota"] == 9.0
    assert "removed" in prompt("agentcap default off")["reason"]
    assert hook("SessionStart", {"session_id": "new-2", "source": "startup"}) is None


# ---------------------------------------------------------------- self-guard
@pytest.mark.parametrize("command", [
    "agentcap approve", "env -u CODEX_HOME agentcap  approve", "python3 -m agentcap.cli approve",
    "echo '{}' > ~/.local/state/agentcap/quota/codex/sessions/x.json",
    "rm -rf ~/.local/state/agentcap", "python3 ~/.codex/plugins/cache/x/agentcap/1/scripts/agentcap_codex.py hook Stop",
])
def test_self_guard_denies(env, command):
    assert denied(pre(command))


def test_self_guard_allows_plain_reads(env):
    assert pre("cat ~/.local/state/agentcap/quota/codex/receipts.jsonl") is None
    assert pre("ls /tmp/agentcap-test") is None


# ---------------------------------------------------------------- stop + entry
def test_stop_reads_live_writes_a_receipt_and_shows_the_share(env):
    pro(env)
    prompt("agentcap 5% 5h")
    set_live(five=12.0)
    out = stop()
    assert out["systemMessage"] == "agentcap: session 2.0% / 5% (5-hour)"
    rec = json.loads((cx.state_dir() / "receipts.jsonl").read_text().splitlines()[-1])
    assert rec["share"] == 2.0 and rec["agent"] == "codex" and rec["measured_by"] == "app-server"
    stop()
    assert len((cx.state_dir() / "receipts.jsonl").read_text().splitlines()) == 1


def test_hooks_fail_open_on_garbage(env):
    out = io.StringIO()
    assert cx.main(["hook", "PreToolUse"], stdin=io.StringIO("{not json"), stdout=out) == 0
    assert out.getvalue() == ""
    assert cx.main(["hook", "PreToolUse"], stdin=io.StringIO("[1,2]"), stdout=out) == 0


def test_state_overrides_need_testing_mode(env, monkeypatch):
    monkeypatch.delenv("AGENTCAP_TESTING")
    assert cx.state_dir() == Path.home() / ".local" / "state" / "agentcap" / "quota" / "codex"
    assert cx.license_path() == Path.home() / ".config" / "agentcap" / "license.json"


# ---------------------------------------------------------------- package
def test_codex_plugin_package_is_in_sync_and_marketplace_points_at_it():
    sys.path.insert(0, str(ROOT / "scripts"))
    import build_plugin
    assert build_plugin.codex_problems() == [], "run: python3 scripts/build_plugin.py"
    market = json.loads((ROOT / ".agents" / "plugins" / "marketplace.json").read_text())
    assert market["plugins"][0]["source"]["path"] == "./codex-plugin"
    manifest = json.loads((ROOT / "codex-plugin" / ".codex-plugin" / "plugin.json").read_text())
    claude = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text())
    assert manifest["version"] == claude["version"] and manifest["hooks"] == "./hooks/hooks.json"
    hooks = json.loads((ROOT / "codex-plugin" / "hooks" / "hooks.json").read_text())["hooks"]
    assert set(hooks) == {"SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse", "Stop"}
    for groups in hooks.values():
        for h in groups[0]["hooks"]:
            assert h["command"].startswith('python3 -B -E -P "${PLUGIN_ROOT}/scripts/agentcap_codex.py" hook ')


def test_copied_codex_plugin_runs_without_the_repo(tmp_path):
    dest = tmp_path / "cache" / "agentcap"
    shutil.copytree(ROOT / "codex-plugin", dest)
    payload = {"session_id": "s", "prompt": "agentcap help", "hook_event_name": "UserPromptSubmit"}
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path / "home")}
    r = subprocess.run([sys.executable, "-E", "-P", str(dest / "scripts" / "agentcap_codex.py"), "hook",
                        "UserPromptSubmit"], input=json.dumps(payload), capture_output=True, text=True,
                       env=env, cwd=tmp_path, timeout=30)
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    assert out["decision"] == "block" and "agentcap 10% 5h" in out["reason"]
    # Codex fingerprints the plugin: a hook that writes __pycache__ into it makes Codex refuse
    # every later hook ("plugin contents changed"), even when the command lacks -B
    assert not list(dest.rglob("__pycache__")) and not list(dest.rglob("*.pyc"))
