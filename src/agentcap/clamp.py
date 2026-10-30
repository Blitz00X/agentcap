"""Budget-fit output clamping.

Most agents never set an output limit, so a full worst-case reservation
(``max_output_tokens`` of the model) is much larger than the actual request.
With a small cap that reservation alone would cross the cap and kill a run that
has spent nothing yet.

This module computes how many output tokens actually fit in the remaining
budget and rewrites *only* the request's output limit.  It never raises an
existing limit, never touches prompts and never changes settlement (which still
uses real usage).
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from typing import Any, Mapping

from .estimate import (
    MIN_GEMINI_THINKING_BUDGET,
    candidate_count,
    explicit_output_tokens,
    gemini_thinking_budget,
    gemini_thinking_capable,
)
from .pricing import MICRO_USD, ONE_MILLION, ModelPrice, effective_rates

#: Clamping below this many tokens is pointless: treat the budget as exhausted.
MIN_CLAMP_OUTPUT = 256


@dataclass(frozen=True)
class OutputFit:
    """A clamped output allowance and the exact reservation it implies."""

    allowed_output: int
    input_cost: Decimal
    reservation: Decimal


def _positive_int(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, float) and value.is_integer() and value > 0:
        return int(value)
    if isinstance(value, str) and value.strip().isdigit() and int(value.strip()) > 0:
        return int(value.strip())
    return None


def fit_output(
    model_price: ModelPrice,
    *,
    provider: str,
    body: Mapping[str, Any],
    input_tokens: int,
    remaining: Decimal,
    now: Any = None,
    cache_write: bool = False,
    tier_multiplier: Decimal = Decimal("1"),
    minimum: int = MIN_CLAMP_OUTPUT,
) -> OutputFit:
    """Largest per-candidate output limit whose reservation fits ``remaining``.

    The returned ``allowed_output`` can be below ``minimum`` (or zero); the
    caller decides whether that is still worth sending.
    """

    if remaining <= 0:
        return OutputFit(0, Decimal("0"), Decimal("0"))

    input_rate, _cached_rate, output_rate = effective_rates(
        model_price, input_tokens, now=now
    )
    if cache_write and model_price.provider == "anthropic":
        input_rate = input_rate * Decimal("2")
    elif cache_write and model_price.cache_write_per_mtok is not None:
        input_rate = max(input_rate, model_price.cache_write_per_mtok)

    input_cost = (
        Decimal(input_tokens) * input_rate / ONE_MILLION
    ).quantize(MICRO_USD, rounding=ROUND_CEILING)
    if tier_multiplier != 1:
        input_cost = (input_cost * tier_multiplier).quantize(
            MICRO_USD, rounding=ROUND_CEILING
        )

    output_per_token = output_rate * tier_multiplier / ONE_MILLION
    candidates = candidate_count(provider, body)
    if output_per_token <= 0:
        return OutputFit(0, input_cost, input_cost)

    budget_for_output = remaining - input_cost
    if budget_for_output <= 0:
        allowed = 0
    else:
        raw = budget_for_output / (output_per_token * Decimal(candidates))
        allowed = int(raw.to_integral_value(rounding=ROUND_FLOOR))
    if allowed < 0:
        allowed = 0

    existing = explicit_output_tokens(provider, body, model_price)
    if existing is not None:
        # Never raise a limit the client already set (math should prevent it).
        allowed = min(allowed, existing)

    def _reservation(value: int) -> Decimal:
        total = input_cost + (
            Decimal(value * candidates) * output_rate * tier_multiplier / ONE_MILLION
        )
        return total.quantize(MICRO_USD, rounding=ROUND_CEILING)

    reservation = _reservation(allowed)
    # Ceiling rounding can push the reservation one micro-dollar over; lower the
    # allowance until it genuinely fits.
    while reservation > remaining and allowed > 0:
        allowed -= 1
        reservation = _reservation(allowed)

    return OutputFit(allowed, input_cost, reservation)


def apply_output_clamp(
    provider: str,
    path: str,
    body: dict[str, Any],
    allowed_output: int,
    *,
    model_price: ModelPrice | None = None,
) -> None:
    """Lower the request's output limit to ``allowed_output`` in place.

    * OpenAI chat: ``max_completion_tokens`` (or the legacy ``max_tokens`` when
      the body already used it; never both).
    * OpenAI responses: ``max_output_tokens``.
    * Gemini: ``generationConfig.maxOutputTokens`` and, for thinking models,
      ``generationConfig.thinkingConfig.thinkingBudget``.  Both are billed as
      output, so the two limits together are kept within ``allowed_output``.
      A client-supplied ``thinkingLevel`` is replaced by the budget: Gemini 3
      rejects a request that sets both.
    """

    allowed = _positive_int(allowed_output)
    if allowed is None:
        return

    if provider == "gemini":
        _apply_gemini_clamp(body, allowed, model_price)
        return
    if provider == "anthropic":
        _apply_anthropic_clamp(body, allowed)
        return

    normalised = path.strip("/")
    if normalised == "v1/responses":
        current = _positive_int(body.get("max_output_tokens"))
        body["max_output_tokens"] = min(allowed, current) if current else allowed
        return

    # Chat Completions (and the legacy /v1/completions shape).
    if normalised == "v1/completions":
        current = _positive_int(body.get("max_tokens"))
        body["max_tokens"] = min(allowed, current) if current else allowed
        return
    legacy = _positive_int(body.get("max_tokens"))
    modern = _positive_int(body.get("max_completion_tokens"))
    if legacy is not None:
        body["max_tokens"] = min(allowed, legacy)
        if modern is not None:
            # Both fields already existed; lower both so neither stays above
            # the reservation.  A new second field is never added.
            body["max_completion_tokens"] = min(allowed, modern)
        return
    body["max_completion_tokens"] = min(allowed, modern) if modern else allowed


ANTHROPIC_MIN_THINKING_BUDGET = 1024


def _apply_anthropic_clamp(body: dict[str, Any], allowed: int) -> None:
    """Lower ``max_tokens`` (which already includes thinking) to ``allowed``.

    Manual extended thinking requires ``budget_tokens < max_tokens`` and
    ``budget_tokens >= 1024``; the budget is lowered to fit, and dropped when
    no valid budget fits.  Adaptive thinking needs no change.
    """

    current = _positive_int(body.get("max_tokens"))
    new_max = min(allowed, current) if current else allowed
    body["max_tokens"] = new_max
    thinking = body.get("thinking")
    if not isinstance(thinking, dict):
        return
    budget = _positive_int(thinking.get("budget_tokens"))
    if budget is None or budget < new_max:
        return
    if new_max - 1 >= ANTHROPIC_MIN_THINKING_BUDGET:
        thinking["budget_tokens"] = new_max - 1
    else:
        body.pop("thinking", None)


def _generation_config_key(body: Mapping[str, Any]) -> str:
    if isinstance(body.get("generationConfig"), Mapping):
        return "generationConfig"
    if isinstance(body.get("generation_config"), Mapping):
        return "generation_config"
    return "generationConfig"


def _thinking_config_key(config: Mapping[str, Any]) -> str:
    if isinstance(config.get("thinkingConfig"), Mapping):
        return "thinkingConfig"
    if isinstance(config.get("thinking_config"), Mapping):
        return "thinking_config"
    return "thinkingConfig"


def _apply_gemini_clamp(
    body: dict[str, Any], allowed: int, model_price: ModelPrice | None
) -> None:
    config_key = _generation_config_key(body)
    config = dict(body.get(config_key) or {})

    current_max = _positive_int(
        config.get("maxOutputTokens", config.get("max_output_tokens"))
    )
    capable = gemini_thinking_capable(body, model_price)
    effective_thinking = gemini_thinking_budget(body, model_price) if capable else 0

    if effective_thinking <= 0:
        # Thinking is explicitly disabled (or the model does not think): only
        # the visible output limit has to fit.
        config["maxOutputTokens"] = (
            min(allowed, current_max) if current_max else allowed
        )
        body[config_key] = config
        return

    # Keep as much of the requested visible output as fits, then spend the
    # remainder on thinking.  If the remainder cannot hold a budget that every
    # thinking model accepts, take the minimum thinking budget out of the text
    # allowance instead of disabling thinking (2.5 Pro cannot disable it).
    text_limit = min(current_max, allowed) if current_max else allowed
    available = max(allowed - text_limit, 0)
    if available < effective_thinking:
        # Never raise a budget the client already set: the floor is capped by
        # the effective budget as well as by what still fits.
        floor = min(
            MIN_GEMINI_THINKING_BUDGET, effective_thinking, max(allowed - 1, 0)
        )
        thinking = max(available, floor)
        thinking = min(thinking, effective_thinking, max(allowed - 1, 0))
        text_limit = min(text_limit, allowed - thinking)
    else:
        thinking = effective_thinking

    if text_limit <= 0:
        text_limit = max(allowed - thinking, 1)
        thinking = max(allowed - text_limit, 0)

    config["maxOutputTokens"] = text_limit
    thinking_key = _thinking_config_key(config)
    thinking_config = dict(config.get(thinking_key) or {})
    # Gemini 3 rejects a thinking config that carries both limits ("You can
    # only set only one of thinking budget and thinking level").  A requested
    # level is therefore replaced by the explicit budget; every other key
    # (``includeThoughts`` in particular) is preserved.
    thinking_config.pop("thinkingLevel", None)
    thinking_config.pop("thinking_level", None)
    thinking_config["thinkingBudget"] = thinking
    config[thinking_key] = thinking_config
    body[config_key] = config


__all__ = [
    "MIN_CLAMP_OUTPUT",
    "OutputFit",
    "apply_output_clamp",
    "fit_output",
]
