"""Unit tests: Anthropic usage parsing, pricing, estimation and clamping."""
from __future__ import annotations

import json
from decimal import Decimal

import pytest

from agentcap.clamp import apply_output_clamp, fit_output
from agentcap.estimate import (
    candidate_count,
    explicit_output_tokens,
    max_output_tokens,
    reservation_usd,
)
from agentcap.pricing import UnknownModelError, cost, load_pricing
from agentcap.usage import AnthropicStreamUsage, Usage, anthropic_usage, parse_anthropic_stream

P = load_pricing()


# ------------------------------------------------------------------ usage
def test_non_stream_usage_normalises_cache_fields():
    u = anthropic_usage({"usage": {
        "input_tokens": 100, "cache_creation_input_tokens": 300,
        "cache_read_input_tokens": 600, "output_tokens": 50,
        "cache_creation": {"ephemeral_5m_input_tokens": 100, "ephemeral_1h_input_tokens": 200},
    }})
    assert u == Usage(1000, 600, 50, 300, 200)


def test_usage_without_breakdown_counts_all_writes_as_5m():
    u = anthropic_usage({"usage": {"input_tokens": 10, "cache_creation_input_tokens": 5, "output_tokens": 1}})
    assert (u.input_tokens, u.cache_write_input_tokens, u.cache_write_1h_input_tokens) == (15, 5, 0)


def test_breakdown_larger_than_total_is_trusted():
    u = anthropic_usage({"usage": {"input_tokens": 1, "cache_creation_input_tokens": 0,
                                    "cache_creation": {"ephemeral_1h_input_tokens": 7}}})
    assert u.cache_write_input_tokens == 7 and u.cache_write_1h_input_tokens == 7
    assert u.input_tokens == 8


@pytest.mark.parametrize("payload", [None, {}, {"usage": None}, {"usage": {}}, {"usage": "x"}])
def test_missing_usage_is_none(payload):
    assert anthropic_usage(payload) is None


def test_as_dict_is_backward_compatible_without_writes():
    assert Usage(1, 0, 2).as_dict() == {"input_tokens": 1, "cached_input_tokens": 0, "output_tokens": 2}
    assert Usage(5, 0, 2, 3, 1).as_dict()["cache_write_1h_input_tokens"] == 1


def _sse(events):
    return "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events).encode()


STREAM = [
    {"type": "message_start", "message": {"usage": {
        "input_tokens": 20, "cache_creation_input_tokens": 10, "cache_read_input_tokens": 70,
        "output_tokens": 1}}},
    {"type": "content_block_delta", "delta": {"type": "thinking_delta", "thinking": "hmm"}},
    {"type": "ping"},
    {"type": "message_delta", "usage": {"output_tokens": 15}},
    {"type": "message_delta", "usage": {"output_tokens": 42}},
    {"type": "message_stop"},
]


def test_stream_usage_merges_start_and_cumulative_delta():
    u = parse_anthropic_stream([_sse(STREAM)])
    assert u == Usage(100, 70, 42, 10, 0)


def test_stream_usage_split_across_arbitrary_chunk_boundaries():
    raw = _sse(STREAM)
    parser = AnthropicStreamUsage()
    for i in range(0, len(raw), 7):
        parser.feed(raw[i:i + 7])
    assert parser.finish() == Usage(100, 70, 42, 10, 0)


def test_stream_delta_may_override_input_counts():
    events = STREAM[:1] + [{"type": "message_delta", "usage": {"output_tokens": 5, "input_tokens": 30}}]
    assert parse_anthropic_stream([_sse(events)]).input_tokens == 110


def test_stream_without_usage_is_none_and_garbage_ignored():
    assert parse_anthropic_stream([b"data: not json\n\n", b"event: ping\ndata: {}\n\n"]) is None


# ---------------------------------------------------------------- pricing
@pytest.mark.parametrize("model,inp,cached,out", [
    ("claude-fable-5-1", "10", "0.25", "50"),
    ("claude-opus-5-5", "4", "0.20", "20"),
    ("claude-opus-4-7", "5", "0.50", "25"),
    ("claude-opus-4-1", "15", "1.50", "75"),
    ("claude-sonnet-5-5", "2", "0.10", "10"),
    ("claude-sonnet-4-6", "3", "0.30", "15"),
    ("claude-haiku-5-5", "0.10", "0.01", "0.50"),
    ("claude-haiku-4-5", "1", "0.10", "5"),
])
def test_price_table(model, inp, cached, out):
    price = P.get(f"anthropic/{model}")
    assert (price.input_per_mtok, price.cached_input_per_mtok, price.output_per_mtok) == (
        Decimal(inp), Decimal(cached), Decimal(out))
    assert price.source.startswith("https://docs.claude.com/")
    assert price.verified_date == "2026-10-30"


def test_sources_list_anthropic():
    assert "anthropic" in P.sources


