"""Local HTTP proxy that reserves spend before forwarding to OpenAI / Gemini.

Design rules that this module enforces:

* Money is always :class:`~decimal.Decimal`; every reservation is checked and
  inserted atomically by the ledger before any upstream byte is sent.
* Fail closed: an unknown model, malformed body or unmetered endpoint blocks
  the request (HTTP 403/400) instead of forwarding it unmetered.
* Credentials (``Authorization``, ``x-goog-api-key``, ``?key=``) are forwarded
  untouched but never parsed, logged, stored or emitted in events.
* Failures to meter settle the reservation conservatively: a mid-stream
  disconnect or a missing usage block charges the whole reservation, while a
  disconnect before upstream headers charges only the estimated input (output
  was never delivered).

Endpoint contract::

    POST /openai/v1/chat/completions
    POST /openai/v1/responses
    POST /openai/v1/completions
    POST /openai/v1/embeddings
    POST /gemini/v1beta/models/<model>:generateContent
    POST /gemini/v1beta/models/<model>:streamGenerateContent
    GET  /_agentcap/status
"""

from __future__ import annotations

import contextlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import ROUND_CEILING, Decimal
from typing import Any, AsyncIterator, Callable, Iterable, Mapping

import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from .clamp import MIN_CLAMP_OUTPUT, apply_output_clamp, fit_output
from .estimate import (
    apply_input_safety_factor,
    cache_write_requested,
    estimate_input_tokens,
    max_output_tokens,
    reservation_usd,
)
from .ledger import CapExceeded, Caps, Ledger
from .license import FREE_FEATURES
from .loop import LoopDetector, request_hash
from .pricing import (
    MICRO_USD,
    ModelPrice,
    PricingStore,
    UnknownModelError,
    UnknownServiceTierError,
    cost as pricing_cost,
    load_pricing,
)
from .usage import (
    AnthropicStreamUsage,
    GeminiStreamUsage,
    OpenAIStreamUsage,
    Usage,
    anthropic_usage,
    gemini_usage,
    openai_chat_usage,
    openai_responses_usage,
)

__all__ = ["ProxyConfig", "create_app"]

DEFAULT_OPENAI_UPSTREAM = "https://api.openai.com"
DEFAULT_GEMINI_UPSTREAM = "https://generativelanguage.googleapis.com"
DEFAULT_ANTHROPIC_UPSTREAM = "https://api.anthropic.com"

#: Anthropic Messages API (Claude Code, Anthropic SDKs).
_ANTHROPIC_METERED = {"v1/messages": "anthropic"}
_ANTHROPIC_FREE = frozenset({"v1/messages/count_tokens"})
#: ``inference_geo`` values with a published price multiplier.
_ANTHROPIC_GEO_MULTIPLIERS = {"global": Decimal("1"), "us": Decimal("1.1")}

_OPENAI_METERED = {
    "v1/chat/completions": ("openai_chat", "chat"),
    "v1/responses": ("openai_responses", "responses"),
    "v1/completions": ("openai_legacy_completions", "chat"),
    "v1/embeddings": ("openai_embeddings", "embeddings"),
}

# path looks like v1beta/models/gemini-2.5-flash:generateContent
_GEMINI_METERED = re.compile(
    r"(?:^|/)models/(?P<model>[^/:]+):(?P<action>generateContent|streamGenerateContent)$"
)

_REQUEST_HEADER_DROP = frozenset({"host", "content-length", "accept-encoding"})
_RESPONSE_HEADER_DROP = frozenset(
    {"content-length", "transfer-encoding", "content-encoding"}
)

_UNMETERED_MESSAGE = (
    "agentcap: endpoint not metered; blocked (fail-closed). "
    "Use --allow-unmetered to pass through."
)
_INVALID_BODY_MESSAGE = (
    "agentcap: request body is not valid JSON; blocked (fail-closed)."
)

EventCallback = Callable[[dict[str, Any]], None]
KillCallback = Callable[..., Any]

_SERVICE_TIER_RE = re.compile(rb'"service_tier"\s*:\s*"([^"]+)"')


@dataclass(frozen=True)
class _Settlement:
    """Everything needed to price a request's actual usage."""

    price: ModelPrice
    cache_write: bool = False
    requested_tier: str | None = None
    multiplier: Decimal = Decimal("1")
    now: datetime | None = None
    #: Padded input-token estimate used to reserve this request.  If the client
    #: disconnects before the upstream answers, this is the part the provider
    #: may already have billed.
    input_tokens: int = 0


@dataclass(frozen=True)
class _Budget:
    """The tightest active scope and how much room it still has."""

    remaining: Decimal
    scope: str
    cap: Decimal
    committed: Decimal


