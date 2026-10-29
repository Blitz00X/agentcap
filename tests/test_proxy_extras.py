"""Service tiers, cache-write pricing and dated schedules through the proxy."""

from __future__ import annotations

import asyncio
import json
import math
from datetime import datetime, timezone
from decimal import Decimal

import httpx
import pytest

from agentcap.estimate import (
    apply_input_safety_factor,
    estimate_input_tokens,
    reservation_usd,
)
from agentcap.ledger import Caps, Ledger
from agentcap.pricing import load_pricing
from agentcap.proxy import ProxyConfig

from proxy_support import (
    Upstream,
    build,
    gemini_usage_payload,
    openai_chat_body,
    openai_chat_response,
    proxy_client,
)

CHAT_URL = "/openai/v1/chat/completions"
GEMINI_URL = "/gemini/v1beta/models/gemini-4-argon:generateContent"


def _post(tmp_path, upstream, body, *, name="ledger.db", **kwargs):
    app, ledger, events = build(tmp_path, upstream, name=name, **kwargs)

    async def scenario():
        async with proxy_client(app) as client:
            return await client.post(CHAT_URL, json=body)

    return asyncio.run(scenario()), ledger, events, upstream


def test_flex_request_is_billed_at_half_price(tmp_path):
    upstream = Upstream(
        lambda _: openai_chat_response(prompt_tokens=1000, completion_tokens=500)
    )
    body = openai_chat_body(service_tier="flex")
    response, ledger, _, _ = _post(tmp_path, upstream, body)
    assert response.status_code == 200
    # 1000 * 0.15 + 500 * 0.60 per Mtok = 0.00045; flex = 0.5x
    assert ledger.summary("run-1")["actual_usd"] == Decimal("0.000225")


def test_response_service_tier_wins_over_requested(tmp_path):
    def responder(_request):
        payload = json.loads(openai_chat_response(prompt_tokens=1000, completion_tokens=500).content)
        payload["service_tier"] = "priority"
        return httpx.Response(200, json=payload)

    upstream = Upstream(responder)
    body = openai_chat_body(service_tier="flex")
    response, ledger, _, _ = _post(tmp_path, upstream, body)
    assert response.status_code == 200
    assert ledger.summary("run-1")["actual_usd"] == Decimal("0.0009")


def test_unknown_service_tier_fails_closed(tmp_path):
    upstream = Upstream(lambda _: openai_chat_response())
    body = openai_chat_body(service_tier="turbo")
    response, ledger, _, _ = _post(tmp_path, upstream, body)
    assert response.status_code == 403
    error = json.loads(response.content)["error"]
    assert error["type"] == "agentcap_unknown_service_tier"
    assert error["code"] == "agentcap_unknown_service_tier"
    assert upstream.calls == 0
    assert ledger.summary("run-1")["reservations"] == 0


def test_unknown_service_tier_passes_through_with_allow_unmetered(tmp_path):
    upstream = Upstream(
        lambda _: openai_chat_response(prompt_tokens=1000, completion_tokens=500)
    )
    body = openai_chat_body(service_tier="turbo")
    response, ledger, _, _ = _post(
        tmp_path, upstream, body, allow_unmetered=True
    )
    assert response.status_code == 200
    assert upstream.calls == 1
    assert ledger.summary("run-1")["actual_usd"] == Decimal("0.00045")


def test_priority_reservation_is_multiplied(tmp_path):
    upstream = Upstream(lambda _: openai_chat_response())
    body = openai_chat_body(model="gpt-5", max_completion_tokens=1000)
    response, ledger, _, _ = _post(
        tmp_path,
        upstream,
        body,
        caps=Caps(run_usd=Decimal("0.012")),
        clamp_output=False,
    )
    assert response.status_code == 200

    priority = openai_chat_body(
        model="gpt-5", max_completion_tokens=1000, service_tier="priority"
    )
    blocked, blocked_ledger, _, blocked_upstream = _post(
        tmp_path,
        Upstream(lambda _: openai_chat_response()),
        priority,
        name="priority.db",
        caps=Caps(run_usd=Decimal("0.012")),
        clamp_output=False,
    )
    assert blocked.status_code == 429
    error = json.loads(blocked.content)["error"]
    assert error["code"] == "agentcap_request_too_large"
    assert "scope=run" in error["message"]
    assert blocked_upstream.calls == 0
    assert blocked_ledger.summary("run-1")["reservations"] == 0


