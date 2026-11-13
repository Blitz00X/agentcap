"""F8: upstream auth/payment rejections are reported plainly, not as a loop.

Real run (2026-11-05): Codex resent a request that OpenRouter answered with
HTTP 401 ("User not found."); the fourth identical resend tripped the loop
breaker and the user was told the agent looped.  Mocked upstreams only.
"""

from __future__ import annotations

import io
import json
import sys
import time
from decimal import Decimal
from typing import Any

import httpx
import pytest

from agentcap.alerts import AlertDispatcher, format_alert
from agentcap.devtools.fake_upstream import create_app as fake_app
from agentcap.ledger import Caps, Ledger
from agentcap.license import FREE_FEATURES, features
from agentcap.proxy import (
    AUTH_REJECTED_STATUSES,
    ProxyConfig,
    create_app,
    upstream_error_message,
)
from agentcap.runner import EXIT_UPSTREAM_AUTH_FAILED, Runner

PRO = features("pro")
OPENROUTER_401 = {"error": {"message": "User not found.", "code": 401}}


class Proxy:
    """A proxy wired to a mocked upstream; records kills and events."""

    def __init__(self, tmp_path, transport, *, feature_set=PRO, upstream="https://openrouter.ai/api"):
        self.events: list[dict[str, Any]] = []
        self.auth_kills: list[dict[str, Any]] = []
        self.loop_kills: list[dict[str, Any]] = []
        self.cap_kills: list[dict[str, Any]] = []
        config = ProxyConfig(
            run_id="run-f8",
            caps=Caps(run_usd=Decimal("0.50")),
            features=feature_set,
            openai_upstream=upstream,
            on_cap_exceeded=self.cap_kills.append,
            on_loop_detected=self.loop_kills.append,
            on_upstream_auth_failed=self.auth_kills.append,
        )
        self.app = create_app(
            config,
            Ledger(tmp_path / f"f8-{time.time_ns()}.db"),
            upstream_transport=transport,
            events=self.events.append,
        )
        self.proxy = self.app.state.proxy

    async def post(self, path: str, body: dict[str, Any], headers: dict[str, str] | None = None):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app), base_url="http://proxy"
        ) as client:
            return await client.post(path, content=json.dumps(body), headers=headers or {})

    def types(self) -> list[str]:
        return [event.get("type") for event in self.events]


class Counting:
    def __init__(self, responder) -> None:
        self.responder = responder
        self.calls = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        return self.responder(request)


def _always(status: int, payload: Any):
    return Counting(lambda _r: httpx.Response(status, json=payload))


CHAT = {"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "same"}]}


# --------------------------------------------------------------- reproduction
async def test_repeated_401_resends_are_not_a_runaway_loop(tmp_path):
    upstream = _always(401, OPENROUTER_401)
    proxy = Proxy(tmp_path, httpx.MockTransport(upstream))

    statuses = [
        (await proxy.post("/openai/v1/chat/completions", CHAT)).status_code
        for _ in range(4)
    ]

    # Every resend reached the upstream and got its 401 back; none was
    # blocked by AgentCap as a loop.
    assert statuses == [401, 401, 401, 401]
    assert upstream.calls == 4
    assert proxy.loop_kills == []
    assert "loop_detected" not in proxy.types()
    # Stopped once, with its own reason, at the third consecutive rejection.
    assert len(proxy.auth_kills) == 1
    kill = proxy.auth_kills[0]
    assert kill["reason"] == "upstream_auth_failed"
    assert kill["http_status"] == 401
    assert kill["count"] == 3
    assert proxy.proxy.auth_reject_streak == 4


async def test_rejection_event_reads_plainly(tmp_path):
    proxy = Proxy(tmp_path, httpx.MockTransport(_always(401, OPENROUTER_401)))
    await proxy.post("/openai/v1/chat/completions", CHAT)

    rejected = [e for e in proxy.events if e["type"] == "upstream_rejected"]
    assert len(rejected) == 1
    event = rejected[0]
    assert event["http_status"] == 401
    assert event["upstream_host"] == "openrouter.ai"
    assert event["detail"] == "User not found."
    text = format_alert({**event, "run_id": "run-f8"})
    assert text == (
        "agentcap [run-f8]: upstream rejected the request (HTTP 401: User not found.) "
        "— check the API key for openrouter.ai; nothing was spent."
    )


async def test_one_401_then_success_stops_nothing(tmp_path):
    answers = iter([401, 200, 401, 401, 200, 401, 401])

    def responder(_request):
        status = next(answers)
        if status == 200:
            return httpx.Response(
                200,
                json={"choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": 1}},
            )
        return httpx.Response(401, json=OPENROUTER_401)

    proxy = Proxy(tmp_path, httpx.MockTransport(responder))
    for i in range(7):
        body = {**CHAT, "messages": [{"role": "user", "content": f"turn {i}"}]}
        await proxy.post("/openai/v1/chat/completions", body)

    assert proxy.auth_kills == []
    assert proxy.loop_kills == []
    assert proxy.proxy.auth_reject_streak == 2


