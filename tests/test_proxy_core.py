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
from agentcap.ledger import Caps, Ledger
from agentcap.license import FREE_FEATURES, features
from agentcap.pricing import load_pricing
from agentcap.proxy import ProxyConfig

from proxy_support import (
    D,
    Upstream,
    build,
    gemini_usage_payload,
    openai_chat_body,
    openai_chat_response,
    proxy_client,
)

STORE = load_pricing()
CHAT_URL = "/openai/v1/chat/completions"


def test_openai_chat_forwarded_unchanged_and_settled(tmp_path):
    body = json.dumps(openai_chat_body()).encode()
    upstream = Upstream(lambda _: openai_chat_response(cached_tokens=200))
    app, ledger, events = build(
        tmp_path, upstream, caps=Caps(run_usd=D("5"))
    )

    async def scenario():
        async with proxy_client(app) as client:
            response = await client.post(
                CHAT_URL,
                content=body,
                headers={"Authorization": "Bearer sk-test"},
            )
            status = await client.get("/_agentcap/status")
        return response, status

    response, status = asyncio.run(scenario())

    assert response.status_code == 200
    assert upstream.requests[0].content == body
    assert upstream.requests[0].headers["authorization"] == "Bearer sk-test"
    assert str(upstream.requests[0].url) == (
        "https://api.openai.com/v1/chat/completions"
    )
    assert upstream.requests[0].headers["accept-encoding"] == "identity"

    # 800 uncached * 0.15 + 200 cached * 0.075 + 500 output * 0.60, per Mtok
    assert ledger.summary("run-1")["actual_usd"] == D("0.000435")
    assert ledger.summary("run-1")["settled"] == 1

    payload = json.loads(status.content)
    assert payload["run_id"] == "run-1"
    assert payload["spent_usd"] == "0.000435"
    assert payload["reserved_usd"] == "0"
    assert payload["caps"]["run_usd"] == "5"
    assert payload["blocked_count"] == 0
    assert payload["killed"] is False

    settled = [event for event in events if event["type"] == "request_settled"]
    assert settled[0]["cost_usd"] == "0.000435"
    assert settled[0]["model"] == "gpt-4o-mini"


def test_openai_embeddings_output_tokens_are_zero(tmp_path):
    body = json.dumps(
        {"model": "gpt-4o-mini", "input": "hello"}
    ).encode()
    upstream = Upstream(
        httpx.Response(
            200,
            json={
                "object": "list",
                "data": [],
                "usage": {"prompt_tokens": 1000, "total_tokens": 1000},
            },
        )
    )
    app, ledger, _ = build(tmp_path, upstream)

    async def scenario():
        async with proxy_client(app) as client:
            return await client.post("/openai/v1/embeddings", content=body)

    response = asyncio.run(scenario())
    assert response.status_code == 200
    assert ledger.summary("run-1")["actual_usd"] == D("0.00015")


def test_openai_responses_non_stream(tmp_path):
    body = json.dumps({"model": "gpt-4o-mini", "input": "hello"}).encode()
    upstream = Upstream(
        httpx.Response(
            200,
            json={
                "id": "resp_mock",
                "object": "response",
                "usage": {
                    "input_tokens": 1000,
                    "output_tokens": 500,
                    "input_tokens_details": {"cached_tokens": 100},
                },
            },
        )
    )
    app, ledger, _ = build(tmp_path, upstream)

    async def scenario():
        async with proxy_client(app) as client:
            return await client.post("/openai/v1/responses", content=body)

    response = asyncio.run(scenario())
    assert response.status_code == 200
    # 900 * 0.15 + 100 * 0.075 + 500 * 0.60 per Mtok
    assert ledger.summary("run-1")["actual_usd"] == D("0.0004425")


def test_gemini_non_stream_bills_thoughts_as_output(tmp_path):
    body = json.dumps({"contents": [{"parts": [{"text": "hi"}]}]}).encode()
    upstream = Upstream(
        httpx.Response(
            200,
            json={
                "candidates": [{"content": {"parts": [{"text": "hello"}]}}],
                "usageMetadata": gemini_usage_payload(
                    prompt=1000, candidates=200, thoughts=50, cached=100
                ),
            },
        )
    )
    app, ledger, _ = build(tmp_path, upstream)

    async def scenario():
        async with proxy_client(app) as client:
            return await client.post(
                "/gemini/v1beta/models/gemini-2.5-flash:generateContent",
                content=body,
                headers={"x-goog-api-key": "test-key"},
            )

    response = asyncio.run(scenario())
    assert response.status_code == 200
    assert "models/gemini-2.5-flash:generateContent" in str(upstream.requests[0].url)
    assert upstream.requests[0].headers["x-goog-api-key"] == "test-key"
    # 900 * 0.30 + 100 * 0.03 + (200 + 50) * 2.50 per Mtok
    assert ledger.summary("run-1")["actual_usd"] == D("0.000898")


