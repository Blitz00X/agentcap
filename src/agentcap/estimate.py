"""Conservative token estimation and worst-case cost reservation."""

from __future__ import annotations

import json
import math
import re
from datetime import date, datetime
from decimal import Decimal, ROUND_CEILING
from typing import Any, Mapping

from .pricing import MICRO_USD, ModelPrice, effective_rates

#: Every request field that can carry prompt text, tooling or other billed
#: input.  Missing one of these is the expensive failure mode: the reservation
#: is then *smaller* than the real prompt, so a single request can settle above
#: its reservation and blow through the cap before the next check.
#:
#: ``systemInstruction``/``system_instruction`` are the Gemini system prompt
#: (Gemini CLI sends its large agent prompt there, not in ``contents``) and
#: ``instructions`` is the OpenAI Responses system prompt (Codex sends its long
#: agent prompt there, not in ``input``).  ``toolConfig``/``tool_config``,
#: ``cachedContent`` and ``prediction`` are additional Gemini prompt-bearing
#: fields.  ``tools`` carries JSON schemas whose descriptions and parameter
#: objects are counted too (see :func:`_relevant_text_chars`).
_TEXT_FIELDS = (
    "messages",
    "input",
    "contents",
    "system",
    "instructions",
    "systemInstruction",
    "system_instruction",
    "tools",
    "toolConfig",
    "tool_config",
    "cachedContent",
    "cached_content",
    "prediction",
)
_TEXT_FIELD_SET = frozenset(_TEXT_FIELDS)
_MEDIA_TOKENS = 2000

#: Gemini thinking is billed as output but is not covered by ``maxOutputTokens``.
#: When a thinking-capable model does not send a budget we still have to reserve
#: a plausible worst case; this matches Gemini's documented default reasoning
#: window for the models in the bundled price list.
DEFAULT_GEMINI_THINKING_BUDGET = 8192

#: The lowest thinking budget that is accepted by every thinking-capable Gemini
#: model (2.5 Pro cannot disable thinking completely).
MIN_GEMINI_THINKING_BUDGET = 128

_GEMINI_VERSION_RE = re.compile(r"gemini[-_/]?(\d+)(?:[.\-](\d+))?", re.IGNORECASE)

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
    """Serialized characters of every prompt-bearing field in the body.

    Values are counted as whole JSON documents (``sort_keys`` keeps the result
    stable) so that tool/function schemas, their descriptions and their
    parameter objects are measured, not just bare string leaves.  A field found
    at any depth is counted once: nested prompt fields are skipped when their
    parent field was already counted, and an object shared between two fields
    is only measured the first time, so nothing is double counted.
    """

    counted: set[int] = set()
    chars = 0

    def count(value: Any) -> None:
        nonlocal chars
        marker = id(value)
        if marker in counted:
            return
        counted.add(marker)
        chars += len(
            json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        )

    def walk(value: Any) -> None:
        if isinstance(value, Mapping):
            for key, child in value.items():
                if not isinstance(key, str) or child is None:
                    continue
                if key in _TEXT_FIELD_SET:
                    count(child)
                else:
                    walk(child)
        elif isinstance(value, (list, tuple)):
            for child in value:
                walk(child)

    walk(body)
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


def _gemini_generation_config(body: Mapping[str, Any]) -> Mapping[str, Any]:
    for config_key in ("generationConfig", "generation_config"):
        value = body.get(config_key)
        if isinstance(value, Mapping):
            return value
    return {}


def _gemini_max_tokens(body: Mapping[str, Any]) -> int | None:
    config = _gemini_generation_config(body)
    for field in ("maxOutputTokens", "max_output_tokens"):
        value = _positive_int(config.get(field))
        if value is not None:
            return value
    return None


def _int_value(value: Any) -> int | None:
    """Parse an integer, preserving zero and negative sentinels."""

    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str):
        stripped = value.strip()
        if re.fullmatch(r"-?\d+", stripped):
            return int(stripped)
    return None