@pytest.mark.parametrize("status", [402, 403])
async def test_payment_and_permission_rejections_stop_too(tmp_path, status):
    payload = {"error": {"message": "Insufficient credits", "code": status}}
    proxy = Proxy(tmp_path, httpx.MockTransport(_always(status, payload)))
    for _ in range(3):
        await proxy.post("/openai/v1/chat/completions", CHAT)
    assert [k["http_status"] for k in proxy.auth_kills] == [status]
    assert proxy.loop_kills == []


async def test_retryable_statuses_keep_their_forgive_behaviour(tmp_path):
    upstream = _always(429, {"error": {"message": "rate limited"}})
    proxy = Proxy(tmp_path, httpx.MockTransport(upstream))
    for _ in range(6):
        assert (await proxy.post("/openai/v1/chat/completions", CHAT)).status_code == 429
    assert upstream.calls == 6
    assert proxy.loop_kills == [] and proxy.auth_kills == []
    assert "upstream_rejected" not in proxy.types()


async def test_real_loop_with_200_responses_still_trips_at_four(tmp_path):
    upstream = _always(
        200, {"choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": 1}}
    )
    proxy = Proxy(tmp_path, httpx.MockTransport(upstream))
    statuses = [
        (await proxy.post("/openai/v1/chat/completions", CHAT)).status_code
        for _ in range(4)
    ]
    assert statuses == [200, 200, 200, 429]
    assert upstream.calls == 3
    assert len(proxy.loop_kills) == 1
    assert proxy.auth_kills == []


def _stuck_turn(turn: int) -> dict[str, Any]:
    """Turn ``turn`` of an agent stuck on one step: the history grows, the step does not."""

    messages: list[dict[str, Any]] = [{"role": "user", "content": "make the tests pass"}]
    for i in range(turn):
        call_id = f"call_{i}abc"
        messages.append({"role": "assistant", "content": None, "tool_calls": [
            {"id": call_id, "type": "function",
             "function": {"name": "bash", "arguments": '{"cmd": "pytest -q"}'}}]})
        messages.append({"role": "tool", "tool_call_id": call_id, "content": "3 passed"})
    return {"model": "gpt-4o-mini", "messages": messages}


@pytest.mark.parametrize("first_status", [429, 503, 401])
async def test_a_loop_whose_every_step_needs_one_retry_still_trips_at_four(tmp_path, first_status):
    # Review finding: the forgiven first attempt was undone, and its identical
    # retry was then skipped as a "resend", so the step counted zero times.  A
    # loop on a rate-limited (or briefly rejected) account never tripped.
    seen: set[bytes] = set()

    def responder(request: httpx.Request) -> httpx.Response:
        if request.content not in seen:
            seen.add(request.content)
            return httpx.Response(first_status, json={"error": {"message": "try again"}})
        return httpx.Response(
            200, json={"choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": 1}}
        )

    proxy = Proxy(tmp_path, httpx.MockTransport(responder))
    blocked_at = None
    for turn in range(1, 13):
        for _attempt in range(2):  # the first try, then the agent's identical retry
            response = await proxy.post("/openai/v1/chat/completions", _stuck_turn(turn))
            if response.headers.get("x-agentcap-blocked") == "loop":
                blocked_at = turn
            if response.status_code == 200 or blocked_at:
                break
        if blocked_at:
            break

    assert blocked_at == 4
    assert len(proxy.loop_kills) == 1
    assert proxy.auth_kills == []


