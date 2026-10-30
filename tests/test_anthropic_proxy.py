"""Proxy tests for /anthropic against the fake Anthropic upstream."""
from __future__ import annotations

import json
from decimal import Decimal

import httpx

from agentcap.license import FREE_FEATURES, PRO_FEATURES
from conftest import body


async def test_non_stream_metered_and_settled(harness):
    h = harness()
    r = await h.messages(body(max_tokens=500))
    assert r.status_code == 200
    s = h.settled()
    assert len(s) == 1 and s[0]["provider"] == "anthropic"
    usage = s[0]["usage"]
    assert usage["output_tokens"] == 500
    # Sonnet 5.5: $2 in / $10 out
    expected = (Decimal(usage["input_tokens"]) * 2 + Decimal(500) * 10) / Decimal(1_000_000)
    assert Decimal(s[0]["cost_usd"]) == expected
    assert h.ledger.spent("run-test") == expected


async def test_stream_metered_from_sse_events(harness):
    h = harness()
    r = await h.messages(body(max_tokens=700, stream=True))
    assert r.status_code == 200
    assert "message_stop" in r.text
    s = h.settled()
    assert len(s) == 1 and s[0]["usage"]["output_tokens"] == 700


async def test_cache_writes_and_reads_settle_at_right_rates(harness):
    h = harness()
    b = body(max_tokens=100, system=[{"type": "text", "text": "x" * 4000,
                                      "cache_control": {"type": "ephemeral", "ttl": "1h"}}])
    await h.messages(b)
    usage = h.settled()[0]["usage"]
    assert usage["cache_write_1h_input_tokens"] == usage["cache_write_input_tokens"] > 0
    w = usage["cache_write_input_tokens"]
    plain = usage["input_tokens"] - w
    expected = (Decimal(plain) * 2 + Decimal(w) * 4 + Decimal(100) * 10) / Decimal(1_000_000)
    assert Decimal(h.settled()[0]["cost_usd"]) == expected
    await h.messages(b, headers={"x-fake-cache-hit": "1"})
    hit = h.settled()[1]["usage"]
    assert hit["cached_input_tokens"] > 0 and "cache_write_input_tokens" not in hit


async def test_unknown_model_fails_closed_and_is_not_forwarded(harness):
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={})
    h = harness(upstream=httpx.MockTransport(handler))
    r = await h.messages(body(model="claude-sonnet-9-9"))
    assert r.status_code >= 400 and seen == []
    r = await h.messages({"max_tokens": 5, "messages": []})
    assert r.status_code >= 400 and seen == []


async def test_openai_model_name_cannot_borrow_price_on_anthropic_route(harness):
    h = harness()
    r = await h.messages(body(model="gpt-6-astra"))
    assert r.status_code >= 400


async def test_invalid_json_blocked(harness):
    h = harness()
    async with h.client() as c:
        r = await c.post("/anthropic/v1/messages", content=b"{nope", headers={"x-api-key": "k"})
    assert r.status_code == 400


async def test_count_tokens_and_models_are_free_passthroughs(harness):
    h = harness()
    r = await h.messages(body(), path="/anthropic/v1/messages/count_tokens")
    assert r.status_code == 200 and r.json()["input_tokens"] > 0
    async with h.client() as c:
        r = await c.get("/anthropic/v1/models", headers={"x-api-key": "k"})
    assert r.status_code == 200
    assert h.settled() == [] and h.ledger.spent("run-test") == 0


async def test_other_paths_blocked_unless_allow_unmetered(harness):
    h = harness()
    r = await h.messages(body(), path="/anthropic/v1/messages/batches")
    assert r.status_code == 403
    h2 = harness(allow_unmetered=True)
    r = await h2.messages(body(), path="/anthropic/v1/messages/batches")
    assert r.status_code == 404  # forwarded; the fake has no such route


async def test_cap_rejects_before_upstream_and_kills(harness):
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={})
    h = harness(cap="0.000001", upstream=httpx.MockTransport(handler), clamp_output=False)
    r = await h.messages(body(max_tokens=64000))
    assert r.status_code == 429 and seen == []


async def test_clamp_rewrites_max_tokens_and_thinking(harness):
    captured = []

    def handler(request):
        captured.append(json.loads(request.content))
        return httpx.Response(200, json={"usage": {"input_tokens": 10, "output_tokens": 5}})
    h = harness(cap="0.05", upstream=httpx.MockTransport(handler))
    r = await h.messages(body(model="claude-sonnet-5-5", max_tokens=64000,
                              thinking={"type": "enabled", "budget_tokens": 60000}))
    assert r.status_code == 200
    sent = captured[0]
    assert sent["max_tokens"] < 64000
    assert sent["thinking"]["budget_tokens"] < sent["max_tokens"]
    assert "output_clamped" in h.types()