def test_status_never_contains_credentials(tmp_path):
    upstream = Upstream(lambda _: openai_chat_response())
    app, _, _ = build(tmp_path, upstream)

    async def scenario():
        async with proxy_client(app) as client:
            await client.post(
                CHAT_URL,
                content=json.dumps(openai_chat_body()).encode(),
                headers={"Authorization": "Bearer sk-test-SECRET"},
            )
            return await client.get("/_agentcap/status")

    body = asyncio.run(scenario()).content.decode().lower()
    assert "authorization" not in body
    assert "bearer" not in body
    assert "secret" not in body
    assert "sk-" not in body


def test_unknown_model_is_blocked_fail_closed(tmp_path):
    upstream = Upstream(lambda _: openai_chat_response())
    app, ledger, _ = build(tmp_path, upstream)
    body = json.dumps(openai_chat_body(model="gpt-does-not-exist")).encode()

    async def scenario():
        async with proxy_client(app) as client:
            return await client.post(CHAT_URL, content=body)

    response = asyncio.run(scenario())
    assert response.status_code == 403
    payload = json.loads(response.content)["error"]
    assert payload["type"] == "agentcap_unknown_model"
    assert "gpt-does-not-exist" in payload["message"]
    assert "pricing.json" in payload["message"]
    assert upstream.calls == 0
    assert ledger.summary("run-1")["reservations"] == 0


def test_malformed_body_is_blocked(tmp_path):
    upstream = Upstream(lambda _: openai_chat_response())
    app, _, _ = build(tmp_path, upstream)

    async def scenario():
        async with proxy_client(app) as client:
            return await client.post(CHAT_URL, content=b"{not json")

    response = asyncio.run(scenario())
    assert response.status_code == 400
    assert json.loads(response.content)["error"]["type"] == "agentcap_invalid_body"
    assert upstream.calls == 0


def test_unmetered_post_blocked_by_default(tmp_path):
    upstream = Upstream(httpx.Response(200, json={"ok": True}))
    app, ledger, events = build(tmp_path, upstream)

    async def scenario():
        async with proxy_client(app) as client:
            return await client.post("/openai/v1/files", content=b"{}")

    response = asyncio.run(scenario())
    assert response.status_code == 403
    error = json.loads(response.content)["error"]
    assert error["type"] == "agentcap_blocked"
    assert error["message"] == (
        "agentcap: endpoint not metered; blocked (fail-closed). "
        "Use --allow-unmetered to pass through."
    )
    assert upstream.calls == 0
    assert ledger.summary("run-1")["reservations"] == 0
    assert events == []


def test_allow_unmetered_forwards_and_emits_event(tmp_path):
    upstream = Upstream(httpx.Response(200, json={"ok": True}))
    app, _, events = build(tmp_path, upstream, allow_unmetered=True)

    async def scenario():
        async with proxy_client(app) as client:
            return await client.post("/openai/v1/files", content=b"{}")

    response = asyncio.run(scenario())
    assert response.status_code == 200
    assert upstream.calls == 1
    assert [event["type"] for event in events] == ["unmetered"]


def test_get_and_count_tokens_are_free_passthroughs(tmp_path):
    upstream = Upstream(httpx.Response(200, json={"data": []}))
    app, ledger, _ = build(tmp_path, upstream)

    async def scenario():
        async with proxy_client(app) as client:
            models = await client.get("/openai/v1/models")
            counted = await client.post(
                "/gemini/v1beta/models/gemini-2.5-flash:countTokens",
                content=b'{"contents": []}',
            )
        return models, counted

    models, counted = asyncio.run(scenario())
    assert models.status_code == 200
    assert counted.status_code == 200
    assert upstream.calls == 2
    assert ledger.summary("run-1")["reservations"] == 0


def test_upstream_error_releases_reservation(tmp_path):
    upstream = Upstream(
        httpx.Response(500, json={"error": {"message": "boom"}})
    )
    app, ledger, _ = build(tmp_path, upstream)

    async def scenario():
        async with proxy_client(app) as client:
            return await client.post(
                CHAT_URL, content=json.dumps(openai_chat_body()).encode()
            )

    response = asyncio.run(scenario())
    assert response.status_code == 500
    summary = ledger.summary("run-1")
    assert summary["released"] == 1
    assert summary["open"] == 0
    assert summary["actual_usd"] == D("0")
    assert ledger.spent("run-1") == D("0")