async def test_free_tier_shadow_detector_does_not_nudge_on_401s(tmp_path):
    proxy = Proxy(
        tmp_path, httpx.MockTransport(_always(401, OPENROUTER_401)), feature_set=FREE_FEATURES
    )
    for _ in range(5):
        await proxy.post("/openai/v1/chat/completions", CHAT)
    assert "loop_nudge" not in proxy.types()
    assert proxy.proxy.loop_nudge is None
    assert len(proxy.auth_kills) == 1


# ------------------------------------------------- every provider, both paths
def _openai_sse_ok():
    return b'data: {"choices":[]}\n\ndata: [DONE]\n\n'


CASES = [
    # (id, path, body, status, payload)
    ("chat", "/openai/v1/chat/completions", CHAT, 401, OPENROUTER_401),
    ("chat-stream", "/openai/v1/chat/completions", {**CHAT, "stream": True}, 401, OPENROUTER_401),
    ("responses", "/openai/v1/responses", {"model": "gpt-4o-mini", "input": "same"}, 401, OPENROUTER_401),
    (
        "responses-stream",
        "/openai/v1/responses",
        {"model": "gpt-4o-mini", "input": "same", "stream": True},
        401,
        OPENROUTER_401,
    ),
    (
        "gemini",
        "/gemini/v1beta/models/gemini-2.5-flash:generateContent",
        {"contents": [{"role": "user", "parts": [{"text": "same"}]}]},
        403,
        {"error": {"code": 403, "message": "Permission denied.", "status": "PERMISSION_DENIED"}},
    ),
    (
        "gemini-stream",
        "/gemini/v1beta/models/gemini-2.5-flash:streamGenerateContent?alt=sse",
        {"contents": [{"role": "user", "parts": [{"text": "same"}]}]},
        403,
        {"error": {"code": 403, "message": "Permission denied.", "status": "PERMISSION_DENIED"}},
    ),
    (
        "gemini-invalid-key",
        "/gemini/v1beta/models/gemini-2.5-flash:generateContent",
        {"contents": [{"role": "user", "parts": [{"text": "same"}]}]},
        400,
        {
            "error": {
                "code": 400,
                "message": "API key not valid. Please pass a valid API key.",
                "status": "INVALID_ARGUMENT",
                "details": [
                    {
                        "@type": "type.googleapis.com/google.rpc.ErrorInfo",
                        "reason": "API_KEY_INVALID",
                        "domain": "googleapis.com",
                    }
                ],
            }
        },
    ),
]


@pytest.mark.parametrize("case", CASES, ids=[c[0] for c in CASES])
async def test_every_provider_path_detects_the_rejection(tmp_path, case):
    _id, path, body, status, payload = case
    upstream = _always(status, payload)
    proxy = Proxy(tmp_path, httpx.MockTransport(upstream))
    for _ in range(4):
        response = await proxy.post(path, body)
        assert response.status_code == status
    assert upstream.calls == 4
    assert proxy.loop_kills == []
    assert [k["http_status"] for k in proxy.auth_kills] == [status]
    assert proxy.proxy.auth_rejection["detail"] == payload["error"]["message"]


@pytest.mark.parametrize("stream", [False, True])
async def test_anthropic_path_with_the_local_fake_upstream(tmp_path, stream):
    # The bundled fake Anthropic API answers 401 authentication_error when the
    # request carries no x-api-key.
    proxy = Proxy(tmp_path, httpx.ASGITransport(app=fake_app()))
    body = {
        "model": "claude-sonnet-5-5",
        "max_tokens": 64,
        "messages": [{"role": "user", "content": "same"}],
        "stream": stream,
    }
    for _ in range(4):
        response = await proxy.post(
            "/anthropic/v1/messages", body, headers={"anthropic-version": "2023-06-01"}
        )
        assert response.status_code == 401
    assert proxy.loop_kills == []
    assert [k["http_status"] for k in proxy.auth_kills] == [401]
    assert proxy.proxy.auth_rejection["detail"] == "x-api-key header is required"
    assert proxy.proxy.auth_rejection["upstream_host"] == "api.anthropic.com"


