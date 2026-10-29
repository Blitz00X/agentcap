"""Budget-fit output clamping: body rewrite, reservations, kills and alerts."""

from __future__ import annotations

import asyncio
import io
import json
import sys
from decimal import Decimal

import httpx

from agentcap.alerts import AlertDispatcher
from agentcap.ledger import Caps, Ledger
from agentcap.runner import Runner

from proxy_support import (
    D,
    Upstream,
    build,
    gemini_usage_payload,
    openai_chat_body,
    openai_chat_response,
    proxy_client,
)

CHAT_URL = "/openai/v1/chat/completions"
RESPONSES_URL = "/openai/v1/responses"
GEMINI_URL = "/gemini/v1beta/models/gemini-4-argon:generateContent"


def _post(app, url=CHAT_URL, body=None):
    async def scenario():
        async with proxy_client(app) as client:
            return await client.post(url, json=body)

    return asyncio.run(scenario())


def test_chat_without_limit_gets_max_completion_tokens(tmp_path):
    upstream = Upstream(lambda _: openai_chat_response())
    ledger = Ledger(tmp_path / "reserve.db")
    real_reserve = ledger.reserve
    reservations: list[Decimal] = []

    def recording_reserve(*args, **kwargs):
        reservations.append(args[3])
        return real_reserve(*args, **kwargs)

    ledger.reserve = recording_reserve  # type: ignore[method-assign]
    app, ledger, events = build(
        tmp_path,
        upstream,
        ledger=ledger,
        caps=Caps(run_usd=D("0.002")),
    )
    body = openai_chat_body(model="gpt-4o-mini")

    async def scenario():
        async with proxy_client(app) as client:
            response = await client.post(CHAT_URL, json=body)
            status = await client.get("/_agentcap/status")
        return response, status

    response, status = asyncio.run(scenario())

    assert response.status_code == 200
    forwarded = json.loads(upstream.requests[0].content)
    assert 256 <= forwarded["max_completion_tokens"] < 16384
    assert "max_tokens" not in forwarded
    assert forwarded["messages"] == body["messages"]
    request = upstream.requests[0]
    assert int(request.headers["content-length"]) == len(request.content)

    # The reservation is exactly what the clamped request can use.
    assert reservations and reservations[0] <= D("0.002")
    assert reservations[0] > D("0")

    clamped = [event for event in events if event["type"] == "output_clamped"]
    assert len(clamped) == 1
    assert clamped[0]["allowed_output_tokens"] == forwarded["max_completion_tokens"]

    payload = json.loads(status.content)
    assert payload["clamped_requests"] == 1


def test_chat_existing_max_tokens_is_lowered(tmp_path):
    upstream = Upstream(lambda _: openai_chat_response())
    app, _, events = build(tmp_path, upstream, caps=Caps(run_usd=D("0.001")))
    body = openai_chat_body(model="gpt-4o-mini", max_tokens=20_000)

    response = _post(app, body=body)

    assert response.status_code == 200
    forwarded = json.loads(upstream.requests[0].content)
    assert 256 <= forwarded["max_tokens"] < 20_000
    assert "max_completion_tokens" not in forwarded
    assert [e["type"] for e in events].count("output_clamped") == 1


def test_existing_smaller_limit_is_untouched_when_it_fits(tmp_path):
    upstream = Upstream(lambda _: openai_chat_response())
    app, _, events = build(tmp_path, upstream, caps=Caps(run_usd=D("5")))
    body = openai_chat_body(model="gpt-4o-mini", max_tokens=123)
    raw = json.dumps(body).encode()

    async def scenario():
        async with proxy_client(app) as client:
            return await client.post(CHAT_URL, content=raw)

    response = asyncio.run(scenario())
    assert response.status_code == 200
    assert upstream.requests[0].content == raw
    assert "output_clamped" not in [event["type"] for event in events]


def test_responses_limit_is_clamped(tmp_path):
    upstream = Upstream(
        lambda _: httpx.Response(
            200,
            json={
                "id": "resp_mock",
                "object": "response",
                "usage": {"input_tokens": 10, "output_tokens": 5},
            },
        )
    )
    app, _, _ = build(tmp_path, upstream, caps=Caps(run_usd=D("0.002")))

    response = _post(
        app, url=RESPONSES_URL, body={"model": "gpt-4o-mini", "input": "hello"}
    )

    assert response.status_code == 200
    forwarded = json.loads(upstream.requests[0].content)
    assert 256 <= forwarded["max_output_tokens"] < 16384
    assert "max_completion_tokens" not in forwarded


def test_gemini_generation_config_is_created(tmp_path):
    upstream = Upstream(
        lambda _: httpx.Response(
            200,
            json={
                "candidates": [],
                "usageMetadata": gemini_usage_payload(prompt=10, candidates=5),
            },
        )
    )
    app, _, _ = build(tmp_path, upstream, caps=Caps(run_usd=D("0.05")))
    body = {"contents": [{"parts": [{"text": "hi"}]}]}

    response = _post(app, url=GEMINI_URL, body=body)

    assert response.status_code == 200
    forwarded = json.loads(upstream.requests[0].content)
    config = forwarded["generationConfig"]
    assert 256 <= config["maxOutputTokens"] < 65536
    assert forwarded["contents"] == body["contents"]