def test_cap_exceeded_blocks_before_upstream_and_calls_callback(tmp_path):
    calls: list[object] = []
    upstream = Upstream(lambda _: openai_chat_response())

    def on_cap_exceeded(payload):
        calls.append(payload)

    app, ledger, events = build(
        tmp_path,
        upstream,
        caps=Caps(run_usd=D("0.0001")),
        on_cap_exceeded=on_cap_exceeded,
    )

    async def scenario():
        async with proxy_client(app) as client:
            return await client.post(
                CHAT_URL, content=json.dumps(openai_chat_body(model="gpt-4o")).encode()
            )

    response = asyncio.run(scenario())
    assert response.status_code == 429
    assert response.headers["x-agentcap-blocked"] == "cap"
    error = json.loads(response.content)["error"]
    assert error["type"] == "agentcap_cap_exceeded"
    assert error["code"] == "agentcap_cap_exceeded"
    assert "Request NOT sent." in error["message"]
    assert "scope=run" in error["message"]
    assert upstream.calls == 0
    assert ledger.summary("run-1")["reservations"] == 0
    assert len(calls) == 1
    assert [event["type"] for event in events].count("cap_exceeded") == 1


def test_hour_and_day_caps_respect_feature_gating(tmp_path):
    body = json.dumps(openai_chat_body(model="gpt-4o")).encode()

    def run_with(caps, feature_set, name):
        upstream = Upstream(lambda _: openai_chat_response())
        app, _, _ = build(
            tmp_path, upstream, caps=caps, features=feature_set, name=name
        )

        async def scenario():
            async with proxy_client(app) as client:
                return await client.post(CHAT_URL, content=body)

        return asyncio.run(scenario()), upstream

    free_response, free_upstream = run_with(
        Caps(hour_usd=D("0.001")), FREE_FEATURES, "free.db"
    )
    assert free_response.status_code == 200
    assert free_upstream.calls == 1

    pro_response, pro_upstream = run_with(
        Caps(hour_usd=D("0.001")), features("pro"), "pro-hour.db"
    )
    assert pro_response.status_code == 429
    assert "scope=hour" in json.loads(pro_response.content)["error"]["message"]
    assert pro_upstream.calls == 0

    day_response, day_upstream = run_with(
        Caps(day_usd=D("0.001")), features("pro"), "pro-day.db"
    )
    assert day_response.status_code == 429
    assert "scope=day" in json.loads(day_response.content)["error"]["message"]
    assert day_upstream.calls == 0


