from __future__ import annotations

import asyncio
import json
from decimal import Decimal

import httpx

from agentcap.estimate import (
    apply_input_safety_factor,
    estimate_request,
    reservation_usd,
)
from agentcap.pricing import load_pricing
from agentcap.proxy import ProxyConfig

from proxy_support import (
    D,
    ChunkStream,
    Upstream,
    build,
    costs,
    gemini_usage_payload,
    openai_chat_body,
    openai_chat_response,
    proxy_client,
    sse,
)

STORE = load_pricing()
CHAT_URL = "/openai/v1/chat/completions"


def _stream_response(chunks, content_type: str = "text/event-stream") -> httpx.Response:
    return httpx.Response(
        200,
        headers={"content-type": content_type},
        stream=ChunkStream(chunks),
    )


def test_openai_chat_stream_is_byte_identical_and_settles(tmp_path):
    usage = {
        "choices": [],
        "usage": {
            "prompt_tokens": 1000,
            "completion_tokens": 500,
            "prompt_tokens_details": {"cached_tokens": 200},
        },
    }
    chunks = [
        b'data: {"choices":[{"delta":{"content":"He',
        b'llo"}}]}\n\n',
        b'data: {"choices":[{"delta":{"content":" world"}}]}\n\n',
        sse(usage),
        b"data: [DONE]\n\n",
    ]
    upstream = Upstream(_stream_response(chunks))
    app, ledger, events = build(tmp_path, upstream)
    body = openai_chat_body(stream=True)

    async def scenario():
        async with proxy_client(app) as client:
            return await client.post(CHAT_URL, json=body)

    response = asyncio.run(scenario())

    assert response.status_code == 200
    assert response.content == b"".join(chunks)
    assert response.headers["content-type"].startswith("text/event-stream")

    forwarded = json.loads(upstream.requests[0].content)
    assert forwarded["stream_options"] == {"include_usage": True}
    assert forwarded["stream"] is True

    assert ledger.summary("run-1")["actual_usd"] == D("0.000435")
    settled = costs(events, "request_settled")
    assert settled[-1]["cost_usd"] == "0.000435"
    assert "usage_missing" not in [event["type"] for event in events]


def test_stream_options_merge_preserves_existing_keys(tmp_path):
    chunks = [
        sse({"choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": 5}}),
        b"data: [DONE]\n\n",
    ]
    upstream = Upstream(_stream_response(chunks))
    app, _, _ = build(tmp_path, upstream)
    body = openai_chat_body(
        stream=True, stream_options={"include_usage": False, "custom": True}
    )

    async def scenario():
        async with proxy_client(app) as client:
            return await client.post(CHAT_URL, json=body)

    asyncio.run(scenario())
    forwarded = json.loads(upstream.requests[0].content)
    assert forwarded["stream_options"] == {"include_usage": True, "custom": True}


def test_stream_options_injection_can_be_disabled(tmp_path):
    chunks = [
        sse({"choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": 5}}),
        b"data: [DONE]\n\n",
    ]
    upstream = Upstream(_stream_response(chunks))
    app, ledger, _ = build(
        tmp_path, upstream, inject_openai_stream_usage=False
    )
    raw = json.dumps(openai_chat_body(stream=True)).encode()

    async def scenario():
        async with proxy_client(app) as client:
            return await client.post(CHAT_URL, content=raw)

    asyncio.run(scenario())
    assert upstream.requests[0].content == raw
    # 10 * 0.15 + 5 * 0.60 per Mtok
    assert ledger.summary("run-1")["actual_usd"] == D("0.0000045")


def test_openai_responses_stream_uses_response_completed(tmp_path):
    chunks = [
        b'event: response.created\n'
        b'data: {"type":"response.created","response":{}}\n\n',
        b'event: response.completed\n'
        b'data: {"type":"response.completed","response":{"usage":'
        b'{"input_tokens":1000,"output_tokens":500,'
        b'"input_tokens_details":{"cached_tokens":100}}}}\n\n',
    ]
    upstream = Upstream(_stream_response(chunks))
    app, ledger, _ = build(tmp_path, upstream)
    body = {"model": "gpt-4o-mini", "input": "hello", "stream": True}

    async def scenario():
        async with proxy_client(app) as client:
            return await client.post("/openai/v1/responses", json=body)

    response = asyncio.run(scenario())
    assert response.status_code == 200
    assert response.content == b"".join(chunks)
    assert ledger.summary("run-1")["actual_usd"] == D("0.0004425")


