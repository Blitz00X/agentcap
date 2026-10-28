from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from agentcap.estimate import reservation_usd
from agentcap.pricing import (
    InvalidPricingError,
    PricingStore,
    UnknownModelError,
    UnknownServiceTierError,
    cost,
    load_pricing,
)


@pytest.fixture(autouse=True)
def _no_user_override(monkeypatch):
    monkeypatch.delenv("AGENTCAP_PRICING_FILE", raising=False)


@pytest.fixture()
def store() -> PricingStore:
    return load_pricing()


def test_bundled_prices_are_verified_with_sources(store):
    assert store.verified is True
    assert store.note
    assert store.version == "2026.10.25"
    assert set(store.sources) == {"openai", "google"}
    for key in store.keys():
        price = store.get(key)
        assert price.verified is True, key
        assert price.verified_at == "2026-10-25", key
        assert price.source.startswith("https://"), key


def test_exact_lookup(store):
    price = store.get("openai/gpt-4o")
    assert price.key == "openai/gpt-4o"
    assert price.provider == "openai"
    assert price.model == "gpt-4o"
    assert price.input_per_mtok == Decimal("2.50")


def test_prefix_lookup_for_dated_model(store):
    price = store.get("openai/gpt-4o-2024-08-06")
    assert price.key == "openai/gpt-4o"


def test_longest_prefix_wins_over_shorter_family(store):
    assert store.get("openai/gpt-4o-mini").key == "openai/gpt-4o-mini"
    assert store.get("openai/gpt-4o-mini-2024-07-18").key == "openai/gpt-4o-mini"
    assert store.get("gpt-4o-mini-2024-07-18").key == "openai/gpt-4o-mini"


def test_bare_gemini_id_and_models_prefix(store):
    assert store.get("gemini-2.5-flash").key == "google/gemini-2.5-flash"
    assert store.get("google/models/gemini-2.5-pro").key == "google/gemini-2.5-pro"


def test_unknown_model_raises(store):
    with pytest.raises(UnknownModelError) as excinfo:
        store.get("openai/not-a-real-model")
    assert "not-a-real-model" in str(excinfo.value)