async def test_plain_400_is_not_an_auth_rejection(tmp_path):
    upstream = _always(400, {"error": {"message": "bad request"}})
    proxy = Proxy(tmp_path, httpx.MockTransport(upstream))
    for i in range(4):
        body = {**CHAT, "messages": [{"role": "user", "content": f"t{i}"}]}
        await proxy.post("/openai/v1/chat/completions", body)
    assert proxy.auth_kills == []
    assert "upstream_rejected" not in proxy.types()


# ------------------------------------------------------------- safe messages
def test_auth_rejected_statuses():
    assert AUTH_REJECTED_STATUSES == frozenset({401, 402, 403})


@pytest.mark.parametrize(
    "secret",
    [
        "sk-or-v1-0123456789abcdef0123456789abcdef0123456789abcdef",
        "sk-proj-AbCdEf0123456789AbCdEf0123456789",
        "sk-ant-api03-AbCdEf0123456789AbCdEf0123456789",
        "AIza" + "Sy" + "T" * 33,  # built at runtime: test fixture, never a real key
        "gh" + "p_" + "0123456789abcdefABCDEF0123456789abcd",  # built at runtime: test fixture, never a real token
        "Bearer abc.def.ghi",
    ],
)
def test_an_echoed_key_is_never_printed(secret):
    body = json.dumps({"error": {"message": f"Incorrect API key provided: {secret}."}}).encode()
    message = upstream_error_message(body)
    assert message is not None
    assert secret not in message
    assert "[redacted]" in message
    for piece in ("0123456789abcdef", "AbCdEf0123", "A1b2C3d4", "abc.def.ghi"):
        assert piece not in message


def test_message_is_short_single_line_and_has_no_control_characters():
    raw = "line one\x1b[31m red\nline two\r\x07" + "x" * 500
    message = upstream_error_message(json.dumps({"error": {"message": raw}}).encode())
    assert message is not None
    assert "\x1b" not in message and "\n" not in message and "\x07" not in message
    assert len(message) <= 120


@pytest.mark.parametrize(
    "body",
    [
        b"<html>401 Unauthorized</html>",
        b"",
        b'{"error": "unauthorized"}',
        b'{"error": {"message": 42}}',
        b"[1, 2]",
        b'{"error": {"message": "' + b"y" * 70000 + b'"}}',
    ],
)
def test_unusable_bodies_give_no_message(body):
    assert upstream_error_message(body) is None


def test_alert_without_message_and_with_spend():
    text = format_alert(
        {
            "type": "upstream_rejected",
            "run_id": "r",
            "http_status": 402,
            "upstream_host": "openrouter.ai",
            "spent_usd": "0.0123",
        }
    )
    assert text == (
        "agentcap [r]: upstream rejected the request (HTTP 402) — check the "
        "account's credits/billing for openrouter.ai; this request was not charged "
        "($0.0123 spent so far)."
    )
    stop = format_alert(
        {"type": "upstream_auth_failed", "run_id": "r", "http_status": 401, "count": 3,
         "upstream_host": "openrouter.ai"}
    )
    assert "3 requests in a row" in stop and "Stopping the agent" in stop
    assert "loop" not in stop


# --------------------------------------------------------------------- runner
def _child(n: int = 50, exit_code: int = 0) -> list[str]:
    return [
        sys.executable,
        "-c",
        (
            "import os, sys, time, httpx\n"
            "base = os.environ['OPENAI_BASE_URL']\n"
            f"for _ in range({n}):\n"
            "    try:\n"
            "        httpx.post(base + '/chat/completions',\n"
            "                   json={'model': 'gpt-4o-mini',\n"
            "                         'messages': [{'role': 'user', 'content': 'same'}]},\n"
            "                   timeout=5)\n"
            "    except Exception:\n"
            "        pass\n"
            "    time.sleep(0.01)\n"
            f"sys.exit({exit_code})\n"
        ),
    ]


