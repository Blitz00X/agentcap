"""Conservative token estimation and worst-case cost reservation."""

from __future__ import annotations

import json
import math
from datetime import date, datetime
from decimal import Decimal, ROUND_CEILING
from typing import Any, Mapping

from .pricing import MICRO_USD, ModelPrice, effective_rates

_TEXT_FIELDS = ("messages", "input", "contents", "system", "tools")
_MEDIA_TOKENS = 2000

_OPENAI_MEDIA_TYPES = {"image_url", "input_image", "file", "input_file"}
_GEMINI_MEDIA_KEYS = ("inline_data", "inlineData", "file_data", "fileData")


def _as_mapping(body: Mapping[str, Any] | dict[str, Any]) -> Mapping[str, Any]:
    if not isinstance(body, Mapping):
        raise TypeError("request body must be a mapping")
    return body


def count_media_parts(body: Mapping[str, Any]) -> int:
    """Count image/file parts (OpenAI and Gemini) anywhere in the body."""

    body = _as_mapping(body)

    def walk(value: Any) -> int:
        count = 0
        if isinstance(value, Mapping):
            part_type = value.get("type")
            is_openai_media = isinstance(part_type, str) and part_type in _OPENAI_MEDIA_TYPES
            is_gemini_media = any(key in value for key in _GEMINI_MEDIA_KEYS)
            # A part may carry both a type marker and a media payload; count once.
            if is_openai_media or is_gemini_media:
                count += 1
            for child in value.values():
                count += walk(child)
        elif isinstance(value, (list, tuple)):
            for child in value:
                count += walk(child)
        return count

    return walk(body)


def _relevant_text_chars(body: Mapping[str, Any]) -> int:
    chars = 0
    for field in _TEXT_FIELDS:
        if field in body and body[field] is not None:
            serialized = json.dumps(
                body[field], ensure_ascii=False, sort_keys=True, separators=(",", ":")
            )
            chars += len(serialized)
    return chars


def estimate_input_tokens(body: Mapping[str, Any] | dict[str, Any]) -> int:
    """Estimate prompt tokens with a deliberately conservative formula.

    ``ceil(characters / 3) * 1.10 + 50`` plus a flat 2000 tokens for every
    image or file part.  Overestimating is safe: it only widens the
    reservation, never the spend.
    """

    body = _as_mapping(body)
    base = math.ceil(_relevant_text_chars(body) / 3)
    overhead = math.ceil(base * 1.10) + 50
    return overhead + count_media_parts(body) * _MEDIA_TOKENS


def apply_input_safety_factor(input_tokens: int, factor: Decimal) -> int:
    """Widen an input-token estimate for a reservation (rounded up).

    The factor only pads the worst-case reservation; settlement always uses
    the usage the provider reported.  ``ProxyConfig`` requires a factor of at
    least 1 so the reservation can only widen.
    """

    if (
        isinstance(input_tokens, bool)
        or not isinstance(input_tokens, int)
        or input_tokens < 0
    ):
        raise ValueError("input_tokens must be a non-negative integer")
    if isinstance(factor, bool) or not isinstance(factor, Decimal):
        raise TypeError("factor must be a Decimal")
    if not factor.is_finite() or factor < 0:
        raise ValueError("factor must be a finite non-negative decimal")
    return int(
        (Decimal(input_tokens) * factor).to_integral_value(
            rounding=ROUND_CEILING
        )
    )


def cache_write_requested(body: Mapping[str, Any] | dict[str, Any]) -> bool:
    """True when the request can write to a prompt cache.

    OpenAI-style cache retention is signalled by a top-level ``prompt_cache*``
    key (e.g. ``prompt_cache_retention``); cache-write capable content parts
    carry a ``cache_control`` key anywhere in the body.
    """

    body = _as_mapping(body)
    for key in body:
        if isinstance(key, str) and key.startswith("prompt_cache"):
            return True

    def walk(value: Any) -> bool:
        if isinstance(value, Mapping):
            if "cache_control" in value:
                return True
            return any(walk(child) for child in value.values())
        if isinstance(value, (list, tuple)):
            return any(walk(child) for child in value)
        return False

    return walk(body)


def _positive_int(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, float) and value.is_integer() and value > 0:
        return int(value)
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.isdigit() and int(stripped) > 0:
            return int(stripped)
    return None


def _normalise_provider(provider: str) -> str:
    normalised = provider.strip().lower()
    if normalised in {"openai", "open-ai"}:
        return "openai"
    if normalised in {"google", "gemini", "google-gemini", "google_genai"}:
        return "gemini"
    raise ValueError(f"Unsupported provider for token estimation: {provider!r}")


