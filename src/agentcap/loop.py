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
        threshold: int = 5,
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