def _gemini_thinking_config(body: Mapping[str, Any]) -> Mapping[str, Any] | None:
    config = _gemini_generation_config(body)
    for key in ("thinkingConfig", "thinking_config"):
        value = config.get(key)
        if isinstance(value, Mapping):
            return value
    return None


def gemini_thinking_capable(
    body: Mapping[str, Any], model_price: ModelPrice | None = None
) -> bool:
    """Whether this Gemini request can produce separately billed thinking tokens.

    An explicit ``thinkingConfig`` is authoritative.  Otherwise the model id is
    used: Gemini 2.5 and newer reason by default (``gemini-2.5-flash-lite`` is
    the one exception, but reserving thinking for it is merely conservative).
    """

    if _gemini_thinking_config(body) is not None:
        return True
    if model_price is None:
        return False
    text = f"{model_price.model} {model_price.key}".lower()
    if "thinking" in text:
        return True
    match = _GEMINI_VERSION_RE.search(text)
    if match is None:
        return False
    major = int(match.group(1))
    minor = int(match.group(2) or 0)
    return major > 2 or (major == 2 and minor >= 5)


def gemini_thinking_budget(
    body: Mapping[str, Any], model_price: ModelPrice | None = None
) -> int:
    """Worst-case thinking tokens for a Gemini request.

    ``thinkingBudget`` can be ``0`` (disabled), ``-1`` (dynamic/unbounded) or a
    positive token count.  A missing budget on a thinking-capable model gets a
    conservative default.  Dynamic thinking is reserved at the model's full
    output capacity; clamping may lower both later so the request still fits.
    """

    config = _gemini_thinking_config(body)
    explicit: int | None = None
    if config is not None:
        for field in ("thinkingBudget", "thinking_budget"):
            value = _int_value(config.get(field))
            if value is not None:
                explicit = value
                break
    if explicit is not None:
        if explicit < 0:
            # -1 means "let the model decide"; the worst case is its cap.
            return model_price.max_output_tokens if model_price is not None else 0
        return explicit
    if not gemini_thinking_capable(body, model_price):
        return 0
    default = DEFAULT_GEMINI_THINKING_BUDGET
    if model_price is not None:
        default = min(default, model_price.max_output_tokens)
    return max(default, 0)


def explicit_thinking_budget(body: Mapping[str, Any]) -> int | None:
    """The thinking budget the client itself set, if any (``None`` when absent)."""

    config = _gemini_thinking_config(body)
    if config is None:
        return None
    for field in ("thinkingBudget", "thinking_budget"):
        value = _int_value(config.get(field))
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


def explicit_output_tokens(
    provider: str,
    body: Mapping[str, Any],
    model_price: ModelPrice | None = None,
) -> int | None:
    """The output limit the client itself asked for, if any.

    For Gemini the total is ``maxOutputTokens + thinkingBudget`` because both
    are billed as output.  When ``model_price`` is supplied, a missing thinking
    budget on a thinking-capable model counts as its conservative default; the
    caller is asking "what is the worst case this body permits?".
    """

    body = _as_mapping(body)
    if _normalise_provider(provider) == "openai":
        return _openai_max_tokens(body)
    requested = _gemini_max_tokens(body)
    if model_price is not None:
        thinking = gemini_thinking_budget(body, model_price)
    else:
        thinking = explicit_thinking_budget(body) or 0
    if requested is None and thinking == 0:
        return None
    return (requested or 0) + max(thinking, 0)


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
        per_candidate = (
            requested if requested is not None else model_price.max_output_tokens
        )
    else:
        requested = _gemini_max_tokens(body)
        candidates = _gemini_candidates(body)
        # Thinking tokens are billed as output but are not covered by
        # ``maxOutputTokens``.  Reserve both, even when the client omitted a
        # thinking budget (dynamic thinking is on by default for 2.5+ models).
        per_candidate = (
            requested if requested is not None else model_price.max_output_tokens
        ) + gemini_thinking_budget(body, model_price)
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
