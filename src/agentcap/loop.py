"""Loop detection and simple spend-velocity guards."""

from __future__ import annotations

import hashlib
import json
import threading
import time
from collections import deque
from decimal import Decimal
from typing import Any, Callable, Mapping

_EXCLUDED_FIELDS = frozenset({"stream", "user", "metadata", "stream_options"})


def canonical_body(body: Mapping[str, Any]) -> str:
    """Canonical JSON for hashing, with volatile request fields removed."""

    if not isinstance(body, Mapping):
        raise TypeError("request body must be a mapping")
    filtered = {key: value for key, value in body.items() if key not in _EXCLUDED_FIELDS}
    return json.dumps(
        filtered, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )


def request_hash(body: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_body(body).encode("utf-8")).hexdigest()


_hash_body = request_hash


class LoopDetector:
    """Detect N identical requests inside a rolling time window."""

    def __init__(
        self,
        threshold: int = 4,
        window_s: float = 120.0,
        *,
        clock: Callable[[], float] | None = None,
    ) -> None:
        if threshold < 1:
            raise ValueError("threshold must be at least 1")
        if window_s <= 0:
            raise ValueError("window_s must be positive")
        self.threshold = int(threshold)
        self.window_s = float(window_s)
        self._clock = clock or time.time
        self._hits: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def record(self, request_hash: str | Mapping[str, Any]) -> bool:
        """Record a hash (or a body); True when the threshold is reached."""

        if isinstance(request_hash, Mapping):
            request_hash = _hash_body(request_hash)
        if not isinstance(request_hash, str) or not request_hash:
            raise ValueError("request_hash must be a non-empty string")
        now = self._clock()
        cutoff = now - self.window_s
        with self._lock:
            hits = self._hits.setdefault(request_hash, deque())
            while hits and hits[0] < cutoff:
                hits.popleft()
            hits.append(now)
            detected = len(hits) >= self.threshold
            if detected:
                # Keep the window intact for reporting, but avoid unbounded
                # growth on a hot hash.
                while len(hits) > self.threshold:
                    hits.popleft()
            return detected

    def record_request(self, body: Mapping[str, Any]) -> bool:
        return self.record(request_hash(body))

    def reset(self, request_hash: str | None = None) -> None:
        with self._lock:
            if request_hash is None:
                self._hits.clear()
            else:
                self._hits.pop(request_hash, None)


class VelocityGuard:
    """Guard against spending more than a budget inside a rolling window."""

    def __init__(
        self,
        max_usd_per_window: Decimal | str | int,
        window_s: float,
        ledger: Any,
    ) -> None:
        if isinstance(max_usd_per_window, float):
            raise TypeError("max_usd_per_window must be Decimal/str/int, not float")
        self.max_usd_per_window = Decimal(str(max_usd_per_window))
        if self.max_usd_per_window < 0:
            raise ValueError("max_usd_per_window must be non-negative")
        if window_s <= 0:
            raise ValueError("window_s must be positive")
        self.window_s = float(window_s)
        self.ledger = ledger

    def current_usd(self) -> Decimal:
        return self.ledger.window_spent(self.window_s)

    def would_exceed(self, additional_usd: Decimal | str | int = Decimal("0")) -> bool:
        if isinstance(additional_usd, float):
            raise TypeError("additional_usd must be Decimal/str/int, not float")
        additional = Decimal(str(additional_usd))
        if additional < 0:
            raise ValueError("additional_usd must be non-negative")
        return self.current_usd() + additional > self.max_usd_per_window

    def exceeded(self) -> bool:
        return self.current_usd() > self.max_usd_per_window


# --------------------------------------------------------------------------
# 0.1.6: runaway detection that survives growing conversation history.
#
# Real agents resend the whole conversation every turn, so two turns of a
# stuck agent are never byte-identical.  ``RunawayDetector`` therefore looks at
# three signals that do not depend on the history prefix:
#
# * ``repeat``      - the *tail* of the request (model + last assistant
#                     tool call(s) + last tool result / user turn) normalised
#                     and hashed; N identical tails inside a window = loop.
# * ``tool_errors`` - N consecutive requests whose latest tool result is an
#                     error (Anthropic ``is_error``, or error-looking text).
# * ``burn``        - committed spend over a rolling window above
#                     ``max_burn_per_min`` USD/minute.
# --------------------------------------------------------------------------

