from __future__ import annotations

import io
import json
from pathlib import Path

import httpx
import pytest

from agentcap import adapters
from agentcap.adapters import AdapterConflict, claude_conflicts, detect_agent, ensure_claude_routing_is_safe, prepare_child
from agentcap.alerts import AlertDispatcher, format_alert
from agentcap.license import FREE_FEATURES, PRO_FEATURES, features
from agentcap.upstreams import UpstreamConfigError, resolve_upstreams


@pytest.mark.parametrize("argv,expected", [
    (["claude", "-p", "hi"], ("claude", 1)),
    (["/usr/local/bin/claude"], ("claude", 1)),
    (["npx", "@anthropic-ai/claude-code", "-p", "x"], ("claude", 2)),
    (["npx", "-y", "@anthropic-ai/claude-code@2.1.0"], ("claude", 3)),
    (["claude-other"], None),
])
def test_detect_claude(argv, expected):
    assert detect_agent(argv) == expected


def test_prepare_child_sets_base_url_and_drops_bypass(tmp_path):
    env = {"HOME": str(tmp_path), "CLAUDE_CODE_USE_BEDROCK": "0", "ANTHROPIC_BASE_URL": "https://evil"}
    p = prepare_child(["claude", "-p", "hi"], port=4321, env=env)
    assert p.argv == ["claude", "-p", "hi"]
    assert p.env["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:4321/anthropic"
    assert "CLAUDE_CODE_USE_BEDROCK" not in p.env
    assert env["ANTHROPIC_BASE_URL"] == "https://evil"  # caller env untouched


@pytest.mark.parametrize("var", adapters.CLAUDE_BYPASS_ENV)
def test_bedrock_vertex_foundry_refused(var, tmp_path):
    with pytest.raises(AdapterConflict) as exc:
        ensure_claude_routing_is_safe(["claude"], {"HOME": str(tmp_path), var: "1"}, cwd=tmp_path)
    assert var in str(exc.value)


def test_falsy_bypass_values_allowed(tmp_path):
    assert claude_conflicts(["claude"], {"HOME": str(tmp_path), "CLAUDE_CODE_USE_VERTEX": "false"}, cwd=tmp_path) == []


def test_settings_env_base_url_refused_without_printing_value(tmp_path):
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "settings.json").write_text(json.dumps({"env": {"ANTHROPIC_BASE_URL": "https://secret-host"}}))
    found = claude_conflicts(["claude"], {"HOME": str(tmp_path)}, cwd=tmp_path / "nowhere")
    assert found and "secret-host" not in " ".join(found)


def test_project_local_settings_checked(tmp_path):
    proj = tmp_path / "proj"
    (proj / ".claude").mkdir(parents=True)
    (proj / ".claude" / "settings.local.json").write_text(json.dumps({"env": {"CLAUDE_CODE_USE_BEDROCK": "1"}}))
    assert claude_conflicts(["claude"], {"HOME": str(tmp_path)}, cwd=proj)


def test_broken_settings_ignored_and_other_clis_unaffected(tmp_path):
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "settings.json").write_text("{nope")
    assert claude_conflicts(["claude"], {"HOME": str(tmp_path)}, cwd=tmp_path) == []
    assert claude_conflicts(["codex"], {"CLAUDE_CODE_USE_BEDROCK": "1"}) == []


def test_anthropic_upstream_override_validated():
    o = resolve_upstreams({"AGENTCAP_ANTHROPIC_UPSTREAM": "http://127.0.0.1:9000"})
    assert o[0].provider == "anthropic"
    with pytest.raises(UpstreamConfigError):
        resolve_upstreams({"AGENTCAP_ANTHROPIC_UPSTREAM": "http://example.com"})


# ------------------------------------------------------------------ tiers
def test_run_cap_is_free_and_pro_is_unattended_pack():
    assert "run_cap" in FREE_FEATURES
    assert not ({"hour_cap", "day_cap"} & FREE_FEATURES)
    assert {"hour_cap", "day_cap"} <= PRO_FEATURES
    assert not ({"loop_detection", "webhook_alerts", "telegram_alerts", "spend_receipt"} & FREE_FEATURES)
    assert {"loop_detection", "webhook_alerts", "telegram_alerts", "spend_receipt"} <= PRO_FEATURES
    assert features("bogus") == set(FREE_FEATURES)


