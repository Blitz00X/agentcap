"""Alert fan-out: terminal output, mocked webhook/Telegram, redaction."""

from __future__ import annotations

import asyncio
import io
import json
from decimal import Decimal

import httpx

from agentcap.alerts import AlertDispatcher, redact, sanitize
from agentcap.ledger import Caps
from agentcap.license import features

from proxy_support import Upstream, build, openai_chat_body, openai_chat_response, proxy_client


class Capture:
    def __init__(self, *, fail: bool = False) -> None:
        self.requests: list[httpx.Request] = []
        self.fail = fail

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.fail:
            raise httpx.ConnectError("upstream refused", request=request)
        return httpx.Response(200, json={"ok": True})

    def payloads(self) -> list[dict]:
        return [json.loads(request.content) for request in self.requests]


def test_terminal_alert_is_red_and_on_stderr():
    stderr = io.StringIO()
    dispatcher = AlertDispatcher(features=features("free"), stderr=stderr, background=False)
    dispatcher.emit(
        {
            "type": "cap_exceeded",
            "scope": "run",
            "cap_usd": "0.50",
            "spent_usd": "0.50",
        }
    )
    output = stderr.getvalue()
    assert "\x1b[31m" in output
    assert "RUN CAP HIT" in output
    assert "Stopping the agent" in output


def test_cap_threshold_alert_reports_percentage():
    stderr = io.StringIO()
    dispatcher = AlertDispatcher(features=features("free"), stderr=stderr, background=False)
    for threshold in (50, 80, 100):
        dispatcher.emit(
            {
                "type": "cap_threshold",
                "threshold": threshold,
                "spent_usd": "0.50",
                "cap_usd": "1.00",
            }
        )
    output = stderr.getvalue()
    for threshold in (50, 80, 100):
        assert f"{threshold}% of the $1.00 run cap" in output


def test_webhook_and_telegram_use_mocked_transport_only_for_pro():
    capture = Capture()
    stderr = io.StringIO()
    dispatcher = AlertDispatcher(
        features=features("pro"),
        webhook_url="https://hooks.invalid/agentcap",
        telegram_token="tok-SECRET",
        telegram_chat_id="chat-1",
        transport=httpx.MockTransport(capture),
        stderr=stderr,
        background=False,
        clock=lambda: __import__("datetime").datetime(
            2026, 10, 25, 12, 0, tzinfo=__import__("datetime").timezone.utc
        ),
    )
    dispatcher.emit(
        {
            "type": "cap_exceeded",
            "run_id": "run-1",
            "scope": "run",
            "cap_usd": "1.00",
            "spent_usd": "1.00",
        }
    )

    assert len(capture.requests) == 2
    webhook, telegram = capture.requests
    assert str(webhook.url) == "https://hooks.invalid/agentcap"
    assert "sendMessage" in str(telegram.url)
    assert "chat_id" not in str(webhook.url)

    payload = json.loads(webhook.content)
    assert payload["source"] == "agentcap"
    assert payload["event"]["type"] == "cap_exceeded"
    assert payload["sent_at"] == "2026-10-25T12:00:00+00:00"

    telegram_payload = json.loads(telegram.content)
    assert telegram_payload["chat_id"] == "chat-1"
    assert "RUN CAP HIT" in telegram_payload["text"]


def test_free_tier_never_posts_to_webhook_or_telegram():
    capture = Capture()
    dispatcher = AlertDispatcher(
        features=features("free"),
        webhook_url="https://hooks.invalid/agentcap",
        telegram_token="tok",
        telegram_chat_id="chat-1",
        transport=httpx.MockTransport(capture),
        stderr=io.StringIO(),
        background=False,
    )
    dispatcher.emit({"type": "usage_missing", "charged_usd": "0.01"})
    assert capture.requests == []


def test_delivery_failure_is_swallowed_with_warning():
    stderr = io.StringIO()
    dispatcher = AlertDispatcher(
        features=features("pro"),
        webhook_url="https://hooks.invalid/agentcap",
        transport=httpx.MockTransport(Capture(fail=True)),
        stderr=stderr,
        background=False,
    )
    dispatcher.emit({"type": "cap_exceeded", "cap_usd": "1", "spent_usd": "1"})
    output = stderr.getvalue()
    assert "alert delivery failed (webhook" in output
    assert "hooks.invalid" not in output


def test_background_delivery_can_be_waited_for():
    capture = Capture()
    dispatcher = AlertDispatcher(
        features=features("pro"),
        webhook_url="https://hooks.invalid/agentcap",
        transport=httpx.MockTransport(capture),
        stderr=io.StringIO(),
    )
    dispatcher.emit({"type": "loop_detected", "threshold": 5, "window_s": 120})
    dispatcher.wait(timeout=5.0)
    assert len(capture.requests) == 1


def test_sanitize_and_redact_drop_secrets_and_prompts():
    payload = {
        "type": "cap_exceeded",
        "model": "sk-SECRET123",
        "authorization": "Bearer sk-SECRET123",
        "prompt": "do not leak this",
        "api_key": "sk-SECRET123",
        "message": "Authorization: Bearer sk-SECRET123",
    }
    clean = sanitize(payload)
    assert set(clean) == {"type", "model"}
    assert "SECRET123" not in json.dumps(clean)
    assert "do not leak this" not in json.dumps(clean)
    assert "SECRET123" not in redact("key sk-SECRET123 leaked")


def test_alerts_integrated_with_proxy_never_leak_keys(tmp_path):
    stderr = io.StringIO()
    capture = Capture()
    dispatcher = AlertDispatcher(
        features=features("pro"),
        webhook_url="https://hooks.invalid/agentcap",
        telegram_token="tok-SECRET",
        telegram_chat_id="chat-1",
        transport=httpx.MockTransport(capture),
        stderr=stderr,
        background=False,
    )
    upstream = Upstream(
        lambda _: openai_chat_response(prompt_tokens=100_000, completion_tokens=100_000)
    )
    app, ledger, _ = build(
        tmp_path,
        upstream,
        caps=Caps(run_usd=Decimal("0.01")),
        features=features("pro"),
        events=dispatcher.emit,
    )

    async def scenario():
        async with proxy_client(app) as client:
            first = await client.post(
                "/openai/v1/chat/completions",
                content=json.dumps(
                    openai_chat_body(model="gpt-4o", max_completion_tokens=1)
                ).encode(),
                headers={"Authorization": "Bearer sk-SECRET123"},
            )
            second = await client.post(
                "/openai/v1/chat/completions",
                content=json.dumps(
                    openai_chat_body(model="gpt-4o", max_completion_tokens=1)
                ).encode(),
                headers={"Authorization": "Bearer sk-SECRET123"},
            )
            return first, second

    first, second = asyncio.run(scenario())
    assert first.status_code == 200
    assert second.status_code == 429
    assert capture.requests

    serialized = json.dumps([payload for payload in capture.payloads()])
    assert "SECRET123" not in serialized
    assert "SECRET123" not in stderr.getvalue()
    assert "do not leak" not in serialized

    for path in tmp_path.glob("ledger.db*"):
        assert b"SECRET123" not in path.read_bytes()

    assert ledger.summary("run-1")["reservations"] == 1