import re

_ERROR_RE = re.compile(
    r"(^|\b)(error|exception|traceback|failed|failure|not found|no such file|"
    r"permission denied|command not found|exit code [1-9]|non-zero exit|"
    r"invalid|cannot|could not)\b",
    re.IGNORECASE,
)
_VOLATILE_RE = re.compile(
    r"(toolu_|call_|fc_|msg_|resp_)[A-Za-z0-9_-]+|"
    r"\b\d{4}-\d{2}-\d{2}[T ][0-9:.]+Z?\b|\b0x[0-9a-f]+\b|\b\d{5,}\b",
    re.IGNORECASE,
)
# Tool *output* also carries counters/timings ("took 1.3s", "attempt 4").
_DIGITS_RE = re.compile(r"\d+(\.\d+)?")


def _text_of(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(_text_of(part) for part in content)
    if isinstance(content, Mapping):
        for key in ("text", "content", "output", "input", "arguments", "result"):
            if key in content:
                value = content[key]
                if not isinstance(value, str):
                    value = json.dumps(value, sort_keys=True, ensure_ascii=False)
                name = content.get("name", "")
                return f"{name}:{value}" if name else value
        return json.dumps(dict(content), sort_keys=True, ensure_ascii=False)
    return str(content)


def _normalise(text: str, output: bool = False) -> str:
    text = _VOLATILE_RE.sub("#", text)
    if output:
        text = _DIGITS_RE.sub("#", text)
    return " ".join(text.split())[:4000]


def _turns(body: Mapping[str, Any]) -> list[Any]:
    for key in ("messages", "input", "contents"):
        value = body.get(key)
        if isinstance(value, list):
            return value
        if isinstance(value, str):
            return [{"role": "user", "content": value}]
    return []


def _role(item: Any) -> str:
    if isinstance(item, Mapping):
        role = item.get("role") or item.get("type") or ""
        return str(role)
    return ""


def tail_hash(body: Mapping[str, Any], depth: int = 2) -> str | None:
    """Hash of model + the last ``depth`` turns, normalised.  None if empty."""

    turns = _turns(body)
    if not turns:
        return None
    tail = turns[-depth:]
    parts = [str(body.get("model", ""))]
    for item in tail:
        role = _role(item)
        parts.append(role)
        output = role in ("tool", "function", "function_call_output") or (
            isinstance(item, Mapping)
            and any(
                isinstance(p, Mapping)
                and (p.get("type") == "tool_result" or "functionResponse" in p)
                for p in (item.get("content") or item.get("parts") or [])
                if isinstance(item.get("content") or item.get("parts"), list)
            )
        )
        if isinstance(item, Mapping):
            parts.append(_normalise(_text_of(item.get("content", item.get("parts", item))), output))
            if item.get("tool_calls"):
                parts.append(_normalise(_text_of(item.get("tool_calls"))))
        else:
            parts.append(_normalise(_text_of(item)))
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()


def last_tool_result_error(body: Mapping[str, Any]) -> bool | None:
    """True/False for the latest turn's tool result; None if it has none."""

    turns = _turns(body)
    if not turns:
        return None
    last = turns[-1]
    if not isinstance(last, Mapping):
        return None
    role = _role(last)
    # OpenAI chat: role=tool.  Responses: type=function_call_output.
    if role in ("tool", "function", "function_call_output"):
        text = _text_of(last.get("content", last.get("output")))
        return bool(_ERROR_RE.search(text[:2000]))
    content = last.get("content")
    if isinstance(content, list):
        results = [
            p for p in content
            if isinstance(p, Mapping) and p.get("type") == "tool_result"
        ]
        if results:
            for part in results:
                if part.get("is_error") is True:
                    return True
            return any(
                _ERROR_RE.search(_text_of(p.get("content"))[:2000]) for p in results
            )
    parts = last.get("parts")  # Gemini functionResponse
    if isinstance(parts, list):
        responses = [p for p in parts if isinstance(p, Mapping) and "functionResponse" in p]
        if responses:
            return any(
                _ERROR_RE.search(_text_of(p["functionResponse"].get("response"))[:2000])
                for p in responses
            )
    return None


class RunawayDetector:
    """Stateful per-run detector.  ``check`` returns a trip dict or None."""

    def __init__(
        self,
        *,
        repeat_threshold: int = 4,
        repeat_window_s: float = 600.0,
        tool_error_threshold: int = 8,
        max_burn_per_min: Decimal | str | int | None = Decimal("2.00"),
        burn_window_s: float = 120.0,
        clock: Callable[[], float] | None = None,
    ) -> None:
        if repeat_threshold < 2 or tool_error_threshold < 2:
            raise ValueError("thresholds must be at least 2")
        if isinstance(max_burn_per_min, float):
            raise TypeError("max_burn_per_min must be Decimal/str/int, not float")
        self.repeat_threshold = int(repeat_threshold)
        self.repeat_window_s = float(repeat_window_s)
        self.tool_error_threshold = int(tool_error_threshold)
        self.max_burn_per_min = (
            None if max_burn_per_min is None else Decimal(str(max_burn_per_min))
        )
        if self.max_burn_per_min is not None and self.max_burn_per_min <= 0:
            raise ValueError("max_burn_per_min must be positive")
        self.burn_window_s = float(burn_window_s)
        self._clock = clock or time.time
        self._last_tail: str | None = None
        self._last_tail_t = 0.0
        self._tail_streak = 0
        self._exact = LoopDetector(threshold=repeat_threshold, window_s=repeat_window_s, clock=self._clock)
        self._error_streak = 0
        self._spend: deque[tuple[float, Decimal]] = deque()
        self._lock = threading.Lock()

    # -- spend ---------------------------------------------------------
    def record_spend(self, usd: Decimal | str | int) -> None:
        amount = Decimal(str(usd))
        if amount <= 0:
            return
        with self._lock:
            self._spend.append((self._clock(), amount))

    def burn_usd(self) -> Decimal:
        cutoff = self._clock() - self.burn_window_s
        with self._lock:
            while self._spend and self._spend[0][0] < cutoff:
                self._spend.popleft()
            return sum((amt for _, amt in self._spend), Decimal("0"))

    def burn_limit_usd(self) -> Decimal | None:
        if self.max_burn_per_min is None:
            return None
        return self.max_burn_per_min * Decimal(str(self.burn_window_s)) / Decimal(60)

    # -- requests ------------------------------------------------------
    def check(self, body: Mapping[str, Any]) -> dict[str, Any] | None:
        now = self._clock()
        if self._exact.record_request(body):
            return {"kind": "repeat", "count": self.repeat_threshold,
                    "window_s": self.repeat_window_s}
        digest = tail_hash(body)
        if digest is not None:
            # Same step *back to back* (normalised, history ignored).  A test
            # command rerun between real edits is not a loop.
            with self._lock:
                if digest == self._last_tail and now - self._last_tail_t <= self.repeat_window_s:
                    self._tail_streak += 1
                else:
                    self._tail_streak = 1
                self._last_tail, self._last_tail_t = digest, now
                streak = self._tail_streak
            if streak >= self.repeat_threshold:
                return {"kind": "repeat", "count": streak,
                        "window_s": self.repeat_window_s}
        err = last_tool_result_error(body)
        with self._lock:
            if err is True:
                self._error_streak += 1
            elif err is False:
                self._error_streak = 0
            streak = self._error_streak
        if streak >= self.tool_error_threshold:
            return {"kind": "tool_errors", "count": streak}
        limit = self.burn_limit_usd()
        if limit is not None:
            burned = self.burn_usd()
            if burned > limit:
                return {"kind": "burn", "burn_usd": str(burned),
                        "window_s": self.burn_window_s,
                        "max_burn_per_min": str(self.max_burn_per_min)}
        return None


def describe_trip(trip: Mapping[str, Any]) -> str:
    kind = trip.get("kind")
    if kind == "tool_errors":
        return f"{trip.get('count')} consecutive failing tool results"
    if kind == "burn":
        return (f"burn rate ${trip.get('burn_usd')} in {float(trip.get('window_s', 0)):g}s "
                f"> ${trip.get('max_burn_per_min')}/min")
    return (f"the same step repeated {trip.get('count')} times in a row")
