"""Model pricing lookup and cost math.

Prices are stored as decimal strings so that no money value ever travels
through ``float``.  Lookup is exact first and then longest-prefix, which makes
dated model ids (``openai/gpt-4o-2024-08-06``) resolve to their family entry
without letting ``gpt-4o`` shadow ``gpt-4o-mini`` or ``gpt-5`` shadow
``gpt-5.4-mini``.

Optional features:

* ``schedule`` — dated price changes (Google announced future increases).
* ``long_context`` — a higher rate above a prompt-size threshold.
* ``cache_write_per_mtok`` — conservative rate for cache-write capable
  requests (``prompt_cache*`` / ``cache_control``).
* ``service_tiers`` — OpenAI service-tier multipliers.
"""

from __future__ import annotations

import copy
import json
import os
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Mapping

ONE_MILLION = Decimal("1000000")
MICRO_USD = Decimal("0.000001")

BUNDLED_PRICING_PATH = Path(__file__).resolve().parent / "data" / "pricing.json"


class PricingError(Exception):
    """Base class for pricing problems."""


class InvalidPricingError(PricingError):
    """A pricing document is malformed or incomplete."""


class UnknownModelError(PricingError):
    """No pricing entry (exact or prefix) exists for a model id."""

    def __init__(self, model: str) -> None:
        self.model = model
        super().__init__(
            f"Unknown model {model!r}: no pricing entry found. "
            "Add one to the pricing override file (AGENTCAP_PRICING_FILE) "
            "before sending requests."
        )


class UnknownServiceTierError(PricingError):
    """A service tier has no configured multiplier (fail closed)."""

    def __init__(self, provider: str, tier: str) -> None:
        self.provider = provider
        self.tier = tier
        super().__init__(
            f"Unknown service tier {tier!r} for provider {provider!r}; "
            "refusing to guess a multiplier."
        )


@dataclass(frozen=True)
class LongContextPrice:
    threshold_tokens: int
    input_per_mtok: Decimal
    output_per_mtok: Decimal
    cached_input_per_mtok: Decimal | None = None


@dataclass(frozen=True)
class ScheduleEntry:
    """A full price triple that applies from ``effective_from`` (UTC date)."""

    effective_from: date
    input_per_mtok: Decimal
    cached_input_per_mtok: Decimal
    output_per_mtok: Decimal


@dataclass(frozen=True)
class ModelPrice:
    key: str
    provider: str
    model: str
    input_per_mtok: Decimal
    cached_input_per_mtok: Decimal
    output_per_mtok: Decimal
    max_output_tokens: int
    source: str = ""
    verified: bool = False
    verified_at: str = ""
    long_context: LongContextPrice | None = None
    schedule: tuple[ScheduleEntry, ...] = ()
    cache_write_per_mtok: Decimal | None = None

    @property
    def is_verified(self) -> bool:
        return bool(self.verified)

    @property
    def verified_date(self) -> str:
        """The verified-on date, or an empty string for legacy bool entries."""

        return self.verified_at

    def rates_at(
        self, now: datetime | date | None = None
    ) -> tuple[Decimal, Decimal, Decimal]:
        """Base (input, cached, output) rates in effect at ``now``."""

        rates = (
            self.input_per_mtok,
            self.cached_input_per_mtok,
            self.output_per_mtok,
        )
        if not self.schedule:
            return rates
        current = _utc_date(now)
        applicable: ScheduleEntry | None = None
        for entry in self.schedule:
            if entry.effective_from <= current:
                applicable = entry
        if applicable is None:
            return rates
        return (
            applicable.input_per_mtok,
            applicable.cached_input_per_mtok,
            applicable.output_per_mtok,
        )


def _utc_date(value: datetime | date | None) -> date:
    if value is None:
        return datetime.now(timezone.utc).date()
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).date()
    if isinstance(value, date):
        return value
    raise TypeError("now must be a datetime, date or None")