def test_cli_hour_and_day_caps_need_pro(monkeypatch):
    from agentcap import cli
    calls = {}

    class FakeRunner:
        def __init__(self, argv, **kw):
            calls.update(kw)

        def run(self):
            return 0
    monkeypatch.setattr(cli, "Runner", FakeRunner)
    monkeypatch.setattr(cli, "status_from_environment", lambda: type("S", (), {"tier": "free"})())
    parser = cli.build_parser() if hasattr(cli, "build_parser") else cli._build_parser()
    args = parser.parse_args(["run", "--cap", "1", "--hour-cap", "2", "--day-cap", "3", "--", "claude", "-p", "x"])
    assert cli._run(args) == 2 and not calls
    monkeypatch.setattr(cli, "status_from_environment", lambda: type("S", (), {"tier": "pro"})())
    assert cli._run(args) == 0
    assert str(calls["hour_cap"]) == "2" and str(calls["day_cap"]) == "3"


# ----------------------------------------------------------------- alerts
def test_loop_nudge_line_text_and_terminal_only():
    sent = []
    err = io.StringIO()
    d = AlertDispatcher(features=PRO_FEATURES, webhook_url="https://hook.example/x",
                        telegram_token="123:ABCSECRET", telegram_chat_id="42",
                        transport=httpx.MockTransport(lambda r: sent.append(r) or httpx.Response(200)),
                        stderr=err, background=False)
    d.emit({"type": "loop_nudge", "spent_usd": "3.456", "threshold": 5, "window_s": 120, "run_id": "r"})
    d.emit({"type": "loop_nudge", "spent_usd": "9", "run_id": "r"})
    out = err.getvalue()
    assert "Pro's loop breaker would have stopped this at $3.46" in out
    assert "https://agentcap.launchbench.studio/#pricing" in out
    assert out.count("loop breaker") == 1 and sent == []


def test_receipt_goes_to_telegram_and_webhook_not_terminal_and_token_never_printed():
    sent = []
    err = io.StringIO()
    d = AlertDispatcher(features=PRO_FEATURES, webhook_url="https://hook.example/x",
                        telegram_token="123:ABCSECRET", telegram_chat_id="42",
                        transport=httpx.MockTransport(lambda r: sent.append(r) or httpx.Response(200)),
                        stderr=err, background=False)
    d.emit({"type": "run_receipt", "run_id": "r", "spent_usd": "1.5", "cap_usd": "5", "requests": 9,
            "reason": "child_exit", "exit_code": 0, "prompt": "PRIVATE"})
    assert err.getvalue() == ""
    hosts = sorted(r.url.host for r in sent)
    assert hosts == ["api.telegram.org", "hook.example"]
    hook = next(r for r in sent if r.url.host == "hook.example")
    payload = json.loads(hook.content)
    assert payload["event"]["requests"] == 9 and "prompt" not in payload["event"]
    tg = json.loads(next(r for r in sent if r.url.host == "api.telegram.org").content)
    assert "spent $1.5000 of $5 cap, 9 requests" in tg["text"]


def test_failed_telegram_delivery_does_not_leak_token():
    err = io.StringIO()

    def boom(request):
        raise httpx.ConnectError(f"failed {request.url}")
    d = AlertDispatcher(features=PRO_FEATURES, telegram_token="123:ABCSECRET", telegram_chat_id="42",
                        transport=httpx.MockTransport(boom), stderr=err, background=False)
    d.emit({"type": "cap_exceeded", "run_id": "r"})
    assert "ABCSECRET" not in err.getvalue() and "telegram" in err.getvalue()


def test_free_tier_never_sends_remote_alerts():
    sent = []
    d = AlertDispatcher(features=FREE_FEATURES, webhook_url="https://hook.example/x",
                        telegram_token="t", telegram_chat_id="c",
                        transport=httpx.MockTransport(lambda r: sent.append(r) or httpx.Response(200)),
                        stderr=io.StringIO(), background=False)
    d.emit({"type": "run_receipt", "run_id": "r"})
    d.emit({"type": "cap_exceeded", "run_id": "r"})
    assert sent == []


def test_format_receipt_without_cap():
    assert "of $" not in format_alert({"type": "run_receipt", "spent_usd": "1", "cap_usd": None})
