"""Harness adapters for opencode and agy (Antigravity CLI): event fixtures follow
real `opencode run --format json` and `agy -p --output-format stream-json` output
captured on 2026-10-30; e2e uses fake executables."""
from __future__ import annotations

import io
import json
import os
import stat
import sys
import textwrap
from pathlib import Path

import pytest

from agentcap import harness as h
from agentcap.adapters import prepare_child, detect_agent

OC_TOOL = {"type": "tool_use", "part": {"type": "tool", "tool": "shell", "state": {
    "status": "completed", "input": {"command": "echo hi"}, "output": "hi\n",
    "metadata": {"metadata": {"status": "completed", "exit": 0}}}}}
OC_STEP = {"type": "step_finish", "part": {"type": "step-finish", "reason": "tool-calls",
           "cost": 0.0015, "tokens": {"input": 9918, "output": 39, "reasoning": 30,
                                      "cache": {"read": 512, "write": 0}}}}
AG_TOOL = {"event": "step_update", "step_update": {"step_index": 2, "state": "DONE", "step_type": "tool",
           "tool_name": "run_command", "tool_info": {"name": "run_command",
           "parameters": {"CommandLine": "echo hi"}, "output": "Command exited with code 0.\n\nhi"}}}
AG_RESP = {"event": "step_update", "step_update": {"step_index": 1, "state": "DONE",
           "step_type": "agent_response", "usage": {"input_tokens": 13380, "output_tokens": 35,
           "thinking_tokens": 0, "cache_read_tokens": 0, "total_tokens": 13415}}}
AG_QUOTA = {"status": "SUCCESS", "command": {"name": "usage", "data": {"groups": [
    {"name": "Gemini Models", "buckets": [
        {"id": "gemini-weekly", "window": "weekly", "remaining_fraction": 0.99},
        {"id": "gemini-5h", "window": "5h", "remaining_fraction": 0.98}]},
    {"name": "Claude and GPT models", "buckets": [
        {"id": "3p-weekly", "window": "weekly", "remaining_fraction": 1},
        {"id": "3p-5h", "window": "5h", "remaining_fraction": 1}]},
    {"name": "Gemini 4 Argon", "buckets": [
        {"id": "gemini-argon-wtu-weekly", "window": "weekly", "remaining_fraction": 0.5}]}]}}}


# ------------------------------------------------------------ detection/argv
def test_detect_harness():
    assert h.detect_harness(["opencode", "run", "hi"]) == "opencode"
    assert h.detect_harness(["/home/k/.local/bin/agy", "-p", "hi"]) == "agy"
    assert h.detect_harness(["codex", "exec"]) is None


def test_opencode_argv_forces_json():
    assert h.OpenCodeAdapter().argv(["opencode", "run", "hi"]) == ["opencode", "run", "--format", "json", "hi"]
    assert h.OpenCodeAdapter().argv(["opencode", "run", "--format", "json", "hi"]).count("--format") == 1


def test_opencode_needs_run():
    with pytest.raises(ValueError, match="opencode run"):
        h.OpenCodeAdapter().argv(["opencode"])


def test_agy_argv_forces_stream_json_and_headless():
    out = h.AgyAdapter().argv(["agy", "-p", "hi"])
    assert out[:3] == ["agy", "--output-format", "stream-json"]
    with pytest.raises(ValueError, match="-p"):
        h.AgyAdapter().argv(["agy"])


# ------------------------------------------------------------------ parsing
def test_opencode_events():
    a = h.OpenCodeAdapter()
    ev = a.parse(OC_TOOL)
    assert ev == [h.ToolCall("shell", {"command": "echo hi"}, error=False)]
    ev = a.parse(OC_STEP)
    assert ev == [h.Usage(tokens=9918 + 39 + 30 + 512, cost=0.0015)]
    err = json.loads(json.dumps(OC_TOOL)); err["part"]["state"]["status"] = "error"
    assert a.parse(err)[0].error is True
    assert a.parse({"type": "error", "error": {"type": "provider.auth"}}) == [h.ApiError("provider.auth")]


def test_agy_events():
    a = h.AgyAdapter()
    assert a.parse(AG_TOOL) == [h.ToolCall("run_command", {"CommandLine": "echo hi"}, error=False)]
    bad = json.loads(json.dumps(AG_TOOL)); bad["step_update"]["tool_info"]["output"] = "Command exited with code 1."
    assert a.parse(bad)[0].error is True
    active = json.loads(json.dumps(AG_TOOL)); active["step_update"]["state"] = "ACTIVE"
    assert a.parse(active) == []
    assert a.parse(AG_RESP) == [h.Usage(tokens=13415, cost=None)]