def _to_decimal(value: Any, *, field: str, key: str) -> Decimal:
    if isinstance(value, Decimal):
        result = value
    elif isinstance(value, bool):
        raise InvalidPricingError(f"{key}: {field} must be a decimal string, got bool")
    else:
        try:
            result = Decimal(str(value))
        except (InvalidOperation, ValueError, TypeError) as exc:
            raise InvalidPricingError(
                f"{key}: {field} is not a valid decimal: {value!r}"
            ) from exc
    if not result.is_finite() or result < 0:
        raise InvalidPricingError(f"{key}: {field} must be a finite non-negative number")
    return result


def _to_positive_int(value: Any, *, field: str, key: str) -> int:
    if isinstance(value, bool):
        raise InvalidPricingError(f"{key}: {field} must be a positive integer")
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise InvalidPricingError(
            f"{key}: {field} must be a positive integer, got {value!r}"
        ) from exc
    if result <= 0:
        raise InvalidPricingError(f"{key}: {field} must be a positive integer")
    return result


def _parse_verified(raw: Any, *, key: str) -> tuple[bool, str]:
    """Accept a bool legacy flag or a date string (``YYYY-MM-DD``)."""

    if raw is None:
        return False, ""
    if isinstance(raw, bool):
        return raw, ""
    if isinstance(raw, str):
        text = raw.strip()
        if not text or text.lower() == "false":
            return False, ""
        if text.lower() == "true":
            return True, ""
        try:
            date.fromisoformat(text)
        except ValueError as exc:
            raise InvalidPricingError(
                f"{key}: verified must be a bool or a YYYY-MM-DD date, got {raw!r}"
            ) from exc
        return True, text
    raise InvalidPricingError(
        f"{key}: verified must be a bool or a YYYY-MM-DD date, got {type(raw).__name__}"
    )


def _parse_long_context(raw: Any, *, key: str) -> LongContextPrice | None:
    if raw is None:
        return None
    if not isinstance(raw, Mapping):
        raise InvalidPricingError(f"{key}: long_context must be an object")
    cached = raw.get("cached_input_per_mtok")
    return LongContextPrice(
        threshold_tokens=_to_positive_int(
            raw.get("threshold_tokens"), field="long_context.threshold_tokens", key=key
        ),
        input_per_mtok=_to_decimal(
            raw.get("input_per_mtok"), field="long_context.input_per_mtok", key=key
        ),
        output_per_mtok=_to_decimal(
            raw.get("output_per_mtok"), field="long_context.output_per_mtok", key=key
        ),
        cached_input_per_mtok=(
            None
            if cached is None
            else _to_decimal(
                cached, field="long_context.cached_input_per_mtok", key=key
            )
        ),
    )