def test_concurrent_requests_never_exceed_cap(tmp_path):
    body = openai_chat_body(max_completion_tokens=100)
    price = STORE.get("openai/gpt-4o-mini")
    input_tokens, output_tokens, _ = estimate_request("openai", body, price)
    reservation = reservation_usd(
        price,
        apply_input_safety_factor(
            input_tokens, ProxyConfig(run_id="run-1").input_safety_factor
        ),
        output_tokens,
    )
    cap = reservation * 5

    # Hold the allowed upstream calls until the ledger is full, so the test
    # observes reservation concurrency rather than the budget freed by
    # immediate settlement.  Only the five requests that fit reach `reserve`;
    # later ones are rejected pre-flight (budget-fit clamping cannot help a
    # request that already sets a 100-token limit).
    all_reserved = asyncio.Event()
    attempts = 0
    real_reserve = None

    async def slow_response(_request):
        await all_reserved.wait()
        return openai_chat_response(prompt_tokens=100, completion_tokens=10)

    upstream = Upstream(slow_response)
    ledger = Ledger(tmp_path / "concurrent.db")
    real_reserve = ledger.reserve

    def counting_reserve(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        try:
            return real_reserve(*args, **kwargs)
        finally:
            if attempts >= 5:
                all_reserved.set()

    ledger.reserve = counting_reserve  # type: ignore[method-assign]

    app, ledger, _ = build(
        tmp_path, upstream, caps=Caps(run_usd=cap), ledger=ledger
    )

    async def scenario():
        async with proxy_client(app) as client:
            return await asyncio.gather(
                *[
                    client.post(CHAT_URL, json=body)
                    for _ in range(20)
                ]
            )

    responses = asyncio.run(scenario())
    statuses = [response.status_code for response in responses]
    assert statuses.count(200) == 5
    assert statuses.count(429) == 15
    assert upstream.calls == 5

    summary = ledger.summary("run-1")
    assert summary["settled"] == 5
    assert summary["open"] == 0
    assert ledger.spent("run-1") <= cap


def test_cap_threshold_events_fire_once_each(tmp_path):
    upstream = Upstream(lambda _: openai_chat_response())
    app, ledger, events = build(tmp_path, upstream, caps=Caps(run_usd=D("1")))

    # Completion-token counts chosen so spend crosses 50%, 80% and 100%.
    tokens = [916667, 500000, 333334]

    async def scenario():
        async with proxy_client(app) as client:
            for completion in tokens:
                upstream.responder = lambda _, c=completion: openai_chat_response(
                    prompt_tokens=100, completion_tokens=c
                )
                response = await client.post(
                    CHAT_URL,
                    content=json.dumps(
                        openai_chat_body(max_completion_tokens=100)
                    ).encode(),
                )
                assert response.status_code == 200, response.content

    asyncio.run(scenario())
    thresholds = [
        event["threshold"] for event in events if event["type"] == "cap_threshold"
    ]
    assert thresholds == [50, 80, 100]
    assert ledger.spent("run-1") > D("1")


def test_loop_detection_blocks_after_threshold(tmp_path):
    kills: list[object] = []
    upstream = Upstream(lambda _: openai_chat_response())
    app, _, events = build(
        tmp_path,
        upstream,
        features=features("pro"),
        loop_threshold=3,
        on_loop_detected=lambda payload: kills.append(payload),
    )
    body = json.dumps(openai_chat_body()).encode()

    async def scenario():
        async with proxy_client(app) as client:
            responses = [
                await client.post(CHAT_URL, content=body) for _ in range(3)
            ]
            status = await client.get("/_agentcap/status")
        return responses, status

    responses, status = asyncio.run(scenario())
    assert [response.status_code for response in responses] == [200, 200, 429]
    assert upstream.calls == 2
    blocked = json.loads(responses[2].content)["error"]
    assert blocked["type"] == "agentcap_loop_detected"
    assert responses[2].headers["x-agentcap-blocked"] == "loop"
    assert len(kills) == 1
    status_payload = json.loads(status.content)
    assert status_payload["killed"] is True
    assert status_payload["blocked_count"] == 1
    assert [event["type"] for event in events].count("loop_detected") == 1


def test_loop_detection_is_not_enabled_on_free_tier(tmp_path):
    upstream = Upstream(lambda _: openai_chat_response())
    app, _, _ = build(
        tmp_path,
        upstream,
        features=FREE_FEATURES,
        loop_threshold=2,
    )
    body = json.dumps(openai_chat_body()).encode()

    async def scenario():
        async with proxy_client(app) as client:
            return [
                await client.post(CHAT_URL, content=body) for _ in range(4)
            ]

    responses = asyncio.run(scenario())
    assert [response.status_code for response in responses] == [200, 200, 200, 200]
    assert upstream.calls == 4


def test_upstream_status_and_headers_are_copied(tmp_path):
    upstream = Upstream(
        httpx.Response(
            200,
            json={
                "choices": [],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            },
            headers={"x-request-id": "req_mock", "cache-control": "no-store"},
        )
    )
    app, _, _ = build(tmp_path, upstream)

    async def scenario():
        async with proxy_client(app) as client:
            return await client.post(
                CHAT_URL, content=json.dumps(openai_chat_body()).encode()
            )

    response = asyncio.run(scenario())
    assert response.status_code == 200
    assert response.headers["x-request-id"] == "req_mock"
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["content-type"].startswith("application/json")


def test_redaction_keeps_secrets_out_of_events_and_ledger(tmp_path):
    upstream = Upstream(lambda _: openai_chat_response())
    app, ledger, events = build(tmp_path, upstream)

    async def scenario():
        async with proxy_client(app) as client:
            await client.post(
                CHAT_URL,
                content=json.dumps(openai_chat_body()).encode(),
                headers={"Authorization": "Bearer sk-test-SECRET"},
            )
            await client.post(
                "/gemini/v1beta/models/gemini-2.5-flash:generateContent",
                json={"contents": []},
                headers={"x-goog-api-key": "key-SECRET"},
            )

    asyncio.run(scenario())

    serialized = json.dumps(events)
    assert "SECRET" not in serialized
    assert events  # events were actually produced

    candidates = list((tmp_path).glob("ledger.db*"))
    assert candidates
    for path in candidates:
        assert b"SECRET" not in path.read_bytes()

    summary = ledger.summary("run-1")
    assert summary["reservations"] == 2