def test_dated_snapshot_resolves_but_unknown_family_member_fails_closed():
    assert P.get("anthropic/claude-sonnet-4-5-20250929").model == "claude-sonnet-4-5"
    assert P.get("claude-haiku-4-5-20251001").model == "claude-haiku-4-5"
    for bad in ("anthropic/claude-sonnet-5-7", "anthropic/claude-opus-4-9", "anthropic/claude-sonnet-5-5-x",
                "anthropic/claude-opus-4-2025"):
        with pytest.raises(UnknownModelError):
            P.get(bad)


def test_cost_cache_writes_priced_at_published_multipliers():
    price = P.get("anthropic/claude-sonnet-5-5")  # $2 in, $0.10 read, $10 out
    # 1M plain + 1M 5m write + 1M 1h write + 1M read + 1M output
    total = cost(price, 4_000_000, 1_000_000, 1_000_000, cache_write_tokens=2_000_000, cache_write_1h_tokens=1_000_000)
    assert total == Decimal("2") + Decimal("2.5") + Decimal("4") + Decimal("0.10") + Decimal("10")


def test_cost_ignores_blanket_cache_write_flag_for_anthropic():
    price = P.get("anthropic/claude-sonnet-5-5")
    assert cost(price, 1_000_000, 0, 0, cache_write=True) == Decimal("2")


def test_haiku_55_long_context_tier_and_writes():
    price = P.get("anthropic/claude-haiku-5-5")
    assert cost(price, 100_000, 0, 0) == Decimal("0.01")
    # >100k: whole request at the higher tier, writes at 1.25x of $0.50
    assert cost(price, 200_000, 0, 1000, cache_write_tokens=200_000) == (
        Decimal(200_000) * Decimal("0.625") + Decimal(1000) * Decimal("2.50")) / Decimal(1_000_000)


def test_cost_rejects_negative_writes():
    with pytest.raises(ValueError):
        cost(P.get("anthropic/claude-haiku-4-5"), 1, 0, 0, cache_write_tokens=-1)


def test_openai_cost_unchanged_by_new_kwargs():
    price = P.get("openai/gpt-6-astra")
    assert cost(price, 1000, 0, 0, cache_write=True) == cost(price, 1000, 0, 0, cache_write=True, cache_write_tokens=500)


# ------------------------------------------------------------- estimation
def test_max_output_uses_max_tokens_including_thinking():
    price = P.get("anthropic/claude-opus-5-5")
    body = {"max_tokens": 16000, "thinking": {"type": "enabled", "budget_tokens": 10000}}
    assert max_output_tokens("anthropic", body, price) == 16000
    assert explicit_output_tokens("anthropic", body) == 16000
    assert candidate_count("anthropic", {"n": 5}) == 1


def test_missing_max_tokens_reserves_model_maximum():
    price = P.get("anthropic/claude-opus-5-5")
    assert max_output_tokens("anthropic", {}, price) == 128000
    assert explicit_output_tokens("anthropic", {}) is None


def test_reservation_with_cache_control_assumes_1h_writes():
    price = P.get("anthropic/claude-sonnet-5-5")
    plain = reservation_usd(price, 1_000_000, 0)
    cached = reservation_usd(price, 1_000_000, 0, cache_write=True)
    assert plain == Decimal("2") and cached == Decimal("4")


def test_reservation_with_cache_control_covers_haiku_long_tier():
    price = P.get("anthropic/claude-haiku-5-5")
    assert reservation_usd(price, 200_000, 0, cache_write=True) == Decimal("0.2")  # 2 x $0.50


# ----------------------------------------------------------------- clamp
def test_clamp_lowers_max_tokens_only():
    body = {"max_tokens": 32000}
    apply_output_clamp("anthropic", "v1/messages", body, 4000)
    assert body == {"max_tokens": 4000}


def test_clamp_never_raises_limit():
    body = {"max_tokens": 100}
    apply_output_clamp("anthropic", "v1/messages", body, 4000)
    assert body["max_tokens"] == 100


def test_clamp_lowers_thinking_budget_below_max_tokens():
    body = {"max_tokens": 32000, "thinking": {"type": "enabled", "budget_tokens": 20000}}
    apply_output_clamp("anthropic", "v1/messages", body, 5000)
    assert body["max_tokens"] == 5000 and body["thinking"]["budget_tokens"] == 4999


def test_clamp_drops_thinking_when_no_valid_budget_fits():
    body = {"max_tokens": 32000, "thinking": {"type": "enabled", "budget_tokens": 20000}}
    apply_output_clamp("anthropic", "v1/messages", body, 900)
    assert body == {"max_tokens": 900}


def test_clamp_leaves_adaptive_thinking_alone():
    body = {"max_tokens": 32000, "thinking": {"type": "adaptive"}}
    apply_output_clamp("anthropic", "v1/messages", body, 2000)
    assert body == {"max_tokens": 2000, "thinking": {"type": "adaptive"}}


def test_fit_output_respects_remaining_budget():
    price = P.get("anthropic/claude-sonnet-5-5")
    fit = fit_output(price, provider="anthropic", body={"max_tokens": 64000},
                     input_tokens=1000, remaining=Decimal("0.05"))
    assert fit.reservation <= Decimal("0.05")
    assert fit.allowed_output == 4800  # (0.05 - 0.002) / 10e-6