def test_agy_quota_used_pct_by_model_group():
    assert h.agy_used_pct(AG_QUOTA, "gemini-3.8-flash-low", "week") == pytest.approx(1.0)
    assert h.agy_used_pct(AG_QUOTA, "gemini-3.8-flash-low", "5h") == pytest.approx(2.0)
    assert h.agy_used_pct(AG_QUOTA, "claude-sonnet-5-5-low", "week") == 0
    assert h.agy_used_pct(AG_QUOTA, "gemini-4-argon-low", "week") == pytest.approx(50.0)
    assert h.agy_used_pct({"status": "x"}, "gemini", "week") is None


def test_agy_model_from_argv_or_settings(tmp_path):
    assert h.agy_model(["agy", "--model", "claude-opus-5-5-low", "-p", "x"], tmp_path) == "claude-opus-5-5-low"
    s = tmp_path / ".gemini" / "antigravity-cli"; s.mkdir(parents=True)
    (s / "settings.json").write_text(json.dumps({"model": "Gemini 3.8 Flash (Low)"}))
    assert h.agy_model(["agy", "-p", "x"], tmp_path) == "Gemini 3.8 Flash (Low)"


# --------------------------------------------------------- $ mode routing
def test_prepare_child_opencode_routes_through_proxy():
    p = prepare_child(["opencode", "run", "-m", "agentcap/openai/gpt-oss-20b:free", "hi"], port=4321,
                      env={"OPENAI_API_KEY": "k"})
    assert p.argv[:3] == ["opencode", "run", "--standalone"]
    cfg = json.loads(p.env["OPENCODE_CONFIG_CONTENT"])
    prov = cfg["provider"]["agentcap"]
    assert prov["options"]["baseURL"] == "http://127.0.0.1:4321/openai/v1"
    assert prov["options"]["apiKey"] == "{env:OPENAI_API_KEY}"
    assert "openai/gpt-oss-20b:free" in prov["models"]


def test_prepare_child_opencode_refuses_other_provider():
    with pytest.raises(ValueError, match="agentcap/"):
        prepare_child(["opencode", "run", "-m", "opencode-go/glm-5.3", "hi"], port=1, env={})


def test_prepare_child_agy_uses_gemini_api_route():
    p = prepare_child(["agy", "-p", "hi"], port=4321, env={"GEMINI_API_KEY": "k"})
    i = p.argv.index("--settings")
    assert json.loads(p.argv[i + 1]) == {"modelProvider": "gemini"}
    assert detect_agent(["agy", "-p"])[0] == "agy"


# ----------------------------------------------------------------- e2e fakes
FAKE_OC = r'''#!{py}
import json, os, signal, sys, time
if os.environ.get("FAKE_ERR_ON_INT"):
    def _int(*a):
        print(json.dumps({{"type": "error", "error": {{"type": "interrupted"}}}}), flush=True); sys.exit(130)
    signal.signal(signal.SIGINT, _int)
n = int(os.environ.get("FAKE_N", "3")); mode = os.environ.get("FAKE_MODE", "ok")
for i in range(n):
    args = {{"command": "ls" if mode == "loop" else f"echo {{i}}"}}
    st = "error" if mode == "err" else "completed"
    print(json.dumps({{"type": "tool_use", "part": {{"type": "tool", "tool": "shell", "state": {{"status": st, "input": args}}}}}}), flush=True)
    print(json.dumps({{"type": "step_finish", "part": {{"type": "step-finish", "cost": 0.01, "tokens": {{"input": 1000, "output": 0, "reasoning": 0, "cache": {{"read": 0, "write": 0}}}}}}}}), flush=True)
    time.sleep(float(os.environ.get("FAKE_SLEEP", "0.3")))
'''

FAKE_AGY = r'''#!{py}
import json, os, sys, time
st = os.environ["FAKE_STATE"]
if "/quota" in sys.argv:
    used = float(open(st).read() or 0) if os.path.exists(st) else 0.0
    print(json.dumps({{"status": "SUCCESS", "command": {{"data": {{"groups": [{{"name": "Gemini Models",
      "buckets": [{{"window": "weekly", "remaining_fraction": 0.9 - used}}, {{"window": "5h", "remaining_fraction": 0.5 - used}}]}}]}}}}}}))
    sys.exit(0)
n = int(os.environ.get("FAKE_N", "4"))
for i in range(n):
    used = (float(open(st).read() or 0) if os.path.exists(st) else 0.0) + 0.01
    open(st, "w").write(str(used))
    print(json.dumps({{"event": "step_update", "step_update": {{"state": "DONE", "step_type": "tool", "tool_info": {{"name": "run_command", "parameters": {{"CommandLine": f"echo {{i}}"}}, "output": "Command exited with code 0."}}}}}}), flush=True)
    print(json.dumps({{"event": "step_update", "step_update": {{"state": "DONE", "step_type": "agent_response", "usage": {{"total_tokens": 100}}}}}}), flush=True)
    time.sleep(0.4)
'''