def test_cache_write_reservation_is_conservative(tmp_path):
    body = openai_chat_body(
        model="gpt-6-astra",
        max_completion_tokens=1,
        prompt_cache_retention="24h",
    )
    response, ledger, _, upstream = _post(
        tmp_path, upstream=Upstream(lambda _: openai_chat_response()),
        body=body, caps=Caps(run_usd=Decimal("0.001")),
    )
    assert response.status_code == 429
    assert upstream.calls == 0
    assert ledger.summary("run-1")["reservations"] == 0

    plain = openai_chat_body(model="gpt-6-astra", max_completion_tokens=1)
    allowed, _, _, _ = _post(
        tmp_path,
        Upstream(lambda _: openai_chat_response()),
        plain,
        name="plain.db",
        caps=Caps(run_usd=Decimal("0.001")),
    )
    assert allowed.status_code == 200


def test_cache_control_charges_cache_write_rate_on_settlement(tmp_path):
    body = openai_chat_body(
        model="gpt-6-astra",
        max_completion_tokens=1,
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": "hello",
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
            }
        ],
    )
    upstream = Upstream(
        lambda _: openai_chat_response(prompt_tokens=1000, completion_tokens=0)
    )
    response, ledger, _, _ = _post(
        tmp_path, upstream, body, caps=Caps(run_usd=Decimal("1"))
    )
    assert response.status_code == 200
    # 1000 * max(10, 25) per Mtok
    assert ledger.summary("run-1")["actual_usd"] == Decimal("0.025")


def test_schedule_is_applied_using_the_proxy_clock(tmp_path):
    def run_at(when, name):
        body = {"contents": [{"parts": [{"text": "hi"}]}]}
        upstream = Upstream(
            lambda _: httpx.Response(
                200,
                json={
                    "candidates": [],
                    "usageMetadata": {"promptTokenCount": 1_000_000},
                },
            )
        )
        app, ledger, _ = build(
            tmp_path,
            upstream,
            name=name,
            clock=lambda: when,
            caps=Caps(run_usd=Decimal("100")),
        )

        async def scenario():
            async with proxy_client(app) as client:
                return await client.post(GEMINI_URL, json=body)

        response = asyncio.run(scenario())
        assert response.status_code == 200
        return ledger.summary("run-1")["actual_usd"]

    before = run_at(datetime(2026, 11, 30, 23, tzinfo=timezone.utc), "before.db")
    after = run_at(datetime(2026, 12, 1, 0, tzinfo=timezone.utc), "after.db")
    assert before == Decimal("2")
    assert after == Decimal("4")


def test_input_safety_factor_pads_reservation_not_settlement(tmp_path):
    body = openai_chat_body(model="gpt-4o-mini", max_completion_tokens=100)
    price = load_pricing().get("openai/gpt-4o-mini")
    ledger = Ledger(tmp_path / "reserve.db")
    reservations: list[Decimal] = []
    real_reserve = ledger.reserve

    def recording_reserve(*args, **kwargs):
        reservations.append(args[3])
        return real_reserve(*args, **kwargs)

    ledger.reserve = recording_reserve  # type: ignore[method-assign]
    upstream = Upstream(
        lambda _: openai_chat_response(prompt_tokens=100, completion_tokens=10)
    )
    response, ledger, _, _ = _post(
        tmp_path,
        upstream,
        body,
        ledger=ledger,
        input_safety_factor=Decimal("2"),
    )

    assert response.status_code == 200
    expected = reservation_usd(
        price,
        apply_input_safety_factor(estimate_input_tokens(body), Decimal("2")),
        100,
    )
    assert reservations == [expected]
    # Settlement is priced from the provider's usage, not the padded estimate.
    assert ledger.summary("run-1")["actual_usd"] == Decimal("0.000021")