async def test_clamped_request_accepted_by_fake_upstream(harness):
    h = harness(cap="0.02")
    r = await h.messages(body(max_tokens=64000, thinking={"type": "enabled", "budget_tokens": 50000}))
    assert r.status_code == 200, r.text


async def test_reservation_never_below_actual(harness):
    h = harness()
    await h.messages(body(max_tokens=2000, stream=True))
    s = h.settled()[0]
    assert Decimal(s["cost_usd"]) <= Decimal(s["reserved_usd"])
    assert "overshoot" not in h.types()


async def test_upstream_error_releases_reservation(harness):
    h = harness(upstream=httpx.MockTransport(lambda r: httpx.Response(529, json={"type": "error"})))
    r = await h.messages(body())
    assert r.status_code == 529
    assert h.ledger.spent("run-test") == 0


async def test_missing_usage_charges_full_reservation(harness):
    h = harness(upstream=httpx.MockTransport(lambda r: httpx.Response(200, json={"content": []})))
    await h.messages(body(max_tokens=100))
    assert "usage_missing" in h.types()
    assert h.ledger.spent("run-test") > 0


async def test_api_key_forwarded_but_never_in_events(harness):
    keys = []

    def handler(request):
        keys.append(request.headers.get("x-api-key"))
        return httpx.Response(200, json={"usage": {"input_tokens": 1, "output_tokens": 1}})
    h = harness(upstream=httpx.MockTransport(handler))
    await h.messages(body())
    assert keys == ["sk-ant-test-SECRET"]
    assert "SECRET" not in json.dumps(h.events, default=str)


async def test_query_string_passed_through(harness):
    urls = []

    def handler(request):
        urls.append(str(request.url))
        return httpx.Response(200, json={"usage": {"input_tokens": 1, "output_tokens": 1}})
    h = harness(upstream=httpx.MockTransport(handler))
    await h.messages(body(), path="/anthropic/v1/messages?beta=true")
    assert urls == ["https://api.anthropic.com/v1/messages?beta=true"]


async def test_inference_geo_us_multiplier(harness):
    h = harness()
    await h.messages(body(max_tokens=100))
    await h.messages(body(max_tokens=100, inference_geo="us"))
    a, b = (Decimal(s["cost_usd"]) for s in h.settled())
    assert b == (a * Decimal("1.1")).quantize(Decimal("0.000001"), rounding="ROUND_CEILING")


async def test_unknown_geo_and_unpriced_fast_mode_fail_closed(harness):
    h = harness()
    assert (await h.messages(body(inference_geo="mars"))).status_code == 403
    assert (await h.messages(body(speed="fast"))).status_code == 403
    assert (await h.messages(body(speed="standard"))).status_code == 200


async def test_hour_and_day_caps_work_on_free(harness):
    h = harness(features=FREE_FEATURES, cap=None, hour_usd=Decimal("0.000001"))
    r = await h.messages(body(max_tokens=64000))
    assert r.status_code == 429
    h2 = harness(features=FREE_FEATURES, cap=None, day_usd=Decimal("0.000001"))
    assert (await h2.messages(body(max_tokens=64000))).status_code == 429


async def test_pro_loop_breaker_blocks_on_fifth_identical_request(harness):
    h = harness(features=PRO_FEATURES)
    codes = [(await h.messages(body(max_tokens=10))).status_code for _ in range(5)]
    assert codes == [200, 200, 200, 200, 429]
    assert h.kills and h.kills[0]["reason"] == "loop_detected"


async def test_free_tier_does_not_block_loop_but_nudges_once(harness):
    h = harness(features=FREE_FEATURES)
    codes = [(await h.messages(body(max_tokens=10))).status_code for _ in range(7)]
    assert codes == [200] * 7
    nudges = [e for e in h.events if e.get("type") == "loop_nudge"]
    assert len(nudges) == 1
    assert Decimal(nudges[0]["spent_usd"]) > 0
    assert h.proxy.loop_nudge is not None and h.kills == []


async def test_free_nudge_not_triggered_by_distinct_requests(harness):
    h = harness(features=FREE_FEATURES)
    for i in range(6):
        await h.messages(body(text=f"step {i}"))
    assert "loop_nudge" not in h.types()


async def test_openai_and_gemini_routes_still_work(harness):
    h = harness()
    async with h.client() as c:
        r = await c.post("/openai/v1/chat/completions", json={"model": "gpt-6-astra", "max_completion_tokens": 10,
                                                              "messages": [{"role": "user", "content": "hi"}]})
        assert r.status_code == 200
    assert h.settled()[0]["provider"] == "openai"