def test_gemini_generation_config_is_merged(tmp_path):
    upstream = Upstream(
        lambda _: httpx.Response(
            200,
            json={
                "candidates": [],
                "usageMetadata": gemini_usage_payload(prompt=10, candidates=5),
            },
        )
    )
    app, _, _ = build(tmp_path, upstream, caps=Caps(run_usd=D("0.05")))
    body = {
        "contents": [{"parts": [{"text": "hi"}]}],
        "generationConfig": {"temperature": 0.2, "candidateCount": 1},
    }

    response = _post(app, url=GEMINI_URL, body=body)

    assert response.status_code == 200
    config = json.loads(upstream.requests[0].content)["generationConfig"]
    assert config["temperature"] == 0.2
    assert 256 <= config["maxOutputTokens"] < 65536


def test_budget_exhausted_kills_only_once(tmp_path):
    calls: list[object] = []
    upstream = Upstream(lambda _: openai_chat_response())
    app, ledger, events = build(
        tmp_path,
        upstream,
        caps=Caps(run_usd=D("0.00001")),
        on_cap_exceeded=calls.append,
    )
    body = openai_chat_body(model="gpt-4o-mini")

    async def scenario():
        async with proxy_client(app) as client:
            return [await client.post(CHAT_URL, json=body) for _ in range(3)]

    responses = asyncio.run(scenario())

    assert [r.status_code for r in responses] == [429, 429, 429]
    for response in responses:
        error = json.loads(response.content)["error"]
        assert error["code"] == "agentcap_cap_exceeded"
    assert upstream.calls == 0
    assert len(calls) == 1
    assert [e["type"] for e in events].count("cap_exceeded") == 3
    assert ledger.summary("run-1")["reservations"] == 0


def test_no_clamp_returns_request_too_large_without_kill(tmp_path):
    calls: list[object] = []
    upstream = Upstream(lambda _: openai_chat_response())
    app, _, events = build(
        tmp_path,
        upstream,
        caps=Caps(run_usd=D("0.002")),
        clamp_output=False,
        on_cap_exceeded=calls.append,
    )

    response = _post(app, body=openai_chat_body(model="gpt-4o-mini"))

    assert response.status_code == 429
    error = json.loads(response.content)["error"]
    assert error["code"] == "agentcap_request_too_large"
    assert "scope=run" in error["message"]
    assert calls == []
    assert upstream.calls == 0
    assert [e["type"] for e in events] == ["request_too_large"]


def test_clamp_alert_is_yellow_and_printed_once():
    stderr = io.StringIO()
    dispatcher = AlertDispatcher(stderr=stderr, background=False)
    for _ in range(3):
        dispatcher.emit(
            {"type": "output_clamped", "allowed_output_tokens": 1000}
        )
        dispatcher.emit(
            {"type": "cap_exceeded", "scope": "run", "cap_usd": "1", "spent_usd": "1"}
        )

    output = stderr.getvalue()
    assert output.count("budget low, limiting output to 1000 tokens per request") == 1
    assert output.count("RUN CAP HIT") == 1
    assert "\x1b[33m" in output


def test_gemini_existing_max_output_tokens_is_lowered(tmp_path):
    upstream = Upstream(
        lambda _: httpx.Response(
            200,
            json={
                "candidates": [],
                "usageMetadata": gemini_usage_payload(prompt=10, candidates=5),
            },
        )
    )
    app, _, events = build(tmp_path, upstream, caps=Caps(run_usd=D("0.05")))
    body = {
        "contents": [{"parts": [{"text": "hi"}]}],
        "generationConfig": {"temperature": 0.2, "maxOutputTokens": 65_536},
    }

    response = _post(app, url=GEMINI_URL, body=body)

    assert response.status_code == 200
    config = json.loads(upstream.requests[0].content)["generationConfig"]
    assert config["temperature"] == 0.2
    assert 256 <= config["maxOutputTokens"] < 65_536
    assert [e["type"] for e in events].count("output_clamped") == 1


def test_gemini_clamp_lowers_max_output_and_thinking_budget(tmp_path):
    upstream = Upstream(
        lambda _: httpx.Response(
            200,
            json={
                "candidates": [],
                "usageMetadata": gemini_usage_payload(prompt=10, candidates=5),
            },
        )
    )
    app, _, events = build(tmp_path, upstream, caps=Caps(run_usd=D("0.02")))
    body = {
        "contents": [{"parts": [{"text": "hi"}]}],
        "generationConfig": {
            "maxOutputTokens": 10_000,
            "thinkingConfig": {"thinkingBudget": 8_000},
        },
    }

    response = _post(app, url=GEMINI_URL, body=body)

    assert response.status_code == 200
    config = json.loads(upstream.requests[0].content)["generationConfig"]
    assert config["maxOutputTokens"] < 10_000
    assert config["thinkingConfig"]["thinkingBudget"] < 8_000
    # Both limits are billed as output, so their sum must fit the clamp.
    assert (
        config["maxOutputTokens"] + config["thinkingConfig"]["thinkingBudget"] <= 8_000
    )
    assert [e["type"] for e in events].count("output_clamped") == 1