def test_input_safety_factor_validation():
    with pytest.raises(TypeError):
        ProxyConfig(run_id="run-1", input_safety_factor=1.25)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        ProxyConfig(run_id="run-1", input_safety_factor=Decimal("0.9"))
    assert ProxyConfig(run_id="run-1").input_safety_factor == Decimal("1.25")


def test_overshoot_settlement_fires_kill_on_next_request(tmp_path):
    cap = Decimal("0.001")
    kills: list[dict] = []
    upstream = Upstream(
        lambda _: openai_chat_response(prompt_tokens=100, completion_tokens=2000)
    )
    app, ledger, events = build(
        tmp_path,
        upstream,
        caps=Caps(run_usd=cap),
        on_cap_exceeded=lambda payload: kills.append(payload),
    )
    body = openai_chat_body(model="gpt-4o-mini", max_completion_tokens=100)

    async def scenario():
        async with proxy_client(app) as client:
            return [
                await client.post(CHAT_URL, json=body),
                await client.post(CHAT_URL, json=body),
            ]

    first, second = asyncio.run(scenario())
    assert first.status_code == 200
    assert second.status_code == 429
    assert second.headers["x-agentcap-blocked"] == "cap"
    # The first settlement exceeded the cap; the next request fires the kill.
    assert ledger.spent("run-1") > cap
    assert len(kills) == 1
    assert kills[0]["reason"] == "cap_exceeded"
    assert [e["type"] for e in events].count("overshoot") == 1


def test_settlement_above_reservation_trips_cap_immediately(tmp_path):
    cap = Decimal("0.001")
    kills: list[dict] = []
    upstream = Upstream(
        lambda _: openai_chat_response(prompt_tokens=100, completion_tokens=2000)
    )
    app, ledger, events = build(
        tmp_path,
        upstream,
        caps=Caps(run_usd=cap),
        on_cap_exceeded=lambda payload: kills.append(payload),
    )
    body = openai_chat_body(model="gpt-4o-mini", max_completion_tokens=100)

    async def scenario():
        async with proxy_client(app) as client:
            response = await client.post(CHAT_URL, json=body)
            return response, client

    asyncio.run(scenario())

    # The request settled above its reservation and over the cap; the kill must
    # not wait for the client's next request.
    assert ledger.spent("run-1") > cap
    assert len(kills) == 1
    assert kills[0]["reason"] == "cap_exceeded"
    types = [e["type"] for e in events]
    assert types.count("overshoot") == 1
    assert types.count("cap_exceeded") == 1
    assert types.index("overshoot") < types.index("cap_exceeded")


def test_gemini_system_instruction_is_inside_the_reservation(tmp_path):
    """Regression for the real-CLI overshoot: Gemini's system prompt is input.

    Gemini CLI puts its large agent prompt in ``systemInstruction``.  When that
    field was ignored the reservation covered only the tiny ``contents`` turn,
    so a single request settled above its own reservation and past the cap.
    """

    system_prompt = "You are Gemini CLI. " * 2000
    body = {
        "contents": [{"role": "user", "parts": [{"text": "run the tests"}]}],
        "systemInstruction": {"parts": [{"text": system_prompt}]},
        "generationConfig": {
            "maxOutputTokens": 64,
            "thinkingConfig": {"thinkingBudget": 0},
        },
    }
    # The upstream bills the whole prompt, at the conservative 3 chars/token.
    billed_prompt = math.ceil(len(system_prompt) / 3)
    upstream = Upstream(
        lambda _: httpx.Response(
            200,
            json={
                "candidates": [{"content": {"parts": [{"text": "ok"}]}}],
                "usageMetadata": gemini_usage_payload(
                    prompt=billed_prompt, candidates=64
                ),
            },
        )
    )
    app, _, events = build(tmp_path, upstream, caps=Caps(run_usd=Decimal("1")))

    async def scenario():
        async with proxy_client(app) as client:
            return await client.post(GEMINI_URL, json=body)

    response = asyncio.run(scenario())
    assert response.status_code == 200

    settled = [e for e in events if e["type"] == "request_settled"]
    assert len(settled) == 1
    assert Decimal(settled[0]["reserved_usd"]) >= Decimal(settled[0]["cost_usd"])
    assert "overshoot" not in [e["type"] for e in events]