@dataclass
class ProxyConfig:
    """Everything the proxy needs to meter one agent run."""

    run_id: str
    caps: Caps = field(default_factory=Caps)
    features: Iterable[str] | None = None
    loop_threshold: int = 5
    loop_window_s: float = 120.0
    openai_upstream: str = DEFAULT_OPENAI_UPSTREAM
    gemini_upstream: str = DEFAULT_GEMINI_UPSTREAM
    anthropic_upstream: str = DEFAULT_ANTHROPIC_UPSTREAM
    inject_openai_stream_usage: bool = True
    clamp_output: bool = True
    min_clamp_output: int = MIN_CLAMP_OUTPUT
    #: Padding applied to estimated input tokens when reserving.  Providers may
    #: bill extra system prompts, tool schemas, images or tokenizer overhead
    #: that the local estimate cannot see; a wider reservation keeps that
    #: estimation error out of the budget.  Settlement still uses real usage.
    input_safety_factor: Decimal = Decimal("1.25")
    allow_unmetered: bool = False
    on_cap_exceeded: KillCallback | None = None
    on_loop_detected: KillCallback | None = None
    pricing: PricingStore | None = None
    request_timeout_s: float = 600.0
    clock: Callable[[], datetime] | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.run_id, str) or not self.run_id:
            raise ValueError("run_id must be a non-empty string")
        if not isinstance(self.caps, Caps):
            raise TypeError("caps must be a Caps instance")
        if not isinstance(self.clamp_output, bool):
            raise TypeError("clamp_output must be a bool")
        if (
            isinstance(self.min_clamp_output, bool)
            or not isinstance(self.min_clamp_output, int)
            or self.min_clamp_output <= 0
        ):
            raise ValueError("min_clamp_output must be a positive integer")
        if isinstance(self.input_safety_factor, bool) or not isinstance(
            self.input_safety_factor, Decimal
        ):
            raise TypeError("input_safety_factor must be a Decimal")
        if (
            not self.input_safety_factor.is_finite()
            or self.input_safety_factor < 1
        ):
            raise ValueError("input_safety_factor must be a finite Decimal >= 1")
        if self.features is None:
            self.features = frozenset(FREE_FEATURES)
        else:
            self.features = frozenset(self.features)
        if self.openai_upstream:
            self.openai_upstream = self.openai_upstream.rstrip("/")
        if self.gemini_upstream:
            self.gemini_upstream = self.gemini_upstream.rstrip("/")
        if self.anthropic_upstream:
            self.anthropic_upstream = self.anthropic_upstream.rstrip("/")

    # Convenience for callers that only care about booleans.
    def has(self, feature: str) -> bool:
        return feature in self.features  # type: ignore[operator]


def _money(value: Decimal) -> str:
    """Format a USD amount the way users read it (``$5.00``)."""

    return f"${value:.2f}"