def test_override_file_merges_over_bundled(store, tmp_path, monkeypatch):
    override = tmp_path / "pricing.json"
    override.write_text(
        json.dumps(
            {
                "version": "override-1",
                "models": {
                    "openai/gpt-4o": {"input_per_mtok": "9.99"},
                    "openai/custom-model": {
                        "input_per_mtok": "1.00",
                        "cached_input_per_mtok": "0.50",
                        "output_per_mtok": "2.00",
                        "max_output_tokens": 1024,
                        "source": "local",
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("AGENTCAP_PRICING_FILE", str(override))

    merged = load_pricing()
    assert merged.version == "override-1"

    gpt4o = merged.get("openai/gpt-4o")
    assert gpt4o.input_per_mtok == Decimal("9.99")
    assert gpt4o.output_per_mtok == Decimal("10.00")  # untouched base field

    custom = merged.get("openai/custom-model")
    assert custom.max_output_tokens == 1024
    assert custom.source == "local"


def test_malformed_override_is_rejected(tmp_path, monkeypatch):
    override = tmp_path / "pricing.json"
    override.write_text('{"models": {"openai/broken": {}}}', encoding="utf-8")
    monkeypatch.setenv("AGENTCAP_PRICING_FILE", str(override))
    with pytest.raises(InvalidPricingError):
        load_pricing()


def test_long_context_tier_reservation(store):
    price = store.get("google/gemini-2.5-pro")
    assert price.long_context is not None
    assert price.long_context.threshold_tokens == 200000

    base = reservation_usd(price, 200000, 10000)
    assert base == Decimal("0.35")  # 200k*1.25 + 10k*10

    long = reservation_usd(price, 200001, 10000)
    assert long == Decimal("0.650003")

    long_bigger = reservation_usd(price, 250000, 10000)
    assert long_bigger > base


def test_cost_accounts_for_cached_tokens(store):
    price = store.get("openai/gpt-4o-mini")
    total = cost(price, input_tokens=1_000_000, cached_input_tokens=200_000, output_tokens=100_000)
    assert total == Decimal("0.195")


def test_cost_uses_long_context_rates(store):
    price = store.get("google/gemini-2.5-pro")
    total = cost(price, input_tokens=250_000, cached_input_tokens=50_000, output_tokens=10_000)
    # 200k uncached * 2.50 + 50k cached * 0.31 + 10k * 15
    assert total == Decimal("0.6625")


def test_cost_clamps_cached_tokens(store):
    price = store.get("openai/gpt-4o-mini")
    assert cost(price, 1000, cached_input_tokens=5000, output_tokens=0) == cost(
        price, 1000, cached_input_tokens=1000, output_tokens=0
    )


def test_reservation_rounds_up_to_microdollar(store):
    price = store.get("openai/gpt-4o-mini")
    # one input token costs 0.00000015 USD
    assert reservation_usd(price, 1, 0) == Decimal("0.000001")


def test_cost_rejects_negative_tokens(store):
    with pytest.raises(ValueError):
        cost(store.get("openai/gpt-4o"), -1, 0, 0)


# ------------------------------------------------------------ prefix matching


def test_prefix_lookup_does_not_swallow_longer_family(store):
    assert store.get("gpt-5.4-mini").key == "openai/gpt-5.4-mini"
    assert store.get("gpt-5.4-mini-2026-03-01").key == "openai/gpt-5.4-mini"
    assert store.get("gpt-5").key == "openai/gpt-5"
    assert store.get("gpt-5-2025-08-07").key == "openai/gpt-5"


def test_bare_digit_suffix_is_not_a_prefix_boundary(store):
    with pytest.raises(UnknownModelError):
        store.get("gpt-50")


def test_models_prefix_is_stripped(store):
    assert store.get("models/gemini-2.5-flash").key == "google/gemini-2.5-flash"
    assert store.get("google/models/gemini-2.5-flash").key == "google/gemini-2.5-flash"


# -------------------------------------------------------------- priced changes


def test_google_schedule_applies_from_effective_date(store):
    price = store.get("google/gemini-4-argon")
    before = datetime(2026, 11, 30, 23, 59, tzinfo=timezone.utc)
    after = datetime(2026, 12, 1, 0, 0, tzinfo=timezone.utc)

    assert reservation_usd(price, 1_000_000, 0, now=before) == Decimal("2")
    assert reservation_usd(price, 1_000_000, 0, now=after) == Decimal("4")
    assert reservation_usd(price, 0, 1_000_000, now=before) == Decimal("10")
    assert reservation_usd(price, 0, 1_000_000, now=after) == Decimal("20")
    assert cost(price, 1_000_000, 500_000, 0, now=after) == Decimal("2.10")


def test_long_context_cached_rate_is_used(store):
    price = store.get("google/gemini-3.1-pro-preview")
    assert price.long_context.cached_input_per_mtok == Decimal("0.40")
    total = cost(price, 200_001, 100_000, 0, now=datetime(2026, 10, 25, tzinfo=timezone.utc))
    # 100001 uncached * 4 + 100000 cached * 0.40 per Mtok
    assert total == Decimal("0.440004")


def test_cache_write_raises_the_input_rate(store):
    price = store.get("openai/gpt-6-astra")
    assert price.cache_write_per_mtok == Decimal("25")
    normal = reservation_usd(price, 100_000, 0)
    cached = reservation_usd(price, 100_000, 0, cache_write=True)
    assert normal == Decimal("1")
    assert cached == Decimal("2.5")
    assert cost(price, 100_000, 0, 0, cache_write=True) == Decimal("2.5")


def test_service_tier_multipliers(store):
    assert store.service_tier_multiplier("openai", "default") == Decimal("1")
    assert store.service_tier_multiplier("openai", "flex") == Decimal("0.5")
    assert store.service_tier_multiplier("openai", "priority") == Decimal("2")
    assert store.service_tier_multiplier("openai", "ultrafast") == Decimal("6")
    with pytest.raises(UnknownServiceTierError):
        store.service_tier_multiplier("openai", "turbo")
    with pytest.raises(UnknownServiceTierError):
        store.service_tier_multiplier("google", "flex")


def test_bool_verified_flag_is_still_accepted(tmp_path):
    override = tmp_path / "pricing.json"
    override.write_text(
        json.dumps(
            {
                "models": {
                    "openai/legacy-verified": {
                        "input_per_mtok": "1",
                        "cached_input_per_mtok": "0.5",
                        "output_per_mtok": "2",
                        "max_output_tokens": 1024,
                        "verified": True,
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    store = load_pricing(override)
    price = store.get("openai/legacy-verified")
    assert price.verified is True
    assert price.verified_at == ""


def test_every_model_parses_and_is_verified(store):
    assert len(store) >= 30
    for key in store.keys():
        price = store.get(key)
        assert price.input_per_mtok >= 0
        assert price.cached_input_per_mtok >= 0
        assert price.output_per_mtok >= 0
        assert price.max_output_tokens >= 1
        for entry in price.schedule:
            assert entry.cached_input_per_mtok >= 0