def _parse_schedule(raw: Any, *, key: str) -> tuple[ScheduleEntry, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise InvalidPricingError(f"{key}: schedule must be a list")
    entries: list[ScheduleEntry] = []
    for index, item in enumerate(raw):
        field = f"schedule[{index}]"
        if not isinstance(item, Mapping):
            raise InvalidPricingError(f"{key}: {field} must be an object")
        raw_date = item.get("effective_from")
        if not isinstance(raw_date, str):
            raise InvalidPricingError(
                f"{key}: {field}.effective_from must be a YYYY-MM-DD string"
            )
        try:
            effective = date.fromisoformat(raw_date)
        except ValueError as exc:
            raise InvalidPricingError(
                f"{key}: {field}.effective_from is not a valid date: {raw_date!r}"
            ) from exc
        entries.append(
            ScheduleEntry(
                effective_from=effective,
                input_per_mtok=_to_decimal(
                    item.get("input_per_mtok"),
                    field=f"{field}.input_per_mtok",
                    key=key,
                ),
                cached_input_per_mtok=_to_decimal(
                    item.get("cached_input_per_mtok"),
                    field=f"{field}.cached_input_per_mtok",
                    key=key,
                ),
                output_per_mtok=_to_decimal(
                    item.get("output_per_mtok"),
                    field=f"{field}.output_per_mtok",
                    key=key,
                ),
            )
        )
    return tuple(sorted(entries, key=lambda entry: entry.effective_from))


def _parse_model_price(key: str, raw: Any) -> ModelPrice:
    if not isinstance(raw, Mapping):
        raise InvalidPricingError(f"{key}: entry must be an object")
    if "/" not in key or not key.split("/", 1)[1]:
        raise InvalidPricingError(
            f"{key!r}: model keys must use the '<provider>/<model>' form"
        )
    required = (
        "input_per_mtok",
        "cached_input_per_mtok",
        "output_per_mtok",
        "max_output_tokens",
    )
    missing = [field for field in required if field not in raw]
    if missing:
        raise InvalidPricingError(
            f"{key}: missing required field(s): {', '.join(missing)}"
        )
    verified, verified_at = _parse_verified(raw.get("verified", False), key=key)
    cache_write = raw.get("cache_write_per_mtok")
    provider, model = key.split("/", 1)
    return ModelPrice(
        key=key,
        provider=provider,
        model=model,
        input_per_mtok=_to_decimal(raw["input_per_mtok"], field="input_per_mtok", key=key),
        cached_input_per_mtok=_to_decimal(
            raw["cached_input_per_mtok"], field="cached_input_per_mtok", key=key
        ),
        output_per_mtok=_to_decimal(
            raw["output_per_mtok"], field="output_per_mtok", key=key
        ),
        max_output_tokens=_to_positive_int(
            raw["max_output_tokens"], field="max_output_tokens", key=key
        ),
        source=str(raw.get("source", "")),
        verified=verified,
        verified_at=verified_at,
        long_context=_parse_long_context(raw.get("long_context"), key=key),
        schedule=_parse_schedule(raw.get("schedule"), key=key),
        cache_write_per_mtok=(
            _to_decimal(cache_write, field="cache_write_per_mtok", key=key)
            if cache_write is not None
            else None
        ),
    )


def _parse_service_tiers(raw: Any) -> dict[str, dict[str, Decimal]]:
    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        raise InvalidPricingError("service_tiers must be an object")
    result: dict[str, dict[str, Decimal]] = {}
    for provider, tiers in raw.items():
        if not isinstance(tiers, Mapping):
            raise InvalidPricingError(
                f"service_tiers.{provider}: must be an object of tier -> multiplier"
            )
        parsed: dict[str, Decimal] = {}
        for tier, multiplier in tiers.items():
            if isinstance(multiplier, bool) or not isinstance(multiplier, (str, int, Decimal)):
                raise InvalidPricingError(
                    f"service_tiers.{provider}.{tier}: multiplier must be a decimal"
                )
            parsed[str(tier)] = _to_decimal(
                multiplier, field=f"service_tiers.{provider}.{tier}", key=provider
            )
        result[str(provider)] = parsed
    return result


def _merge_documents(base: Mapping[str, Any], override: Mapping[str, Any]) -> dict[str, Any]:
    merged: dict[str, Any] = copy.deepcopy(dict(base))
    for field in (
        "version",
        "updated",
        "verified",
        "note",
        "sources",
        "service_tiers",
    ):
        if field in override:
            merged[field] = override[field]

    # Merge service-tier maps provider by provider so an override can add or
    # change one tier without dropping the rest.
    if "service_tiers" in base and "service_tiers" in override:
        merged_tiers: dict[str, Any] = copy.deepcopy(dict(base["service_tiers"]))
        for provider, tiers in (override["service_tiers"] or {}).items():
            if isinstance(merged_tiers.get(provider), Mapping) and isinstance(
                tiers, Mapping
            ):
                merged_tiers[provider] = {**merged_tiers[provider], **tiers}
            else:
                merged_tiers[provider] = copy.deepcopy(tiers)
        merged["service_tiers"] = merged_tiers

    models: dict[str, Any] = dict(merged.get("models") or {})
    for key, raw in (override.get("models") or {}).items():
        if not isinstance(raw, Mapping):
            raise InvalidPricingError(f"{key}: override entry must be an object")
        if key in models and isinstance(models[key], Mapping):
            entry = {**models[key], **raw}
            base_long = models[key].get("long_context")
            if (
                "long_context" in raw
                and isinstance(raw["long_context"], Mapping)
                and isinstance(base_long, Mapping)
            ):
                entry["long_context"] = {**base_long, **raw["long_context"]}
            models[key] = entry
        else:
            models[key] = dict(raw)
    merged["models"] = models
    return merged


def _read_json(path: Path) -> Any:
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError:
        raise
    except json.JSONDecodeError as exc:
        raise InvalidPricingError(f"{path}: invalid JSON: {exc}") from exc
    except OSError as exc:
        raise InvalidPricingError(f"{path}: cannot read pricing file: {exc}") from exc


def _is_prefix_boundary(rest: str) -> bool:
    """True when ``rest`` is a legal suffix after a model family prefix.

    Allowed: nothing at all (exact family), ``@...`` / ``:...`` (provider
    qualifiers) and ``-YYYY...`` date snapshots.  A plain ``-mini`` or a bare
    digit is NOT a boundary, so ``gpt-5`` never swallows ``gpt-5.4-mini`` or
    ``gpt-50``.
    """

    if rest == "":
        return True
    if rest[0] in "@:":
        return True
    return rest[0] == "-" and len(rest) > 1 and rest[1].isdigit()


class PricingStore:
    """Immutable price table with exact + longest-prefix lookup."""

    def __init__(
        self,
        models: Mapping[str, ModelPrice],
        *,
        version: str = "",
        updated: str = "",
        verified: bool = False,
        note: str = "",
        sources: Mapping[str, str] | None = None,
        service_tiers: Mapping[str, Mapping[str, Decimal]] | None = None,
    ) -> None:
        self._models: dict[str, ModelPrice] = dict(models)
        self.version = version
        self.updated = updated
        self.verified = verified
        self.note = note
        self.sources: dict[str, str] = dict(sources or {})
        self.service_tiers: dict[str, dict[str, Decimal]] = {
            provider: dict(tiers)
            for provider, tiers in (service_tiers or {}).items()
        }
        self._prefix_order = sorted(self._models, key=len, reverse=True)

    @classmethod
    def from_document(cls, document: Mapping[str, Any]) -> "PricingStore":
        if not isinstance(document, Mapping):
            raise InvalidPricingError("pricing document must be a JSON object")
        raw_models = document.get("models")
        if not isinstance(raw_models, Mapping) or not raw_models:
            raise InvalidPricingError("pricing document must contain a non-empty 'models' object")
        models = {
            str(key): _parse_model_price(str(key), raw)
            for key, raw in raw_models.items()
        }
        sources = document.get("sources") or {}
        if not isinstance(sources, Mapping):
            raise InvalidPricingError("'sources' must be an object")
        return cls(
            models,
            version=str(document.get("version", "")),
            updated=str(document.get("updated", "")),
            verified=bool(document.get("verified", False)),
            note=str(document.get("note", "")),
            sources={str(key): str(value) for key, value in sources.items()},
            service_tiers=_parse_service_tiers(document.get("service_tiers")),
        )

    def __len__(self) -> int:
        return len(self._models)

    def keys(self) -> list[str]:
        return list(self._models)

    def service_tier_multiplier(self, provider: str, tier: str) -> Decimal:
        tiers = self.service_tiers.get(provider)
        if not tiers or tier not in tiers:
            raise UnknownServiceTierError(provider, tier)
        return tiers[tier]

    def get(self, model: str) -> ModelPrice:
        if not isinstance(model, str) or not model.strip():
            raise UnknownModelError(str(model))
        query = model.strip()
        variants = [query]
        if query.startswith("models/"):
            variants.append(query[len("models/"):])
        if "/models/" in query:
            variants.append(query.replace("/models/", "/", 1))

        for candidate in variants:
            exact = self._models.get(candidate)
            if exact is not None:
                return exact

        # Longest matching prefix wins.  A prefix is only accepted when the
        # remaining suffix is a legal boundary (see ``_is_prefix_boundary``).
        best: ModelPrice | None = None
        best_len = -1
        for key in self._prefix_order:
            price = self._models[key]
            for candidate in variants:
                for prefix in (key, price.model):
                    if not candidate.startswith(prefix):
                        continue
                    if not _is_prefix_boundary(candidate[len(prefix):]):
                        continue
                    if len(prefix) > best_len:
                        best = price
                        best_len = len(prefix)

        if best is not None:
            return best

        raise UnknownModelError(model)

    # Friendly aliases for the lookup operation.
    def lookup(self, model: str) -> ModelPrice:
        return self.get(model)

    def price_for(self, model: str) -> ModelPrice:
        return self.get(model)

    @classmethod
    def load(cls, path: str | os.PathLike[str] | None = None) -> "PricingStore":
        return load_pricing(path)


def default_override_path() -> Path:
    return Path.home() / ".config" / "agentcap" / "pricing.json"


def load_pricing(path: str | os.PathLike[str] | None = None) -> PricingStore:
    """Load bundled prices, merged with an optional user override file.

    Resolution order: explicit ``path`` argument, ``AGENTCAP_PRICING_FILE``,
    then ``~/.config/agentcap/pricing.json``.  A missing override file is not an
    error; a malformed one is.
    """

    base_document = _read_json(BUNDLED_PRICING_PATH)

    if path is not None:
        override_path = Path(path)
    else:
        env_path = os.environ.get("AGENTCAP_PRICING_FILE")
        override_path = Path(env_path) if env_path else default_override_path()

    if override_path.exists():
        override_document = _read_json(override_path)
        if not isinstance(override_document, Mapping):
            raise InvalidPricingError(f"{override_path}: override must be a JSON object")
        base_document = _merge_documents(base_document, override_document)

    return PricingStore.from_document(base_document)


def effective_rates(
    model_price: ModelPrice,
    input_tokens: int,
    now: datetime | date | None = None,
) -> tuple[Decimal, Decimal, Decimal]:
    """Return (input, cached_input, output) rates for the applicable tier."""

    input_rate, cached_rate, output_rate = model_price.rates_at(now)
    long_context = model_price.long_context
    if long_context is not None and input_tokens > long_context.threshold_tokens:
        return (
            long_context.input_per_mtok,
            (
                long_context.cached_input_per_mtok
                if long_context.cached_input_per_mtok is not None
                else cached_rate
            ),
            long_context.output_per_mtok,
        )
    return input_rate, cached_rate, output_rate


def cost(
    model_price: ModelPrice,
    input_tokens: int,
    cached_input_tokens: int = 0,
    output_tokens: int = 0,
    now: datetime | date | None = None,
    cache_write: bool = False,
) -> Decimal:
    """Cost in USD for a completed request.

    ``input_tokens`` includes cached tokens; ``cached_input_tokens`` is billed
    at the discounted cached rate.  When the prompt crosses a long-context
    threshold the whole request is billed at that tier's rates.  When
    ``cache_write`` is true, non-cached input is billed at
    ``max(input, cache_write)``.
    """

    for name, value in (
        ("input_tokens", input_tokens),
        ("cached_input_tokens", cached_input_tokens),
        ("output_tokens", output_tokens),
    ):
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"{name} must be a non-negative integer")

    cached = min(cached_input_tokens, input_tokens)
    uncached = input_tokens - cached
    input_rate, cached_rate, output_rate = effective_rates(
        model_price, input_tokens, now=now
    )
    if cache_write and model_price.cache_write_per_mtok is not None:
        input_rate = max(input_rate, model_price.cache_write_per_mtok)

    total = (
        Decimal(uncached) * input_rate
        + Decimal(cached) * cached_rate
        + Decimal(output_tokens) * output_rate
    ) / ONE_MILLION
    return total