def _run(tmp_path, monkeypatch, child, transport, *, pro=True):
    if pro:
        monkeypatch.setattr(
            "agentcap.runner.status_from_environment",
            lambda: __import__("agentcap.license", fromlist=["LicenseStatus"]).LicenseStatus(
                True, "pro", "license valid"
            ),
        )
    stderr = io.StringIO()
    feature_set = PRO if pro else FREE_FEATURES
    runner = Runner(
        child,
        cap="0.50",
        ledger=Ledger(tmp_path / f"run-{time.time_ns()}.db"),
        transport=transport,
        stderr=stderr,
        alerts=AlertDispatcher(features=feature_set, stderr=stderr, background=False),
        features=feature_set,
        openai_upstream="https://openrouter.ai/api",
        poll_interval=0.01,
        term_timeout=2.0,
        receipt_dir=tmp_path / "receipts",
    )
    code = runner.run()
    return runner, code, stderr.getvalue()


@pytest.mark.parametrize("pro", [True, False], ids=["pro", "free"])
def test_runner_stops_on_auth_failure_not_as_a_loop(tmp_path, monkeypatch, pro):
    upstream = _always(401, OPENROUTER_401)
    runner, code, output = _run(
        tmp_path, monkeypatch, _child(), httpx.MockTransport(upstream), pro=pro
    )

    assert code == EXIT_UPSTREAM_AUTH_FAILED == 2
    assert runner.summary.reason == "upstream_auth_failed"
    assert runner.summary.exit_code == 2
    assert runner.summary.spent_usd == 0
    assert upstream.calls == 3
    assert "runaway loop" not in output
    assert "looks like a runaway loop" not in output
    assert (
        "upstream rejected the request (HTTP 401: User not found.) "
        "— check the API key for openrouter.ai; nothing was spent." in output
    )
    assert "reason: upstream_auth_failed" in output
    receipt = runner.receipt_payload(runner.summary)
    assert receipt["reason"] == "upstream_auth_failed"
    assert receipt["loop_detected"] is False


def test_runner_keeps_the_child_exit_code_when_the_agent_gives_up_first(tmp_path, monkeypatch):
    upstream = _always(401, OPENROUTER_401)
    runner, code, output = _run(
        tmp_path, monkeypatch, _child(n=1, exit_code=1), httpx.MockTransport(upstream)
    )
    assert code == 1
    assert runner.summary.reason == "upstream_auth_failed"
    assert upstream.calls == 1
    assert "upstream rejected the request (HTTP 401: User not found.)" in output


def test_runner_one_401_then_success_is_a_normal_run(tmp_path, monkeypatch):
    answers = iter([401] + [200] * 10)

    def responder(_request):
        if next(answers) == 401:
            return httpx.Response(401, json=OPENROUTER_401)
        return httpx.Response(
            200, json={"choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": 1}}
        )

    child = [
        sys.executable,
        "-c",
        (
            "import os, httpx\n"
            "base = os.environ['OPENAI_BASE_URL']\n"
            "for i in range(2):\n"
            "    httpx.post(base + '/chat/completions',\n"
            "               json={'model': 'gpt-4o-mini',\n"
            "                     'messages': [{'role': 'user', 'content': 'same'}]},\n"
            "               timeout=5)\n"
        ),
    ]
    runner, code, _output = _run(tmp_path, monkeypatch, child, httpx.MockTransport(responder))
    assert code == 0
    assert runner.summary.reason == "child_exit"


async def test_a_bare_proxy_without_the_callback_reports_but_never_stops(tmp_path):
    # ``agentcap serve`` passes only cap/loop callbacks: a bad key there must
    # not mark the proxy killed (that would mute later cap alerts).
    events: list[dict[str, Any]] = []
    cap_kills: list[dict[str, Any]] = []
    config = ProxyConfig(
        run_id="serve-1",
        caps=Caps(run_usd=Decimal("0.50")),
        features=PRO,
        on_cap_exceeded=cap_kills.append,
        on_loop_detected=cap_kills.append,
    )
    app = create_app(
        config,
        Ledger(tmp_path / "serve.db"),
        upstream_transport=httpx.MockTransport(_always(401, OPENROUTER_401)),
        events=events.append,
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://proxy"
    ) as client:
        for _ in range(5):
            await client.post("/openai/v1/chat/completions", json=CHAT)
    assert cap_kills == []
    assert app.state.proxy.killed is False
    types = [e["type"] for e in events]
    assert "upstream_rejected" in types
    assert "upstream_auth_failed" not in types and "loop_detected" not in types