def _openai_max_tokens(body: Mapping[str, Any]) -> int | None:
    # Chat Completions uses max_completion_tokens (new) or max_tokens (old).
    # Responses API uses max_output_tokens.  Prefer the most explicit fields.
    for field in ("max_completion_tokens", "max_output_tokens", "max_tokens"):
        value = _positive_int(body.get(field))
        if value is not None:
            return value
    return None


def _gemini_max_tokens(body: Mapping[str, Any]) -> int | None:
    candidates: list[Mapping[str, Any]] = []
    for config_key in ("generationConfig", "generation_config"):
        value = body.get(config_key)
        if isinstance(value, Mapping):
            candidates.append(value)
    for config in candidates:
        for field in ("maxOutputTokens", "max_output_tokens"):
            value = _positive_int(config.get(field))
            if value is not None:
                return value
    return None


def _openai_candidates(body: Mapping[str, Any]) -> int:
    # ``n`` produces n completions; each may use the full output budget.
    return _positive_int(body.get("n")) or 1


def _gemini_candidates(body: Mapping[str, Any]) -> int:
    for config_key in ("generationConfig", "generation_config"):
        value = body.get(config_key)
        if isinstance(value, Mapping):
            for field in ("candidateCount", "candidate_count"):
                count = _positive_int(value.get(field))
                if count is not None:
                    return count
    return 1


def explicit_output_tokens(provider: str, body: Mapping[str, Any]) -> int | None:
    """The output limit the client itself asked for, if any."""

    body = _as_mapping(body)
    if _normalise_provider(provider) == "openai":
        return _openai_max_tokens(body)
    return _gemini_max_tokens(body)


def candidate_count(provider: str, body: Mapping[str, Any]) -> int:
    """How many completions/candidates one request can produce (>= 1)."""

    body = _as_mapping(body)
    if _normalise_provider(provider) == "openai":
        return _openai_candidates(body)
    return _gemini_candidates(body)


def max_output_tokens(
    provider: str, body: Mapping[str, Any], model_price: ModelPrice
) -> int:
    """Worst-case output tokens for a request, clamped to the model default."""

    body = _as_mapping(body)
    normalised = _normalise_provider(provider)

    if normalised == "openai":
        requested = _openai_max_tokens(body)
        candidates = _openai_candidates(body)
    else:
        requested = _gemini_max_tokens(body)
        candidates = _gemini_candidates(body)

    per_candidate = requested if requested is not None else model_price.max_output_tokens
    return per_candidate * candidates


def reservation_usd(
    model_price: ModelPrice,
    input_tokens: int,
    output_tokens: int,
    now: datetime | date | None = None,
    cache_write: bool = False,
) -> Decimal:
    """Worst-case USD reservation, rounded up to 6 decimals.

    Cached prompt tokens are ignored here: the reservation assumes no cache
    hits.  A long-context prompt is reserved at the higher tier.  When
    ``cache_write`` is true, input is reserved at
    ``max(input, cache_write)`` (conservative).
    """

    if isinstance(input_tokens, bool) or not isinstance(input_tokens, int) or input_tokens < 0:
        raise ValueError("input_tokens must be a non-negative integer")
    if isinstance(output_tokens, bool) or not isinstance(output_tokens, int) or output_tokens < 0:
        raise ValueError("output_tokens must be a non-negative integer")

    input_rate, _cached_rate, output_rate = effective_rates(
        model_price, input_tokens, now=now
    )
    if cache_write and model_price.cache_write_per_mtok is not None:
        input_rate = max(input_rate, model_price.cache_write_per_mtok)
    total = (
        Decimal(input_tokens) * input_rate + Decimal(output_tokens) * output_rate
    ) / Decimal("1000000")
    return total.quantize(MICRO_USD, rounding=ROUND_CEILING)


def estimate_request(
    provider: str,
    body: Mapping[str, Any],
    model_price: ModelPrice,
    now: datetime | date | None = None,
    cache_write: bool | None = None,
) -> tuple[int, int, Decimal]:
    """Convenience helper: ``(input_tokens, output_tokens, reservation_usd)``."""

    input_tokens = estimate_input_tokens(body)
    output_tokens = max_output_tokens(provider, body, model_price)
    if cache_write is None:
        cache_write = cache_write_requested(body)
    return input_tokens, output_tokens, reservation_usd(
        model_price, input_tokens, output_tokens, now=now, cache_write=cache_write
    )