def _optional_money(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


def _agentcap_error(
    status_code: int,
    message: str,
    error_type: str,
    *,
    code: str | None = None,
    headers: Mapping[str, str] | None = None,
) -> JSONResponse:
    error: dict[str, str] = {"message": message, "type": error_type}
    if code is not None:
        error["code"] = code
    return JSONResponse(
        {"error": error},
        status_code=status_code,
        headers=dict(headers or {}),
    )


def _filter_query(request: Request) -> str:
    query = request.url.query
    return query.decode() if isinstance(query, bytes) else str(query or "")


def _parse_usage(
    kind: str, payload: Any
) -> Usage | None:
    if not isinstance(payload, Mapping):
        return None
    if kind == "openai_responses":
        return openai_responses_usage(payload)
    if kind == "gemini":
        return gemini_usage(payload)
    if kind == "anthropic":
        return anthropic_usage(payload)
    # chat, legacy completions and embeddings all carry prompt/completion keys.
    return openai_chat_usage(payload)


class _Proxy:
    def __init__(
        self,
        config: ProxyConfig,
        ledger: Ledger,
        upstream_transport: httpx.AsyncBaseTransport | None = None,
        events: EventCallback | None = None,
    ) -> None:
        if not isinstance(config, ProxyConfig):
            raise TypeError("config must be a ProxyConfig")
        if not isinstance(ledger, Ledger):
            raise TypeError("ledger must be a Ledger")
        self.config = config
        self.ledger = ledger
        self.events = events
        self.pricing = config.pricing if config.pricing is not None else load_pricing()
        self.loop: LoopDetector | None = None
        #: Free tier: a detector that only *observes*.  It never blocks; it
        #: lets the run say what Pro's loop breaker would have saved.
        self.shadow_loop: LoopDetector | None = None
        self.loop_nudge: dict[str, Any] | None = None
        if config.has("loop_detection"):
            self.loop = LoopDetector(
                threshold=config.loop_threshold, window_s=config.loop_window_s
            )
        else:
            self.shadow_loop = LoopDetector(
                threshold=config.loop_threshold, window_s=config.loop_window_s
            )
        self.blocked_count = 0
        #: Model requests the proxy actually saw (forwarded or refused).
        self.observed_requests = 0
        #: Model requests refused before they reached the upstream.
        self.refused_requests = 0
        #: Worst-case cost of the most recently refused request, so the runner
        #: can suggest a cap when the very first request did not fit.
        self.last_refused_reserved_usd: Decimal | None = None
        self.clamped_requests = 0
        self.killed = False
        self._thresholds_emitted: set[int] = set()
        self._client = httpx.AsyncClient(
            transport=upstream_transport,
            timeout=httpx.Timeout(config.request_timeout_s, connect=10.0),
        )

    # ------------------------------------------------------------- app wiring
    def build(self) -> Starlette:
        @contextlib.asynccontextmanager
        async def lifespan(_app: Starlette):
            try:
                yield
            finally:
                with contextlib.suppress(Exception):
                    await self._client.aclose()

        methods = ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]
        app = Starlette(
            routes=[
                Route(
                    "/openai/{path:path}", self._handle_openai, methods=methods
                ),
                Route(
                    "/gemini/{path:path}", self._handle_gemini, methods=methods
                ),
                Route(
                    "/anthropic/{path:path}",
                    self._handle_anthropic,
                    methods=methods,
                ),
                Route("/_agentcap/status", self._status, methods=["GET"]),
            ],
            lifespan=lifespan,
        )
        app.state.config = self.config
        app.state.ledger = self.ledger
        app.state.proxy = self
        app.state.http_client = self._client
        return app

    # ---------------------------------------------------------------- events
    def _emit(self, event: dict[str, Any]) -> None:
        if self.events is None:
            return
        try:
            self.events(event)
        except Exception:
            # Alerting must never take the proxy down.
            pass

    def _invoke(self, callback: KillCallback | None, payload: dict[str, Any]) -> None:
        if callback is None:
            return
        try:
            callback(payload)
        except TypeError:
            try:
                callback()
            except Exception:
                pass
        except Exception:
            pass

    def _trip_kill(self, callback: KillCallback | None, payload: dict[str, Any]) -> None:
        self.killed = True
        self._invoke(callback, payload)

    def _kill_once(
        self, callback: KillCallback | None, payload: dict[str, Any]
    ) -> None:
        """Fire a kill callback at most once per run (dedupe repeated rejects)."""

        if self.killed:
            return
        self._trip_kill(callback, payload)

    def _observe(self) -> None:
        """Record one model request that reached the proxy."""

        self.observed_requests += 1

    def _count_blocked(self) -> None:
        """Count a request that is refused before it reaches the upstream."""

        self.blocked_count += 1
        self.refused_requests += 1

    # ---------------------------------------------------------------- status
    async def _status(self, request: Request) -> JSONResponse:
        summary = self.ledger.summary(self.config.run_id)
        payload = {
            "run_id": self.config.run_id,
            "spent_usd": str(summary["actual_usd"]),
            "reserved_usd": str(summary["reserved_usd"]),
            "caps": {
                "run_usd": _optional_money(self.config.caps.run_usd),
                "hour_usd": _optional_money(self.config.caps.hour_usd),
                "day_usd": _optional_money(self.config.caps.day_usd),
            },
            "blocked_count": self.blocked_count,
            "observed_requests": self.observed_requests,
            "refused_requests": self.refused_requests,
            "last_refused_reserved_usd": _optional_money(
                self.last_refused_reserved_usd
            ),
            "clamped_requests": self.clamped_requests,
            "killed": self.killed,
        }
        return JSONResponse(payload)

    # -------------------------------------------------------------- routing
    async def _handle_openai(self, request: Request) -> Response:
        return await self._handle(request, "openai")

    async def _handle_gemini(self, request: Request) -> Response:
        return await self._handle(request, "gemini")

    async def _handle_anthropic(self, request: Request) -> Response:
        return await self._handle(request, "anthropic")

    async def _handle(self, request: Request, provider: str) -> Response:
        path = request.path_params["path"]
        # Codex can fall back to a websocket transport.  The proxy cannot meter
        # a websocket upgrade, so reject it explicitly instead of forwarding it
        # unmetered (POST /v1/responses keeps working over HTTP).
        if (
            provider == "openai"
            and request.method == "GET"
            and path.strip("/") == "v1/responses"
            and "websocket" in request.headers.get("upgrade", "").lower()
        ):
            return Response(
                "agentcap: websocket upgrades are not supported; "
                "configure the Responses API over HTTP.\n",
                status_code=405,
                headers={"Allow": "POST"},
                media_type="text/plain",
            )
        body = await request.body()
        decision = self._classify(provider, path, request.method)
        if decision == "blocked":
            self._observe()
            self._count_blocked()
            return _agentcap_error(403, _UNMETERED_MESSAGE, "agentcap_blocked")
        if decision == "allowed_unmetered":
            self._observe()
            self._emit(
                {
                    "type": "unmetered",
                    "provider": provider,
                    "method": request.method,
                    "path": path,
                }
            )
            return await self._forward_raw(request, provider, path, body)
        if decision is None:
            return await self._forward_raw(request, provider, path, body)

        kind, model, action = decision
        return await self._metered(
            request, provider, path, body, kind=kind, model=model, action=action
        )

    def _classify(
        self, provider: str, path: str, method: str
    ) -> tuple[str, str, str] | str | None:
        """Return ``(kind, model, action)``, a passthrough marker, or None."""

        if provider == "openai":
            if method != "POST":
                return None  # GET /v1/models and friends are free passthroughs
            entry = _OPENAI_METERED.get(path.strip("/"))
            if entry is not None:
                kind, action = entry
                return (kind, "", action)
            return "allowed_unmetered" if self.config.allow_unmetered else "blocked"

        if provider == "anthropic":
            if method != "POST":
                return None  # GET /v1/models is a free passthrough
            normalised = path.strip("/")
            kind = _ANTHROPIC_METERED.get(normalised)
            if kind is not None:
                return (kind, "", "messages")
            if normalised in _ANTHROPIC_FREE:
                return None  # token counting is free
            return "allowed_unmetered" if self.config.allow_unmetered else "blocked"

        if method != "POST":
            return None
        match = _GEMINI_METERED.search(path)
        if match is not None:
            return ("gemini", match.group("model"), match.group("action"))
        if path.rstrip("/").endswith(":countTokens"):
            return None  # token counting is free
        return "allowed_unmetered" if self.config.allow_unmetered else "blocked"

    # -------------------------------------------------------------- metered
    def _effective_caps(self) -> Caps:
        return Caps(
            run_usd=self.config.caps.run_usd,
            hour_usd=self.config.caps.hour_usd
            if self.config.has("hour_cap")
            else None,
            day_usd=self.config.caps.day_usd if self.config.has("day_cap") else None,
        )

    def _budget_states(self, caps: Caps) -> list[tuple[str, Decimal, Decimal]]:
        states: list[tuple[str, Decimal, Decimal]] = []
        if caps.run_usd is not None:
            states.append(
                ("run", caps.run_usd, self.ledger.spent(self.config.run_id))
            )
        if caps.hour_usd is not None:
            states.append(
                ("hour", caps.hour_usd, self.ledger.window_spent(3600.0))
            )
        if caps.day_usd is not None:
            states.append(
                ("day", caps.day_usd, self.ledger.window_spent(86400.0))
            )
        return states

    def _budget_state(self, caps: Caps) -> _Budget | None:
        """Remaining room in the tightest active scope (spent + reserved)."""

        states = self._budget_states(caps)
        if not states:
            return None
        scope, cap, committed = min(states, key=lambda item: item[1] - item[2])
        return _Budget(
            remaining=cap - committed, scope=scope, cap=cap, committed=committed
        )

    def _exceeded_budget(self) -> _Budget | None:
        """The first active scope whose settled spend has reached its cap."""

        states = self._budget_states(self._effective_caps())
        for scope, cap, committed in states:
            # A $0 cap with no spend has not been "reached": a child that never
            # made a request must not be reported as cap_exceeded.
            if committed >= cap and (cap > 0 or committed > 0):
                return _Budget(
                    remaining=cap - committed,
                    scope=scope,
                    cap=cap,
                    committed=committed,
                )
        return None

    def cap_reached(self) -> bool:
        """True when any active cap has been reached (used to finalise a run)."""

        try:
            return self._exceeded_budget() is not None
        except Exception:
            return False

    def _trip_if_cap_reached(self, model: str, provider: str) -> None:
        """Kill immediately when settlement pushes spend to/over an active cap.

        Reserving the worst case is not enough: providers can settle above the
        reservation (thinking tokens, estimation error).  Once real spend
        reaches the cap the agent must stop now, not on its next request.
        """

        try:
            budget = self._exceeded_budget()
        except Exception:
            return
        if budget is None:
            return
        payload = {
            "scope": budget.scope,
            "cap_usd": str(budget.cap),
            "spent_usd": str(budget.committed),
            "model": model,
            "provider": provider,
        }
        if not self.killed:
            self._emit({"type": "cap_exceeded", **payload})
        self._kill_once(
            self.config.on_cap_exceeded,
            {"type": "cap_exceeded", "reason": "cap_exceeded", **payload},
        )

    def _reject_cap_exceeded(
        self,
        budget: _Budget,
        needed: Decimal,
        model: str,
        provider: str,
    ) -> Response:
        """Budget truly exhausted: reject and kill (at most once per run)."""

        self._count_blocked()
        self.last_refused_reserved_usd = needed
        payload = {
            "scope": budget.scope,
            "cap_usd": str(budget.cap),
            "spent_usd": str(budget.committed),
            "needed_usd": str(needed),
            "model": model,
            "provider": provider,
        }
        self._emit({"type": "cap_exceeded", **payload})
        self._kill_once(
            self.config.on_cap_exceeded,
            {"type": "cap_exceeded", "reason": "cap_exceeded", **payload},
        )
        return _agentcap_error(
            429,
            "agentcap: spend cap reached "
            f"(scope={budget.scope}, cap={_money(budget.cap)}, "
            f"spent={_money(budget.committed)}, needed={_money(needed)}). "
            "Request NOT sent.",
            "agentcap_cap_exceeded",
            code="agentcap_cap_exceeded",
            headers={"x-agentcap-blocked": "cap"},
        )

    def _reject_request_too_large(
        self,
        budget: _Budget,
        needed: Decimal,
        model: str,
        provider: str,
    ) -> Response:
        """Clamping is off and one request is bigger than the remaining budget.

        This is not a cap hit: the run may still be able to make cheaper
        requests, so no kill callback fires.
        """

        self._count_blocked()
        self.last_refused_reserved_usd = needed
        payload = {
            "scope": budget.scope,
            "cap_usd": str(budget.cap),
            "spent_usd": str(budget.committed),
            "needed_usd": str(needed),
            "remaining_usd": str(budget.remaining),
            "model": model,
            "provider": provider,
        }
        self._emit({"type": "request_too_large", **payload})
        return _agentcap_error(
            429,
            "agentcap: request too large for the remaining budget "
            f"(scope={budget.scope}, cap={_money(budget.cap)}, "
            f"spent={_money(budget.committed)}, needed={_money(needed)}). "
            "Request NOT sent. Raise the cap, set an output limit, or enable "
            "output clamping.",
            "agentcap_request_too_large",
            code="agentcap_request_too_large",
            headers={"x-agentcap-blocked": "size"},
        )

    def _anthropic_multiplier(
        self, parsed: Mapping[str, Any], price: ModelPrice
    ) -> Decimal | Response:
        """Price multiplier for Anthropic request options (fail closed).

        * ``inference_geo: "us"`` is billed at 1.1x.
        * ``speed: "fast"`` (fast mode) is billed at a premium; it is only
          allowed when the price table lists a ``fast_multiplier``.
        """

        multiplier = Decimal("1")
        geo = parsed.get("inference_geo")
        if geo is not None:
            geo_rate = (
                _ANTHROPIC_GEO_MULTIPLIERS.get(geo.strip().lower())
                if isinstance(geo, str)
                else None
            )
            if geo_rate is None:
                if not self.config.allow_unmetered:
                    return _unknown_service_tier_error(f"inference_geo={geo}")
                geo_rate = Decimal("1")
            multiplier *= geo_rate
        speed = parsed.get("speed")
        if speed is not None and speed != "standard":
            if speed == "fast" and price.fast_multiplier is not None:
                multiplier *= price.fast_multiplier
            elif not self.config.allow_unmetered:
                return _unknown_service_tier_error(f"speed={speed}")
        return multiplier

    def _loop_nudge(self, model: str, provider: str) -> None:
        """Free tier: record (once) what Pro's loop breaker would have done."""

        spent = self._run_spent()
        self.loop_nudge = {
            "type": "loop_nudge",
            "model": model,
            "provider": provider,
            "spent_usd": spent,
            "threshold": self.config.loop_threshold,
            "window_s": self.config.loop_window_s,
        }
        self._emit(dict(self.loop_nudge))

    def _lookup_price(self, provider: str, model: str) -> ModelPrice:
        if provider == "gemini" and not model.startswith("google/"):
            return self.pricing.get(f"google/{model}")
        if provider == "anthropic":
            # Provider-qualified, so a Claude ID can never borrow another
            # provider's price (and vice versa).
            price = self.pricing.get(
                model if model.startswith("anthropic/") else f"anthropic/{model}"
            )
            if price.provider != "anthropic":
                raise UnknownModelError(model)
            return price
        return self.pricing.get(model)

    async def _metered(
        self,
        request: Request,
        provider: str,
        path: str,
        body: bytes,
        *,
        kind: str,
        model: str,
        action: str,
    ) -> Response:
        self._observe()
        try:
            parsed = json.loads(body)
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
            self._count_blocked()
            return _agentcap_error(400, _INVALID_BODY_MESSAGE, "agentcap_invalid_body")
        if not isinstance(parsed, dict):
            self._count_blocked()
            return _agentcap_error(400, _INVALID_BODY_MESSAGE, "agentcap_invalid_body")

        if provider in ("openai", "anthropic"):
            raw_model = parsed.get("model")
            model = raw_model.strip() if isinstance(raw_model, str) else ""
        if not model:
            self._count_blocked()
            return _unknown_model_error("")

        try:
            price = self._lookup_price(provider, model)
        except UnknownModelError:
            self._count_blocked()
            return _unknown_model_error(model)

        # Service tiers (OpenAI only).  An unknown tier is fail-closed unless
        # the operator explicitly opted out with --allow-unmetered.
        requested_tier: str | None = None
        tier_multiplier = Decimal("1")
        if provider == "openai" and "service_tier" in parsed:
            raw_tier = parsed.get("service_tier")
            if not isinstance(raw_tier, str) or not raw_tier.strip():
                if not self.config.allow_unmetered:
                    self._count_blocked()
                    return _unknown_service_tier_error(str(raw_tier))
                raw_tier = None
            requested_tier = raw_tier.strip() if isinstance(raw_tier, str) else None
            if requested_tier is None:
                tier_multiplier = Decimal("1")
            else:
                try:
                    tier_multiplier = self.pricing.service_tier_multiplier(
                        "openai", requested_tier
                    )
                except UnknownServiceTierError:
                    if not self.config.allow_unmetered:
                        self._count_blocked()
                        return _unknown_service_tier_error(requested_tier)
                    tier_multiplier = Decimal("1")

        if provider == "anthropic":
            anthropic_multiplier = self._anthropic_multiplier(parsed, price)
            if isinstance(anthropic_multiplier, Response):
                self._count_blocked()
                return anthropic_multiplier
            tier_multiplier = anthropic_multiplier

        now = self._now()
        cache_write = cache_write_requested(parsed)

        if self.shadow_loop is not None and self.loop_nudge is None:
            if self.shadow_loop.record_request(parsed):
                self._loop_nudge(model, provider)

        # Loop detection happens before money is reserved.
        if self.loop is not None:
            if self.loop.record_request(parsed):
                self._count_blocked()
                # ``on_loop_detected`` is the specific hook; a generic kill
                # callback supplied as ``on_cap_exceeded`` also stops the run.
                kill_callback = (
                    self.config.on_loop_detected or self.config.on_cap_exceeded
                )
                self._kill_once(
                    kill_callback,
                    {
                        "type": "loop_detected",
                        "reason": "loop_detected",
                        "model": model,
                        "threshold": self.config.loop_threshold,
                        "window_s": self.config.loop_window_s,
                        "request_hash": request_hash(parsed),
                    },
                )
                self._emit(
                    {
                        "type": "loop_detected",
                        "model": model,
                        "provider": provider,
                        "threshold": self.config.loop_threshold,
                        "window_s": self.config.loop_window_s,
                        "request_hash": request_hash(parsed),
                    }
                )
                return _agentcap_error(
                    429,
                    "agentcap: identical request repeated "
                    f"{self.config.loop_threshold} times within "
                    f"{self.config.loop_window_s:g}s; blocked to stop a runaway "
                    "loop. Request NOT sent.",
                    "agentcap_loop_detected",
                    headers={"x-agentcap-blocked": "loop"},
                )

        # Worst-case reservation.  Embeddings have no output tokens.  The
        # input estimate is padded by ``input_safety_factor`` because providers
        # may bill system prompts, tool schemas, images or tokenizer overhead
        # that the local estimate cannot see.
        input_tokens = apply_input_safety_factor(
            estimate_input_tokens(parsed), self.config.input_safety_factor
        )
        estimate_provider = (
            provider if provider in ("gemini", "anthropic") else "openai"
        )
        if kind == "openai_embeddings":
            output_tokens = 0
        else:
            output_tokens = max_output_tokens(estimate_provider, parsed, price)
        reserved = reservation_usd(
            price, input_tokens, output_tokens, now=now, cache_write=cache_write
        )
        if tier_multiplier != 1:
            reserved = (reserved * tier_multiplier).quantize(
                MICRO_USD, rounding=ROUND_CEILING
            )
        caps = self._effective_caps()
        budget = self._budget_state(caps)

        clamped_output: int | None = None
        if budget is not None and reserved > budget.remaining:
            # The worst case does not fit.  The run is only out of budget when
            # the tightest scope has nothing left; otherwise this is one
            # oversized request and clamping (or a 429) can handle it.
            if budget.remaining <= 0:
                return self._reject_cap_exceeded(
                    budget, reserved, model, provider
                )
            if output_tokens <= 0:
                # Input-only requests (embeddings) have no output limit to
                # clamp, so this request cannot be made to fit.  The run may
                # still afford smaller requests: do not kill it.
                return self._reject_request_too_large(
                    budget, reserved, model, provider
                )
            fit = fit_output(
                price,
                provider=estimate_provider,
                body=parsed,
                input_tokens=input_tokens,
                remaining=budget.remaining,
                now=now,
                cache_write=cache_write,
                tier_multiplier=tier_multiplier,
                minimum=self.config.min_clamp_output,
            )
            if (
                not self.config.clamp_output
                or fit.allowed_output < self.config.min_clamp_output
            ):
                if not self.config.clamp_output:
                    return self._reject_request_too_large(
                        budget, reserved, model, provider
                    )
                return self._reject_cap_exceeded(
                    budget, max(reserved, fit.reservation), model, provider
                )
            apply_output_clamp(
                provider, path, parsed, fit.allowed_output, model_price=price
            )
            clamped_output = fit.allowed_output
            reserved = fit.reservation

        try:
            reservation_id = self.ledger.reserve(
                self.config.run_id,
                {"gemini": "google", "anthropic": "anthropic"}.get(
                    provider, "openai"
                ),
                model,
                reserved,
                caps,
            )
        except CapExceeded as exc:
            self._count_blocked()
            self._emit(
                {
                    "type": "cap_exceeded",
                    "scope": exc.scope,
                    "cap_usd": str(exc.cap),
                    "spent_usd": str(exc.committed),
                    "needed_usd": str(exc.requested),
                    "model": model,
                    "provider": provider,
                }
            )
            self._kill_once(
                self.config.on_cap_exceeded,
                {
                    "type": "cap_exceeded",
                    "reason": "cap_exceeded",
                    "scope": exc.scope,
                    "cap_usd": str(exc.cap),
                    "spent_usd": str(exc.committed),
                    "needed_usd": str(exc.requested),
                },
            )
            return _agentcap_error(
                429,
                "agentcap: spend cap reached "
                f"(scope={exc.scope}, cap={_money(exc.cap)}, "
                f"spent={_money(exc.committed)}, needed={_money(exc.requested)}). "
                "Request NOT sent.",
                "agentcap_cap_exceeded",
                code="agentcap_cap_exceeded",
                headers={"x-agentcap-blocked": "cap"},
            )

        if clamped_output is not None:
            self.clamped_requests += 1
            self._emit(
                {
                    "type": "output_clamped",
                    "run_id": self.config.run_id,
                    "model": model,
                    "provider": provider,
                    "allowed_output_tokens": clamped_output,
                }
            )

        # The only permitted body mutations: ask OpenAI for a final usage chunk
        # and lower an output limit that does not fit the remaining budget.
        forwarded = body
        streaming = False
        body_changed = clamped_output is not None
        if provider == "openai":
            streaming = parsed.get("stream") is True
            if (
                kind == "openai_chat"
                and streaming
                and self.config.inject_openai_stream_usage
            ):
                options = parsed.get("stream_options")
                options = dict(options) if isinstance(options, Mapping) else {}
                options["include_usage"] = True
                parsed["stream_options"] = options
                body_changed = True
        elif provider == "anthropic":
            streaming = parsed.get("stream") is True
        else:
            streaming = action == "streamGenerateContent"
        if body_changed:
            forwarded = json.dumps(parsed, separators=(",", ":")).encode("utf-8")

        upstream_request = self._build_request(request, provider, path, forwarded)
        settlement = _Settlement(
            price=price,
            cache_write=cache_write,
            requested_tier=requested_tier,
            multiplier=tier_multiplier,
            now=now,
            input_tokens=input_tokens,
        )

        if streaming:
            return await self._stream(
                upstream_request,
                reservation_id=reservation_id,
                reserved=reserved,
                settlement=settlement,
                model=model,
                kind=kind,
                provider=provider,
            )
        return await self._non_stream(
            upstream_request,
            reservation_id=reservation_id,
            reserved=reserved,
            settlement=settlement,
            model=model,
            kind=kind,
            provider=provider,
        )

    # ----------------------------------------------------------- forwarding
    def _upstream_base(self, provider: str) -> str:
        if provider == "openai":
            return self.config.openai_upstream
        if provider == "anthropic":
            return self.config.anthropic_upstream
        return self.config.gemini_upstream

    def _build_request(
        self, request: Request, provider: str, path: str, body: bytes
    ) -> httpx.Request:
        base = self._upstream_base(provider)
        url = f"{base}/{path.lstrip('/')}"
        query = _filter_query(request)
        if query:
            url = f"{url}?{query}"
        headers = [
            (key, value)
            for key, value in request.headers.items()
            if key.lower() not in _REQUEST_HEADER_DROP
        ]
        # Force identity so response bodies can be parsed for usage.
        headers.append(("accept-encoding", "identity"))
        return self._client.build_request(
            request.method, url, content=body, headers=headers
        )

    @staticmethod
    def _response_headers(response: httpx.Response) -> list[tuple[str, str]]:
        return [
            (key, value)
            for key, value in response.headers.multi_items()
            if key.lower() not in _RESPONSE_HEADER_DROP
        ]

    async def _forward_raw(
        self, request: Request, provider: str, path: str, body: bytes
    ) -> Response:
        upstream = self._build_request(request, provider, path, body)
        try:
            response = await self._client.send(upstream)
        except httpx.HTTPError:
            return _agentcap_error(
                502,
                "agentcap: upstream request failed; nothing was metered.",
                "agentcap_upstream_error",
            )
        try:
            content = await response.aread()
        except httpx.HTTPError:
            return _agentcap_error(
                502,
                "agentcap: upstream request failed; nothing was metered.",
                "agentcap_upstream_error",
            )
        finally:
            with contextlib.suppress(Exception):
                await response.aclose()
        return Response(
            content=content,
            status_code=response.status_code,
            headers=dict(self._response_headers(response)),
        )

    async def _non_stream(
        self,
        upstream: httpx.Request,
        *,
        reservation_id: str,
        reserved: Decimal,
        settlement: _Settlement,
        model: str,
        kind: str,
        provider: str,
    ) -> Response:
        response: httpx.Response | None = None
        try:
            response = await self._client.send(upstream)
            content = await response.aread()
        except httpx.HTTPError:
            self._release_quietly(reservation_id)
            self._emit(
                {
                    "type": "upstream_error",
                    "model": model,
                    "provider": provider,
                }
            )
            return _agentcap_error(
                502,
                "agentcap: upstream request failed; reservation released.",
                "agentcap_upstream_error",
            )
        except BaseException:
            # The client went away (or the handler was cancelled) while waiting
            # for the upstream: charge only the input the provider may already
            # have billed, then let the cancellation propagate.
            await self._abort_response(response)
            self._settle_client_disconnect(
                reservation_id, reserved, settlement, model, provider
            )
            raise
        finally:
            if response is not None:
                with contextlib.suppress(BaseException):
                    await response.aclose()

        assert response is not None
        payload = _load_json(content)
        usage = _parse_usage(kind, payload)
        if usage is not None:
            self._settle_actual(
                reservation_id,
                reserved,
                settlement,
                model,
                provider,
                usage,
                response_tier=_service_tier(payload),
            )
        elif response.status_code >= 400:
            self._release_quietly(reservation_id)
        else:
            self._settle_missing(reservation_id, reserved, model, provider)

        return Response(
            content=content,
            status_code=response.status_code,
            headers=dict(self._response_headers(response)),
        )

    async def _stream(
        self,
        upstream: httpx.Request,
        *,
        reservation_id: str,
        reserved: Decimal,
        settlement: _Settlement,
        model: str,
        kind: str,
        provider: str,
    ) -> Response:
        response: httpx.Response | None = None
        try:
            response = await self._client.send(upstream, stream=True)
        except httpx.HTTPError:
            self._release_quietly(reservation_id)
            return _agentcap_error(
                502,
                "agentcap: upstream request failed; reservation released.",
                "agentcap_upstream_error",
            )
        except BaseException:
            # Cancelled (or the client disconnected) before the upstream
            # produced headers.  Abort and charge the estimated input only.
            await self._abort_response(response)
            self._settle_client_disconnect(
                reservation_id, reserved, settlement, model, provider
            )
            raise

        if response.status_code >= 400:
            # Errors are not streams; drain, release, and replay as a response.
            try:
                content = await response.aread()
            except httpx.HTTPError:
                self._release_quietly(reservation_id)
                return _agentcap_error(
                    502,
                    "agentcap: upstream request failed; reservation released.",
                    "agentcap_upstream_error",
                )
            except BaseException:
                await self._abort_response(response)
                self._settle_client_disconnect(
                    reservation_id, reserved, settlement, model, provider
                )
                raise
            finally:
                with contextlib.suppress(BaseException):
                    await response.aclose()
            self._release_quietly(reservation_id)
            return Response(
                content=content,
                status_code=response.status_code,
                headers=dict(self._response_headers(response)),
            )

        if provider == "gemini":
            parser = GeminiStreamUsage()
        elif provider == "anthropic":
            parser = AnthropicStreamUsage()
        else:
            parser = OpenAIStreamUsage()
        finalized = False
        response_tier: str | None = None

        async def body_stream() -> AsyncIterator[bytes]:
            nonlocal finalized, response_tier
            try:
                async for chunk in response.aiter_bytes():
                    if provider == "openai" and response_tier is None:
                        match = _SERVICE_TIER_RE.search(chunk)
                        if match is not None:
                            response_tier = match.group(1).decode(
                                "utf-8", errors="replace"
                            )
                    parser.feed(chunk)
                    yield chunk
            finally:
                if not finalized:
                    finalized = True
                    usage = parser.finish()
                    if usage is not None:
                        self._settle_actual(
                            reservation_id,
                            reserved,
                            settlement,
                            model,
                            provider,
                            usage,
                            response_tier=response_tier,
                        )
                    else:
                        self._settle_missing(
                            reservation_id, reserved, model, provider
                        )
                with contextlib.suppress(BaseException):
                    await response.aclose()

        return StreamingResponse(
            body_stream(),
            status_code=response.status_code,
            headers=dict(self._response_headers(response)),
        )

    # ------------------------------------------------------------ settlement
    def _now(self) -> datetime:
        if self.config.clock is not None:
            value = self.config.clock()
            if isinstance(value, datetime):
                return value
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                return datetime.fromtimestamp(float(value), timezone.utc)
            raise TypeError("ProxyConfig.clock must return a datetime or timestamp")
        return datetime.now(timezone.utc)

    def _run_spent(self) -> str | None:
        try:
            return str(self.ledger.spent(self.config.run_id))
        except Exception:
            return None

    def _release_quietly(self, reservation_id: str) -> None:
        try:
            self.ledger.release(reservation_id)
        except Exception:
            pass

    @staticmethod
    async def _abort_response(response: httpx.Response | None) -> None:
        """Close an in-flight upstream response without hiding cancellation."""

        if response is None:
            return
        with contextlib.suppress(BaseException):
            await response.aclose()

    def _input_only_cost(self, settlement: _Settlement) -> Decimal | None:
        """Cost of the prompt alone: provider may have billed it before close."""

        try:
            charged = pricing_cost(
                settlement.price,
                settlement.input_tokens,
                0,
                0,
                now=settlement.now,
                cache_write=settlement.cache_write,
                # Anthropic: assume the worst case, all input written to the
                # 1-hour cache.
                cache_write_tokens=(
                    settlement.input_tokens if settlement.cache_write else 0
                ),
                cache_write_1h_tokens=(
                    settlement.input_tokens if settlement.cache_write else 0
                ),
            )
            if settlement.multiplier != 1:
                charged = (charged * settlement.multiplier).quantize(
                    MICRO_USD, rounding=ROUND_CEILING
                )
            return charged
        except Exception:
            return None

    def _settle_client_disconnect(
        self,
        reservation_id: str,
        reserved: Decimal,
        settlement: _Settlement,
        model: str,
        provider: str,
    ) -> None:
        """Settle a reservation after a client disconnect / cancellation.

        Output is assumed to be zero because the connection was closed; the
        provider may still have billed the input.  The reservation must never
        be left open, so a fallback charge (the full reservation) is used if
        the input cost cannot be computed, and a failed settle is released.
        """

        charged = self._input_only_cost(settlement)
        if charged is None:
            charged = reserved
        try:
            self.ledger.settle(reservation_id, charged)
        except Exception:
            self._release_quietly(reservation_id)
        self._emit(
            {
                "type": "client_disconnect",
                "reserved_usd": str(reserved),
                "charged_usd": str(charged),
            }
        )
        try:
            self._check_thresholds()
        except Exception:
            pass
        self._trip_if_cap_reached(model, provider)

    def _settle_actual(
        self,
        reservation_id: str,
        reserved: Decimal,
        settlement: _Settlement,
        model: str,
        provider: str,
        usage: Usage,
        *,
        response_tier: str | None = None,
    ) -> None:
        try:
            actual = pricing_cost(
                settlement.price,
                usage.input_tokens,
                usage.cached_input_tokens,
                usage.output_tokens,
                now=settlement.now,
                cache_write=settlement.cache_write,
                cache_write_tokens=usage.cache_write_input_tokens,
                cache_write_1h_tokens=usage.cache_write_1h_input_tokens,
            )
            tier = (
                (response_tier or settlement.requested_tier)
                if provider == "openai"
                else None
            )
            multiplier = settlement.multiplier
            if tier is not None:
                try:
                    multiplier = self.pricing.service_tier_multiplier(
                        "openai", tier
                    )
                except UnknownServiceTierError:
                    # An unexpected tier in the response cannot be blocked any
                    # more; charge with the requested (or default) multiplier.
                    pass
            if True:
                if multiplier != 1:
                    actual = (actual * multiplier).quantize(
                        MICRO_USD, rounding=ROUND_CEILING
                    )
            overshoot = self.ledger.settle(reservation_id, actual)
        except Exception:
            # Leave the reservation in place (conservative) if settlement fails.
            return

        self._emit(
            {
                "type": "request_settled",
                "model": model,
                "provider": provider,
                "cost_usd": str(actual),
                "reserved_usd": str(reserved),
                "run_spent_usd": self._run_spent(),
                "usage": usage.as_dict(),
            }
        )
        if overshoot:
            self._emit(
                {
                    "type": "overshoot",
                    "model": model,
                    "provider": provider,
                    "cost_usd": str(actual),
                    "reserved_usd": str(reserved),
                }
            )
        # The cap check must never be skipped just because an alert hook failed:
        # a reservation that under-estimated the request is exactly the case
        # where the next request would otherwise run past the cap.
        try:
            self._check_thresholds()
        except Exception:
            pass
        self._trip_if_cap_reached(model, provider)

    def _settle_missing(
        self,
        reservation_id: str,
        reserved: Decimal,
        model: str,
        provider: str,
    ) -> None:
        try:
            self.ledger.settle(reservation_id, reserved)
        except Exception:
            return
        self._emit(
            {
                "type": "usage_missing",
                "model": model,
                "provider": provider,
                "charged_usd": str(reserved),
            }
        )
        self._emit(
            {
                "type": "request_settled",
                "model": model,
                "provider": provider,
                "cost_usd": str(reserved),
                "reserved_usd": str(reserved),
                "run_spent_usd": self._run_spent(),
                "usage": None,
            }
        )
        try:
            self._check_thresholds()
        except Exception:
            pass
        self._trip_if_cap_reached(model, provider)

    def _check_thresholds(self) -> None:
        cap = self.config.caps.run_usd
        if cap is None or cap <= 0:
            return
        spent = self.ledger.spent(self.config.run_id)
        for threshold in (50, 80, 100):
            if threshold in self._thresholds_emitted:
                continue
            if spent * 100 >= cap * threshold:
                self._thresholds_emitted.add(threshold)
                self._emit(
                    {
                        "type": "cap_threshold",
                        "threshold": threshold,
                        "spent_usd": str(spent),
                        "cap_usd": str(cap),
                    }
                )