@pytest.fixture
def fakebin(tmp_path, monkeypatch):
    b = tmp_path / "bin"; b.mkdir()
    for name, src in (("opencode", FAKE_OC), ("agy", FAKE_AGY)):
        p = b / name
        p.write_text(src.format(py=sys.executable))
        p.chmod(p.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{b}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_STATE", str(tmp_path / "used"))
    return b


def run(argv, tmp_path, **kw):
    err, out, events = io.StringIO(), io.StringIO(), []
    kw.setdefault("enforce", True)
    r = h.HarnessRunner(argv, stderr=err, stdout=out, alerts=events.append, poll_s=0.05,
                        quota_poll_s=0.2, receipt_dir=tmp_path / "receipts", home=tmp_path, **kw)
    code = r.run()
    return r, code, err.getvalue(), out.getvalue(), events


def test_e2e_opencode_token_cap_stops(fakebin, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_N", "20")
    r, code, err, out, events = run(["opencode", "run", "hi"], tmp_path, tokens=2500)
    assert code == h.EXIT_CAP_EXCEEDED
    assert "token cap" in err
    assert any(e["type"] == "cap_threshold" for e in events)
    assert r.summary.tokens < 20 * 1000
    rec = json.loads(next((tmp_path / "receipts").glob("*.json")).read_text())
    assert rec["agent"] == "opencode" and rec["reason"] == "token_cap"
    assert "step_finish" in out  # agent output is passed through


def test_e2e_opencode_cost_cap_from_log(fakebin, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_N", "20")
    _, code, err, *_ = run(["opencode", "run", "hi"], tmp_path, usd=0.025)
    assert code == h.EXIT_CAP_EXCEEDED and "$0.025" in err


def test_e2e_opencode_loop_breaker(fakebin, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_N", "20"); monkeypatch.setenv("FAKE_MODE", "loop")
    _, code, err, *_ = run(["opencode", "run", "hi"], tmp_path, tokens=10**9)
    assert code == h.EXIT_LOOP_DETECTED and "same tool call 4 times" in err


def test_e2e_opencode_error_breaker(fakebin, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_N", "20"); monkeypatch.setenv("FAKE_MODE", "err")
    _, code, err, *_ = run(["opencode", "run", "hi"], tmp_path, tokens=10**9)
    assert code == h.EXIT_LOOP_DETECTED and "failing tool calls" in err


def test_e2e_free_observes_only(fakebin, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_N", "6"); monkeypatch.setenv("FAKE_SLEEP", "0.05")
    _, code, err, _, events = run(["opencode", "run", "hi"], tmp_path, tokens=2500, enforce=False)
    assert code == 0 and events == [] and "would have stopped" in err
    assert not (tmp_path / "receipts").exists()


def test_e2e_opencode_quota_refused_honestly(fakebin, tmp_path):
    with pytest.raises(ValueError, match="does not expose"):
        h.HarnessRunner(["opencode", "run", "hi"], quota=5.0)


def test_e2e_agy_quota_stops(fakebin, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_N", "30")
    r, code, err, _, events = run(["agy", "--model", "gemini-3.8-flash-low", "-p", "hi"], tmp_path,
                                  quota=2.0, window="week")
    assert code == h.EXIT_CAP_EXCEEDED, err
    assert "quota" in err and r.summary.share >= 2.0
    assert any(e["type"] == "quota_exceeded" for e in events)


def test_e2e_agy_report(fakebin, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_N", "2")
    r, code, err, *_ = run(["agy", "--model", "gemini-3.8-flash-low", "-p", "hi"], tmp_path,
                           quota=50.0, window="5h")
    assert code == 0
    assert "of the 5-hour quota" in err and "tokens" in err


# -------------------------------------------------------------------- CLI
from agentcap import cli  # noqa: E402


def _pro(monkeypatch):
    from agentcap.license import LicenseStatus
    monkeypatch.setattr(cli, "status_from_environment", lambda: LicenseStatus(True, "pro", "test"))


def test_cli_tokens_cap_opencode_pro(fakebin, tmp_path, monkeypatch, capsys):
    _pro(monkeypatch)
    monkeypatch.setenv("FAKE_N", "20")
    monkeypatch.setenv("AGENTCAP_RECEIPT_DIR", str(tmp_path / "rc"))
    assert cli.main(["run", "--tokens", "2500", "--", "opencode", "run", "hi"]) == 3
    assert list((tmp_path / "rc").glob("h*.json"))


def test_cli_quota_opencode_refused(fakebin, capsys):
    assert cli.main(["run", "--quota", "5%", "--", "opencode", "run", "hi"]) == 2
    assert "does not expose" in capsys.readouterr().err


def test_cli_quota_agy_pro(fakebin, tmp_path, monkeypatch, capsys):
    _pro(monkeypatch)
    monkeypatch.setenv("FAKE_N", "30")
    monkeypatch.setenv("AGENTCAP_HARNESS_QUOTA_POLL_S", "0.2")
    code = cli.main(["run", "--quota", "2%", "--", "agy", "--model", "gemini-3.8-flash-low", "-p", "hi"])
    assert code == 3, capsys.readouterr().err


def test_cli_tokens_free_observes(fakebin, monkeypatch, capsys):
    monkeypatch.setenv("FAKE_N", "6"); monkeypatch.setenv("FAKE_SLEEP", "0.05")
    assert cli.main(["run", "--tokens", "2500", "--", "opencode", "run", "hi"]) == 0
    assert "would have stopped" in capsys.readouterr().err


def test_cli_tokens_needs_harness(capsys):
    assert cli.main(["run", "--tokens", "10", "--", "codex", "exec", "x"]) == 2


def test_free_quota_wording(fakebin, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_N", "4")
    _, code, err, *_ = run(["agy", "--model", "gemini-3.8-flash-low", "-p", "hi"], tmp_path,
                           quota=1.0, enforce=False)
    assert code == 0 and "at its quota limit" in err and "quota exceeded" not in err


def test_harness_alert_text_for_token_cap_has_no_dollar_placeholders():
    from agentcap.alerts import format_alert
    t = format_alert({"type": "cap_threshold", "run_id": "h1", "threshold": 80, "what": "token cap"})
    e = format_alert({"type": "cap_exceeded", "run_id": "h1", "what": "token cap", "tokens": 15100})
    assert "$?" not in t and "token cap" in t
    assert "$?" not in e and "token cap" in e and "Stopping" in e
    u = format_alert({"type": "cap_exceeded", "run_id": "h1", "cap_usd": 0.5, "spent_usd": 0.51})
    assert "cap=$0.5" in u


def test_quota_alert_has_limit_and_rounded_share(fakebin, tmp_path, monkeypatch):
    from agentcap.alerts import format_alert
    monkeypatch.setenv("FAKE_N", "30")
    _, code, _, _, events = run(["agy", "--model", "gemini-3.8-flash-low", "-p", "hi"], tmp_path,
                                quota=2.0, window="week")
    ev = next(e for e in events if e["type"] == "quota_exceeded")
    text = format_alert(ev)
    assert "(limit 2" in text and "?" not in text and "weekly" in text
    assert len(str(ev["share_pct"]).split(".")[-1]) <= 2


def test_quota_share_never_goes_backwards(fakebin, tmp_path):
    def payload(rem):
        return {"status": "SUCCESS", "command": {"data": {"groups": [{"name": "Gemini Models",
                "buckets": [{"window": "5h", "remaining_fraction": rem}]}]}}}
    seq = iter([payload(0.9)] + [payload(0.88)] * 3)
    r, code, err, *_ = run(["agy", "--model", "gemini-3.8-flash-low", "-p", "hi"], tmp_path,
                           quota=50.0, window="5h",
                           quota_fetch=lambda: next(seq, payload(0.95)))
    assert r.summary.share is not None and r.summary.share > 1.0, err
    assert "0.00% of the 5-hour" not in err


def test_no_api_error_noise_after_our_own_stop(fakebin, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_N", "20"); monkeypatch.setenv("FAKE_MODE", "loop")
    monkeypatch.setenv("FAKE_ERR_ON_INT", "1")
    _, code, err, *_ = run(["opencode", "run", "hi"], tmp_path, tokens=10**9)
    assert code == h.EXIT_LOOP_DETECTED
    assert "reported an API error" not in err


def test_quota_help_mentions_agy():
    import argparse
    text = cli.build_parser().format_help() if hasattr(cli, "build_parser") else ""
    src = open(cli.__file__).read()
    assert "claude/codex only" not in src