def test_gemini_clamp_adds_thinking_budget_when_missing(tmp_path):
    upstream = Upstream(
        lambda _: httpx.Response(
            200,
            json={
                "candidates": [],
                "usageMetadata": gemini_usage_payload(prompt=10, candidates=5),
            },
        )
    )
    app, _, events = build(tmp_path, upstream, caps=Caps(run_usd=D("0.02")))
    body = {"contents": [{"parts": [{"text": "hi"}]}]}

    response = _post(app, url=GEMINI_URL, body=body)

    assert response.status_code == 200
    config = json.loads(upstream.requests[0].content)["generationConfig"]
    assert config["maxOutputTokens"] < 65_536
    # A thinking-capable model without a budget gets a bounded one so dynamic
    # thinking cannot settle above the reservation.
    assert config["thinkingConfig"]["thinkingBudget"] > 0
    assert config["thinkingConfig"]["thinkingBudget"] <= 128
    assert [e["type"] for e in events].count("output_clamped") == 1


def test_legacy_completions_lowers_max_tokens(tmp_path):
    upstream = Upstream(
        lambda _: httpx.Response(
            200,
            json={
                "id": "cmpl-mock",
                "object": "text_completion",
                "choices": [],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            },
        )
    )
    app, _, _ = build(tmp_path, upstream, caps=Caps(run_usd=D("0.002")))

    response = _post(
        app,
        url="/openai/v1/completions",
        body={"model": "gpt-4o-mini", "prompt": "hello"},
    )

    assert response.status_code == 200
    forwarded = json.loads(upstream.requests[0].content)
    assert 256 <= forwarded["max_tokens"] < 16_384
    assert "max_completion_tokens" not in forwarded


def test_input_only_request_too_large_does_not_kill(tmp_path):
    calls: list[object] = []
    upstream = Upstream(
        lambda _: httpx.Response(
            200,
            json={"object": "list", "data": [], "usage": {"prompt_tokens": 5}},
        )
    )
    app, _, events = build(
        tmp_path,
        upstream,
        caps=Caps(run_usd=D("0.000001")),
        on_cap_exceeded=calls.append,
    )

    response = _post(
        app,
        url="/openai/v1/embeddings",
        body={"model": "text-embedding-3-small", "input": "hello"},
    )

    assert response.status_code == 429
    error = json.loads(response.content)["error"]
    assert error["code"] == "agentcap_request_too_large"
    assert calls == []
    assert upstream.calls == 0
    assert [e["type"] for e in events] == ["request_too_large"]


def test_clamp_alert_is_printed_once_per_run():
    stderr = io.StringIO()
    dispatcher = AlertDispatcher(stderr=stderr, background=False)
    for run_id in ("run-a", "run-a", "run-b"):
        dispatcher.emit(
            {
                "type": "output_clamped",
                "run_id": run_id,
                "allowed_output_tokens": 1000,
            }
        )

    output = stderr.getvalue()
    assert output.count("budget low, limiting output to 1000 tokens per request") == 2
    assert "run-a" in output and "run-b" in output


def test_runner_e2e_small_cap_gemini_request_is_clamped(tmp_path):
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "candidates": [{"content": {"parts": [{"text": "ok"}]}}],
                "usageMetadata": gemini_usage_payload(prompt=100, candidates=200),
            },
        )

    child = [
        sys.executable,
        "-c",
        (
            "import os, sys, httpx\n"
            "base = os.environ['GOOGLE_GEMINI_BASE_URL']\n"
            "r = httpx.post(base + '/v1beta/models/gemini-4-argon:generateContent',\n"
            "               json={'contents': [{'parts': [{'text': 'hi'}]}]},\n"
            "               timeout=10)\n"
            "sys.exit(0 if r.status_code == 200 else 1)\n"
        ),
    ]
    stderr = io.StringIO()
    runner = Runner(
        child,
        cap="0.05",
        ledger=Ledger(tmp_path / "e2e.db"),
        transport=httpx.MockTransport(handler),
        stderr=stderr,
        alerts=AlertDispatcher(stderr=stderr, background=False),
        poll_interval=0.01,
        term_timeout=2.0,
    )

    code = runner.run()

    assert code == 0
    assert runner.summary is not None
    assert runner.summary.reason == "child_exit"
    assert runner.summary.clamped_requests == 1
    assert runner.summary.spent_usd < Decimal("0.05")
    assert seen
    assert 256 <= seen[0]["generationConfig"]["maxOutputTokens"] < 65536
    assert "budget low, limiting output" in stderr.getvalue()
    assert stderr.getvalue().count("budget low, limiting output") == 1