def _load_json(content: bytes) -> Any:
    try:
        return json.loads(content)
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
        return None


def _service_tier(payload: Any) -> str | None:
    if not isinstance(payload, Mapping):
        return None
    value = payload.get("service_tier")
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _unknown_model_error(model: str) -> JSONResponse:
    label = model or "<missing>"
    return _agentcap_error(
        403,
        f"agentcap: unknown model {label!r}; no price is configured, so the "
        "request was blocked (fail-closed). Add an override under "
        '"models" in ~/.config/agentcap/pricing.json (or set '
        "AGENTCAP_PRICING_FILE).",
        "agentcap_unknown_model",
    )


def _unknown_service_tier_error(tier: str) -> JSONResponse:
    label = tier or "<missing>"
    return _agentcap_error(
        403,
        f"agentcap: unknown service_tier {label!r}; no multiplier is "
        "configured, so the request was blocked (fail-closed). Use a known "
        "tier or --allow-unmetered to pass through.",
        "agentcap_unknown_service_tier",
        code="agentcap_unknown_service_tier",
    )


def create_app(
    config: ProxyConfig,
    ledger: Ledger,
    upstream_transport: httpx.AsyncBaseTransport | None = None,
    events: EventCallback | None = None,
) -> Starlette:
    """Build the ASGI application.

    ``upstream_transport`` is injectable so tests can use
    :class:`httpx.MockTransport` and never touch the network.
    """

    return _Proxy(config, ledger, upstream_transport, events).build()
