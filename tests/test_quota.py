"""Quota mode: fixtures follow the documented Claude statusline schema and the
Codex rollout `token_count` schema; e2e uses fake claude/codex executables."""
from __future__ import annotations

import io
import json
import os
import stat
import sys
import textwrap
import time
from pathlib import Path

import pytest

from agentcap import cli
from agentcap import quota as q
from agentcap.alerts import format_alert, sanitize

NOW = 1_793_000_000.0


def statusline_payload(five=23.5, week=41.2, sid="sess-1", resets5=NOW + 3600, resets7=NOW + 86400 * 3):
    return {
        "cwd": "/proj", "session_id": sid, "model": {"id": "claude-opus-5-5", "display_name": "Opus"},
        "workspace": {"current_dir": "/proj", "project_dir": "/proj"}, "version": "2.1.197",
        "cost": {"total_cost_usd": 0.01234}, "context_window": {"used_percentage": 8},
        "rate_limits": {
            "five_hour": {"used_percentage": five, "resets_at": resets5},
            "seven_day": {"used_percentage": week, "resets_at": resets7},
        },
    }


def codex_line(primary=12.0, secondary=40.0, resets_p=NOW + 3600, resets_s=NOW + 86400 * 4, legacy=False):
    def block(used, minutes, resets):
        b = {"used_percent": used, "window_minutes": minutes}
        if legacy:
            b["resets_in_seconds"] = int(resets - NOW)
        else:
            b["resets_at"] = int(resets)
        return b
    return {
        "timestamp": "2026-10-30T10:00:00.000Z", "type": "event_msg",
        "payload": {
            "type": "token_count",
            "info": {"total_token_usage": {"input_tokens": 1200, "output_tokens": 300, "total_tokens": 1500}},
            "rate_limits": {"primary": block(primary, 300, resets_p),
                            "secondary": block(secondary, 10080, resets_s), "plan_type": "plus"},
        },
    }


# --------------------------------------------------------------- parsing
def test_parse_quota():
    assert q.parse_quota("5%") == 5.0
    assert q.parse_quota(" 2.5 ") == 2.5
    for bad in ("0", "101", "x%"):
        with pytest.raises(ValueError):
            q.parse_quota(bad)


def test_claude_readings_schema():
    r = q.claude_readings(statusline_payload())
    assert r["5h"] == q.Reading(23.5, NOW + 3600)
    assert r["week"].used == 41.2
    assert q.claude_readings({"session_id": "x"}) == {}  # API-key login: no rate_limits
    partial = statusline_payload()
    del partial["rate_limits"]["five_hour"]
    assert set(q.claude_readings(partial)) == {"week"}


def test_codex_readings_schema_and_legacy():
    r = q.codex_readings(codex_line())
    assert r["5h"].used == 12.0 and r["week"].used == 40.0
    legacy = q.codex_readings(codex_line(legacy=True), now=NOW)
    assert legacy["week"].resets_at == pytest.approx(NOW + 86400 * 4)
    assert q.codex_readings({"type": "event_msg", "payload": {"type": "agent_message"}}) == {}
    # Pro accounts often only have a weekly window in `primary`
    only_week = {"type": "event_msg", "payload": {"type": "token_count", "rate_limits": {
        "primary": {"used_percent": 7.0, "window_minutes": 10080, "resets_at": NOW + 10}}}}
    assert set(q.codex_readings(only_week)) == {"week"}


# --------------------------------------------------------------- tracker
def test_tracker_share_and_reset():
    t = q.ShareTracker()
    t.update(q.Reading(40.0, NOW + 100))
    t.update(q.Reading(42.5, NOW + 100))
    assert t.share == 2.5
    # window resets mid-job: usage restarts at 0
    t.update(q.Reading(1.0, NOW + 100 + 18000))
    assert t.share == 3.5 and t.resets_seen == 1
    t2 = q.ShareTracker.from_dict(json.loads(json.dumps(t.to_dict())))
    assert t2.share == 3.5


