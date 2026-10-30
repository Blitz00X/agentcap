"""Early warning: "at this burn rate the cap is hit in ~N minutes"."""

from __future__ import annotations

import time
from collections import deque
from decimal import Decimal
from typing import Callable

DEFAULT_HORIZON_MIN = 10.0
DEFAULT_WINDOW_S = 300.0


class EtaWarner:
    """Feed it cumulative spend; it returns an ETA in minutes once, when close.

    The burn rate is measured over a rolling window so a single expensive
    request early in a run does not dominate.  Returns ``None`` until the
    projected time-to-cap drops to ``horizon_min`` or below, then the ETA
    exactly once.  Spend already at or over the cap is left to cap_exceeded.
    """

    def __init__(
        self,
        cap: Decimal | float | int,
        *,
        horizon_min: float = DEFAULT_HORIZON_MIN,
        window_s: float = DEFAULT_WINDOW_S,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.cap = float(cap)
        self.horizon_min = float(horizon_min)
        self.window_s = float(window_s)
        self.clock = clock
        self.samples: deque[tuple[float, float]] = deque()
        self.fired = False

    def rate_per_min(self) -> float:
        if len(self.samples) < 2:
            return 0.0
        (t0, s0), (t1, s1) = self.samples[0], self.samples[-1]
        if t1 <= t0 or s1 <= s0:
            return 0.0
        return (s1 - s0) / ((t1 - t0) / 60.0)

    def eta_minutes(self) -> float | None:
        if not self.samples:
            return None
        spent = self.samples[-1][1]
        rate = self.rate_per_min()
        if rate <= 0 or spent >= self.cap:
            return None
        return (self.cap - spent) / rate

    def observe(self, spent: Decimal | float | int) -> float | None:
        now = self.clock()
        self.samples.append((now, float(spent)))
        while len(self.samples) > 2 and now - self.samples[1][0] >= self.window_s:
            self.samples.popleft()
        if self.fired or self.cap <= 0:
            return None
        eta = self.eta_minutes()
        if eta is None or eta > self.horizon_min:
            return None
        self.fired = True
        return eta