def test_gemini_sse_stream_uses_last_cumulative_usage(tmp_path):
    chunks = [
        sse(
            {
                "candidates": [{"content": {"parts": [{"text": "hel"}]}}],
                "usageMetadata": gemini_usage_payload(prompt=1000, candidates=100),
            }
        ),
        sse(
            {
                "candidates": [{"content": {"parts": [{"text": "lo"}]}}],
                "usageMetadata": gemini_usage_payload(
                    prompt=1000, candidates=200, thoughts=50, cached=100
                ),
            }
        ),
    ]
    upstream = Upstream(_stream_response(chunks))
    app, ledger, _ = build(tmp_path, upstream)
    body = {"contents": [{"parts": [{"text": "hi"}]}]}

    async def scenario():
        async with proxy_client(app) as client:
            return await client.post(
                "/gemini/v1beta/models/gemini-2.5-flash:streamGenerateContent?alt=sse",
                json=body,
            )

    response = asyncio.run(scenario())
    assert response.status_code == 200
    assert response.content == b"".join(chunks)
    assert "alt=sse" in str(upstream.requests[0].url)
    assert ledger.summary("run-1")["actual_usd"] == D("0.000898")


def test_gemini_json_array_stream_split_mid_body(tmp_path):
    array = json.dumps(
        [
            {
                "candidates": [{"content": {"parts": [{"text": "hi"}]}}],
                "usageMetadata": gemini_usage_payload(prompt=1000, candidates=100),
            },
            {
                "candidates": [{"content": {"parts": [{"text": "!"}]}}],
                "usageMetadata": gemini_usage_payload(
                    prompt=1000, candidates=200, thoughts=50, cached=100
                ),
            },
        ]
    ).encode()
    chunks = [array[:17], array[17:41], array[41:]]
    upstream = Upstream(
        _stream_response(chunks, content_type="application/json")
    )
    app, ledger, _ = build(tmp_path, upstream)

    async def scenario():
        async with proxy_client(app) as client:
            return await client.post(
                "/gemini/v1beta/models/gemini-2.5-flash:streamGenerateContent",
                json={"contents": []},
            )

    response = asyncio.run(scenario())
    assert response.status_code == 200
    assert response.content == array
    assert ledger.summary("run-1")["actual_usd"] == D("0.000898")


def test_missing_stream_usage_charges_full_reservation(tmp_path):
    chunks = [
        b'data: {"choices":[{"delta":{"content":"partial"}}]}\n\n',
        b"data: [DONE]\n\n",
    ]
    upstream = Upstream(_stream_response(chunks))
    app, ledger, events = build(tmp_path, upstream)
    body = openai_chat_body(max_completion_tokens=200, stream=True)
    price = STORE.get("openai/gpt-4o-mini")
    input_tokens, output_tokens, _ = estimate_request("openai", body, price)
    reservation = reservation_usd(
        price,
        apply_input_safety_factor(
            input_tokens, ProxyConfig(run_id="run-1").input_safety_factor
        ),
        output_tokens,
    )

    async def scenario():
        async with proxy_client(app) as client:
            return await client.post(CHAT_URL, json=body)

    response = asyncio.run(scenario())
    assert response.status_code == 200
    assert response.content == b"".join(chunks)

    summary = ledger.summary("run-1")
    assert summary["actual_usd"] == reservation
    assert summary["open"] == 0

    missing = costs(events, "usage_missing")
    assert len(missing) == 1
    assert missing[0]["charged_usd"] == str(reservation)


def test_streaming_upstream_error_releases_reservation(tmp_path):
    upstream = Upstream(
        httpx.Response(500, json={"error": {"message": "upstream exploded"}})
    )
    app, ledger, events = build(tmp_path, upstream)
    body = openai_chat_body(stream=True)

    async def scenario():
        async with proxy_client(app) as client:
            return await client.post(CHAT_URL, json=body)

    response = asyncio.run(scenario())
    assert response.status_code == 500
    assert json.loads(response.content)["error"]["message"] == "upstream exploded"

    summary = ledger.summary("run-1")
    assert summary["released"] == 1
    assert summary["open"] == 0
    assert summary["actual_usd"] == Decimal("0")
    assert costs(events, "usage_missing") == []


def test_stream_chunks_are_forwarded_before_settlement(tmp_path):
    seen: list[bytes] = []

    async def handler(_request):
        return _stream_response(
            [
                sse({"choices": [{"delta": {"content": "a"}}]}),
                sse({"choices": [], "usage": {"prompt_tokens": 1, "completion_tokens": 1}}),
                b"data: [DONE]\n\n",
            ]
        )

    upstream = Upstream(handler)
    app, ledger, _ = build(tmp_path, upstream)

    async def scenario():
        async with proxy_client(app) as client:
            async with client.stream(
                "POST", CHAT_URL, json=openai_chat_body(stream=True)
            ) as response:
                async for chunk in response.aiter_raw():
                    seen.append(chunk)
        return seen

    chunks = asyncio.run(scenario())
    assert b"".join(chunks)
    assert ledger.summary("run-1")["settled"] == 1