def test_baseline_from_last_rules():
    last = {"ts": NOW - 60, "readings": {"week": {"used": 40.0, "resets_at": NOW + 999}}}
    assert q.baseline_from_last(last, "week", now=NOW) == q.Reading(40.0, NOW + 999)
    assert q.baseline_from_last({**last, "ts": NOW - 3600}, "week", now=NOW) is None
    assert q.baseline_from_last(last, "5h", now=NOW) is None


# ------------------------------------------------- statusline + hook units
@pytest.fixture
def job(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTCAP_QUOTA_DIR", str(tmp_path / "state"))
    d = tmp_path / "job"
    d.mkdir()
    (d / "job.json").write_text(json.dumps({"quota": 5, "window": "week", "started_at": NOW, "enforce": True}))
    return d


def run_statusline(job, payload, now=NOW):
    out = io.StringIO()
    q.statusline_main(job, io.StringIO(json.dumps(payload)), out, now=now)
    return out.getvalue()


def run_hook(job):
    out = io.StringIO()
    q.hook_main(job, io.StringIO(json.dumps({"hook_event_name": "PreToolUse", "tool_name": "Bash"})), out)
    return out.getvalue()


def test_statusline_records_and_hook_denies(job):
    assert "job 0.0% / 5%" in run_statusline(job, statusline_payload(week=40.0))
    assert run_hook(job) == ""  # allowed
    run_statusline(job, statusline_payload(week=43.0))
    assert run_hook(job) == ""
    run_statusline(job, statusline_payload(week=45.1))
    out = json.loads(run_hook(job))
    hso = out["hookSpecificOutput"]
    assert hso["hookEventName"] == "PreToolUse" and hso["permissionDecision"] == "deny"
    assert "5.1% of the weekly" in hso["permissionDecisionReason"]
    assert out["continue"] is False
    assert (job / "denies.log").exists()


def test_statusline_exact_baseline_from_previous_reading(job, tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    (state / "claude_last.json").write_text(json.dumps(
        {"ts": NOW - 30, "readings": {"week": {"used": 38.0, "resets_at": NOW + 86400 * 3}}}))
    run_statusline(job, statusline_payload(week=39.5))
    status = json.loads((job / "status.json").read_text())
    assert status["share"] == 1.5 and status["tracker"]["baseline_exact"] is True


def test_statusline_chains_user_command_and_never_breaks(job):
    data = json.loads((job / "job.json").read_text())
    data["chain"] = "cat >/dev/null; echo '[Opus] my line'"
    (job / "job.json").write_text(json.dumps(data))
    out = run_statusline(job, statusline_payload())
    assert out.splitlines()[0] == "[Opus] my line"
    assert out.splitlines()[1].startswith("agentcap: job")
    data["chain"] = "exit 7"
    (job / "job.json").write_text(json.dumps(data))
    assert "agentcap" in run_statusline(job, statusline_payload())
    # garbage stdin never raises
    out = io.StringIO()
    assert q.statusline_main(job, io.StringIO("{not json"), out) == 0


def test_statusline_parallel_sessions_recorded(job):
    run_statusline(job, statusline_payload(sid="a"))
    run_statusline(job, statusline_payload(sid="b"))
    assert json.loads((job / "status.json").read_text())["sessions"] == ["a", "b"]


def test_hook_fails_open_without_state(tmp_path):
    assert q.hook_main(tmp_path, io.StringIO("{}"), io.StringIO()) == 0


def test_user_statusline_precedence(tmp_path):
    home, cwd = tmp_path / "h", tmp_path / "p"
    (home / ".claude").mkdir(parents=True)
    (cwd / ".claude").mkdir(parents=True)
    (home / ".claude" / "settings.json").write_text(json.dumps({"statusLine": {"type": "command", "command": "user.sh"}}))
    assert q.user_statusline_command(cwd, home) == "user.sh"
    (cwd / ".claude" / "settings.local.json").write_text(json.dumps({"statusLine": {"type": "command", "command": "local.sh"}}))
    assert q.user_statusline_command(cwd, home) == "local.sh"


def test_settings_layer_and_injection(tmp_path):
    s = q.claude_settings(tmp_path / "j k", python="/usr/bin/python3")
    assert s["statusLine"]["type"] == "command" and "statusline --job" in s["statusLine"]["command"]
    assert "'" in s["statusLine"]["command"]  # path quoted
    pre = s["hooks"]["PreToolUse"][0]
    assert pre["matcher"] == "*" and pre["hooks"][0]["type"] == "command"
    assert q.inject_claude_settings(["claude", "-p", "hi"], Path("/x/s.json")) == ["claude", "--settings", "/x/s.json", "-p", "hi"]


def test_alert_formats_and_whitelist():
    ev = {"type": "quota_exceeded", "run_id": "q1", "window": "week", "quota_pct": 5, "share_pct": 5.2}
    assert "QUOTA LIMIT HIT" in format_alert(ev)
    assert sanitize(ev)["share_pct"] == 5.2
    assert "5-hour" in format_alert({**ev, "type": "quota_threshold", "window": "5h"})


# ------------------------------------------------------------------- e2e
FAKE_CLAUDE = textwrap.dedent('''\
    #!{python}
    """Mimics Claude Code: --settings layer, statusline after each response,
    PreToolUse hook before each tool call; stops when the hook says so."""
    import json, os, subprocess, sys, time
    args = sys.argv[1:]
    assert args[0] == "--settings", args
    settings = json.load(open(args[1]))
    sl = settings["statusLine"]["command"]
    hook = settings["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
    week = float(os.environ.get("FAKE_START", "40"))
    step = float(os.environ.get("FAKE_STEP", "1"))
    sid = os.environ.get("FAKE_SID", "sess-1")
    for i in range(int(os.environ.get("FAKE_N", "20"))):
        week += step
        payload = {{"session_id": sid, "model": {{"display_name": "Opus"}},
                   "rate_limits": {{"five_hour": {{"used_percentage": 10 + i, "resets_at": time.time() + 3600}},
                                   "seven_day": {{"used_percentage": week, "resets_at": time.time() + 86400}}}}}}
        if os.environ.get("FAKE_APIKEY"):
            payload.pop("rate_limits")
        out = subprocess.run(sl, shell=True, input=json.dumps(payload), capture_output=True, text=True).stdout
        print("STATUS", out.strip().replace("\\n", " | "), flush=True)
        res = subprocess.run(hook, shell=True, input=json.dumps({{"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {{"command": "same" if os.environ.get("FAKE_SAME") else f"step {{i}}"}}}}),
                             capture_output=True, text=True).stdout.strip()
        if res:
            d = json.loads(res)
            if d["hookSpecificOutput"]["permissionDecision"] == "deny":
                print("DENIED", d["hookSpecificOutput"]["permissionDecisionReason"], flush=True)
                if d.get("continue") is False and not os.environ.get("FAKE_IGNORE_STOP"):
                    sys.exit(0)
        print("TOOL", i, flush=True)
        time.sleep(0.15)
    sys.exit(0)
''')

FAKE_CODEX = textwrap.dedent('''\
    #!{python}
    """Mimics Codex CLI: appends token_count events to a new rollout file."""
    import json, os, sys, time, datetime
    home = os.environ["CODEX_HOME"]
    day = datetime.date.today()
    d = os.path.join(home, "sessions", f"{{day:%Y}}", f"{{day:%m}}", f"{{day:%d}}")
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, f"rollout-{{time.time():.0f}}-fake.jsonl")
    week = float(os.environ.get("FAKE_START", "40"))
    step = float(os.environ.get("FAKE_STEP", "1"))
    reset_at = int(os.environ.get("FAKE_RESET_AT", "0"))
    with open(path, "a") as fh:
        fh.write(json.dumps({{"type": "session_meta", "payload": {{"id": "x"}}}}) + "\\n")
        for i in range(int(os.environ.get("FAKE_N", "40"))):
            week += step
            resets = time.time() + 86400
            if reset_at and i >= reset_at:
                if i == reset_at:
                    week = step
                resets += 7 * 86400
            line = {{"timestamp": "t", "type": "event_msg", "payload": {{"type": "token_count", "rate_limits": {{
                "primary": {{"used_percent": 10.0, "window_minutes": 300, "resets_at": int(time.time() + 3600)}},
                "secondary": {{"used_percent": week, "window_minutes": 10080, "resets_at": int(resets)}}}}}}}}
            fh.write(json.dumps(line) + "\\n"); fh.flush()
            if os.environ.get("FAKE_LOOP"):
                fc = {{"timestamp": "t", "type": "response_item", "payload": {{"type": "function_call",
                      "name": "shell", "arguments": json.dumps({{"command": ["pytest"]}}), "call_id": f"c{{i}}"}}}}
                fo = {{"timestamp": "t", "type": "response_item", "payload": {{"type": "function_call_output",
                      "call_id": f"c{{i}}", "output": json.dumps({{"output": "x", "metadata": {{"exit_code": 1}}}})}}}}
                fh.write(json.dumps(fc) + "\\n" + json.dumps(fo) + "\\n"); fh.flush()
            print("TURN", i, flush=True)
            time.sleep(0.15)
''')


@pytest.fixture
def fakebin(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, src in (("claude", FAKE_CLAUDE), ("codex", FAKE_CODEX)):
        p = bin_dir / name
        p.write_text(src.format(python=sys.executable))
        p.chmod(p.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("AGENTCAP_QUOTA_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    src = str(Path(__file__).resolve().parents[1] / "src")
    monkeypatch.setenv("PYTHONPATH", src + os.pathsep + os.environ.get("PYTHONPATH", ""))
    return bin_dir


def make_runner(argv, tmp_path, **kw):
    events = []
    err = io.StringIO()
    r = q.QuotaRunner(argv, quota=kw.pop("quota", 5.0), alerts=events.append, stderr=err,
                      poll_s=0.05, claude_grace_s=kw.pop("grace", 1.0), receipt_dir=tmp_path / "receipts",
                      cwd=tmp_path, home=tmp_path / "home", **kw)
    return r, events, err


def test_e2e_claude_denied_at_quota(fakebin, tmp_path):
    r, events, err = make_runner(["claude", "-p", "fix"], tmp_path)
    code = r.run()
    types = [e["type"] for e in events]
    assert code == 3
    assert "quota_threshold" in types and "quota_exceeded" in types and types[-1] == "quota_receipt"
    assert r.summary.share >= 5.0 and r.summary.share < 7
    assert "this job used" in err.getvalue() and "weekly quota" in err.getvalue()
    receipt = json.loads(next((tmp_path / "receipts").glob("*.json")).read_text())
    assert receipt["type"] == "agentcap_quota_receipt" and receipt["exit_code"] == 3
    assert (r.job_dir / "denies.log").exists()


def test_live_status_file_for_editor(fakebin, tmp_path, monkeypatch):
    live = tmp_path / "live.json"
    monkeypatch.setenv("AGENTCAP_QUOTA_LIVE", str(live))
    r, events, err = make_runner(["codex"], tmp_path)
    assert r.run() == 3
    data = json.loads(live.read_text())
    assert data["agent"] == "codex" and data["limit_hit"] is True and data["share_pct"] >= 5


def test_e2e_claude_sigint_when_model_ignores_stop(fakebin, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_IGNORE_STOP", "1")
    monkeypatch.setenv("FAKE_N", "200")
    r, events, err = make_runner(["claude", "-p", "fix"], tmp_path, grace=0.5)
    t0 = time.time()
    assert r.run() == 3
    assert time.time() - t0 < 20


def test_e2e_claude_under_quota_passes_through(fakebin, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_N", "3")
    r, events, err = make_runner(["claude"], tmp_path)
    assert r.run() == 0
    assert "quota_exceeded" not in [e["type"] for e in events]


def test_e2e_claude_api_key_login_warns(fakebin, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_APIKEY", "1")
    monkeypatch.setenv("FAKE_N", "2")
    r, events, err = make_runner(["claude"], tmp_path)
    assert r.run() == 0
    assert "no subscription rate_limits" in err.getvalue()


def test_e2e_free_observe_only_nudge(fakebin, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_N", "10")
    r, events, err = make_runner(["claude"], tmp_path, enforce=False)
    assert r.run() == 0
    assert "Pro's quota mode would have stopped this job at 5%" in err.getvalue()
    assert events == []


def test_e2e_codex_sigint_at_quota(fakebin, tmp_path):
    r, events, err = make_runner(["codex", "exec", "fix"], tmp_path)
    assert r.run() == 3
    assert r.summary.share >= 5.0 and r.summary.share < 7
    assert "quota_exceeded" in [e["type"] for e in events]


def test_e2e_codex_baseline_from_previous_rollout_and_parallel(fakebin, tmp_path, monkeypatch):
    d = tmp_path / "codex" / "sessions" / "2026" / "10" / "29"
    d.mkdir(parents=True)
    old = d / "rollout-old.jsonl"
    line = codex_line(secondary=38.0, resets_s=time.time() + 86400)
    old.write_text(json.dumps(line) + "\n")
    monkeypatch.setenv("FAKE_START", "39")  # first own reading 40 -> share 2 thanks to exact baseline
    monkeypatch.setenv("FAKE_N", "1")
    r, events, err = make_runner(["codex"], tmp_path)
    assert r.run() == 0
    assert r.summary.baseline_exact and r.summary.share == 2.0


def test_e2e_codex_window_reset_mid_job(fakebin, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_N", "6")
    monkeypatch.setenv("FAKE_STEP", "0.5")
    monkeypatch.setenv("FAKE_RESET_AT", "3")
    r, events, err = make_runner(["codex"], tmp_path)
    assert r.run() == 0
    # 40.5 -> 41.5 (1.0) then reset -> 0.5, 1.0, 1.5 (+1.5 carried logic) => 2.5
    assert r.summary.resets_seen == 1 and r.summary.share == pytest.approx(2.5)


def test_e2e_codex_parallel_session_warns(fakebin, tmp_path, monkeypatch):
    d = tmp_path / "codex" / "sessions" / "2026" / "10" / "29"
    d.mkdir(parents=True)
    other = d / "rollout-other.jsonl"
    other.write_text("")
    monkeypatch.setenv("FAKE_N", "8")
    r, events, err = make_runner(["codex"], tmp_path, quota=50)
    import threading

    def writer():
        time.sleep(0.4)
        with open(other, "a") as fh:
            fh.write(json.dumps(codex_line()) + "\n")
    threading.Thread(target=writer, daemon=True).start()
    assert r.run() == 0
    assert "another session" in err.getvalue()
    assert "quota_parallel" in [e["type"] for e in events]


# -------------------------------------------------------------------- CLI
def test_cli_quota_requires_supported_agent_and_no_cap(capsys):
    assert cli.main(["run", "--quota", "5%", "--", "gemini"]) == 2
    assert cli.main(["run", "--quota", "5%", "--cap", "1", "--", "claude"]) == 2
    assert "cannot be combined" in capsys.readouterr().err
    assert cli.main(["run", "--quota", "abc", "--", "claude"]) == 2


def test_cli_free_runs_observe_only(fakebin, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("FAKE_N", "8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    code = cli.main(["run", "--quota", "5%", "--", "claude"])
    err = capsys.readouterr().err
    assert code == 0 and "stopping is Pro" in err and "would have stopped" in err


def test_cli_pro_enforces(fakebin, tmp_path, monkeypatch, capsys):
    from agentcap.license import LicenseStatus
    monkeypatch.setattr(cli, "status_from_environment", lambda: LicenseStatus(True, "pro", "test"))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("AGENTCAP_RECEIPT_DIR", str(tmp_path / "rc"))
    assert cli.main(["run", "--quota", "5", "--window", "week", "--", "codex"]) == 3
    assert list((tmp_path / "rc").glob("*.json"))


def test_cli_quota_status_read_only(fakebin, tmp_path, monkeypatch, capsys):
    state = tmp_path / "state"
    state.mkdir(parents=True)
    (state / "claude_last.json").write_text(json.dumps(
        {"ts": time.time() - 5, "readings": {"week": {"used": 41.0, "resets_at": time.time() + 7200}}}))
    d = tmp_path / "codex" / "sessions" / "2026" / "10" / "30"
    d.mkdir(parents=True)
    (d / "rollout-a.jsonl").write_text(json.dumps(codex_line(resets_s=time.time() + 3600)) + "\n")
    before = sorted(p.stat().st_mtime for p in tmp_path.rglob("*") if p.is_file())
    assert cli.main(["quota"]) == 0
    out = capsys.readouterr().out
    assert "claude week: 41.0% used" in out and "codex week: 40.0% used" in out
    assert sorted(p.stat().st_mtime for p in tmp_path.rglob("*") if p.is_file()) == before


# ------------------------------------------------ loop breaker on subscriptions
def _hook(job, tool="Bash", cmd="npm test"):
    out = io.StringIO()
    q.hook_main(job, io.StringIO(json.dumps({"hook_event_name": "PreToolUse", "tool_name": tool,
                                             "tool_input": {"command": cmd}})), out)
    return json.loads(out.getvalue()) if out.getvalue().strip() else None


def test_hook_denies_fourth_identical_tool_call(tmp_path):
    (tmp_path / "job.json").write_text(json.dumps({"enforce": True, "quota": 5, "window": "week"}))
    assert _hook(tmp_path) is None
    assert _hook(tmp_path, cmd="ls") is None  # a different call resets the streak
    for _ in range(3):
        assert _hook(tmp_path) is None
    d = _hook(tmp_path)
    assert d["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "same tool call 4 times" in d["hookSpecificOutput"]["permissionDecisionReason"]
    assert "npm test" not in json.dumps(json.loads((tmp_path / "loop.json").read_text()))


def test_hook_free_records_but_never_denies_loops(tmp_path):
    (tmp_path / "job.json").write_text(json.dumps({"enforce": False, "quota": 5, "window": "week"}))
    for _ in range(5):
        assert _hook(tmp_path) is None
    assert json.loads((tmp_path / "loop.json").read_text())["count"] >= 4


def test_e2e_codex_loop_breaker_stops_subscription_run(fakebin, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_LOOP", "1")
    monkeypatch.setenv("FAKE_STEP", "0.01")
    r, events, err = make_runner(["codex", "exec", "fix"], tmp_path, quota=50.0)
    assert r.run() == 4
    assert r.summary.reason == "loop_detected"
    assert "loop_detected" in [e["type"] for e in events]
    assert "same tool call 4 times" in err.getvalue()


def test_e2e_codex_loop_free_nudge(fakebin, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_LOOP", "1")
    monkeypatch.setenv("FAKE_STEP", "0.01")
    monkeypatch.setenv("FAKE_N", "8")
    r, events, err = make_runner(["codex", "exec", "fix"], tmp_path, quota=50.0, enforce=False)
    assert r.run() == 0
    assert "Pro's loop breaker would have stopped this job at 4 repeats" in err.getvalue()


def test_e2e_claude_loop_breaker_denies_and_stops(fakebin, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_SAME", "1")
    monkeypatch.setenv("FAKE_STEP", "0.01")
    r, events, err = make_runner(["claude", "-p", "fix"], tmp_path, quota=50.0)
    assert r.run() == 4
    assert r.summary.reason == "loop_detected"
    assert "same tool call 4 times" in err.getvalue()


# ------------------------------------------- 0.2.1: interactive stop, approve
def test_e2e_interactive_claude_is_not_sigint_when_hook_denies(fakebin, tmp_path, monkeypatch):
    # interactive session: the hook denies and ends the turn; agentcap must not kill the session
    monkeypatch.setenv("FAKE_IGNORE_STOP", "1")
    monkeypatch.setenv("FAKE_N", "14")
    r, events, err = make_runner(["claude"], tmp_path, grace=0.2)
    sent = []
    monkeypatch.setattr(r, "_signal", lambda sig: sent.append(sig))
    assert r.run() == 3
    assert sent == []
    assert "session stays open" in err.getvalue()


def test_e2e_headless_claude_still_sigint(fakebin, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_IGNORE_STOP", "1")
    monkeypatch.setenv("FAKE_N", "200")
    r, events, err = make_runner(["claude", "--print", "fix"], tmp_path, grace=0.3)
    assert r.run() == 3


def test_is_headless_claude():
    assert q.is_headless_claude(["claude", "-p", "x"])
    assert q.is_headless_claude(["claude", "--print"])
    assert not q.is_headless_claude(["claude"])
    assert not q.is_headless_claude(["claude", "--resume"])


def _job(tmp_path, **kw):
    root = tmp_path / "state"
    d = root / "jobs" / "q1-1"
    d.mkdir(parents=True)
    job = {"enforce": True, "quota": 4.0, "window": "week", "started_at": 1}
    job.update(kw)
    (d / "job.json").write_text(json.dumps(job))
    (d / "status.json").write_text(json.dumps({"limit_hit": True, "share": 4.2, "quota": 4.0}))
    return root, d


def test_approve_raises_quota_50pct_at_most_twice(tmp_path):
    root, d = _job(tmp_path)
    (d / "loop.json").write_text("{}"); (d / "tools.json").write_text("{}")
    msg = q.approve_job(root)
    assert "6%" in msg and "1/2" in msg
    job = json.loads((d / "job.json").read_text())
    assert job["quota"] == 6.0 and job["approvals"] == 1
    assert json.loads((d / "status.json").read_text())["limit_hit"] is False
    assert not (d / "loop.json").exists() and not (d / "tools.json").exists()
    assert "9%" in q.approve_job(root)
    assert "already approved twice" in q.approve_job(root)
    assert json.loads((d / "job.json").read_text())["quota"] == 9.0


def test_approve_free_job_and_no_job(tmp_path):
    assert "no running" in q.approve_job(tmp_path / "none")
    root, d = _job(tmp_path, enforce=False)
    assert "Pro" in q.approve_job(root)


def test_approve_skips_finished_jobs(tmp_path):
    root, d = _job(tmp_path)
    (d / "done.json").write_text("{}")
    assert "no running" in q.approve_job(root)


def test_hook_records_last_tool_name_only(tmp_path):
    (tmp_path / "job.json").write_text(json.dumps({"enforce": True, "quota": 5, "window": "week"}))
    _hook(tmp_path, tool="Edit", cmd="secret-arg")
    t = json.loads((tmp_path / "tools.json").read_text())
    assert t["tool"] == "Edit" and "secret-arg" not in json.dumps(t)


def test_receipt_has_last_tool(fakebin, tmp_path):
    r, events, err = make_runner(["claude", "-p", "fix"], tmp_path)
    assert r.run() == 3
    receipt = json.loads(next((tmp_path / "receipts").glob("*.json")).read_text())
    assert receipt["last_tool"] == "Bash"
    assert "last tool call: Bash" in err.getvalue()


def test_runner_picks_up_approval(fakebin, tmp_path, monkeypatch):
    # approve while running: quota goes 5 -> 7.5, the job continues past 5%
    monkeypatch.setenv("FAKE_N", "7")
    r, events, err = make_runner(["claude", "-p", "fix"], tmp_path, quota=3.0)
    orig = r._live
    def live(share, observed, hit):
        if hit and not getattr(r, "_did", False):
            r._did = True
            q.approve_job(r.job_root)
        return orig(share, observed, hit)
    monkeypatch.setattr(r, "_live", live)
    code = r.run()
    assert "approved" in err.getvalue()
    assert r.summary.quota == 4.5


def test_cli_approve(tmp_path, monkeypatch, capsys):
    from agentcap import cli
    root, d = _job(tmp_path)
    monkeypatch.setenv("AGENTCAP_QUOTA_DIR", str(root))
    assert cli.main(["approve"]) == 0
    assert "6%" in capsys.readouterr().out


def test_approve_refuses_a_job_already_stopped_by_sigint(tmp_path):
    # real Codex run 30 Oct: approve after the SIGINT said "the run continues" but the job was ending
    root, d = _job(tmp_path, agent="codex", stopped=True)
    msg = q.approve_job(root)
    assert "already stopped" in msg and "continues" not in msg
    assert json.loads((d / "job.json").read_text())["quota"] == 4.0


def test_codex_stop_marks_job_stopped(fakebin, tmp_path):
    r, events, err = make_runner(["codex"], tmp_path)
    assert r.run() == 3
    assert json.loads((r.job_dir / "job.json").read_text())["stopped"] is True
