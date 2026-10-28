from __future__ import annotations

import stat
import threading
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

import pytest

from agentcap.ledger import (
    CapExceeded,
    Caps,
    InvalidReservationState,
    Ledger,
    ReservationNotFound,
)

D = Decimal


class FakeClock:
    def __init__(self, now: float = 1_000_000.0) -> None:
        self.now = float(now)

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += float(seconds)


@pytest.fixture()
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture()
def ledger(tmp_path, clock) -> Ledger:
    return Ledger(tmp_path / "ledger.db", clock=clock)


def test_ledger_file_is_private(ledger):
    mode = stat.S_IMODE(ledger.path.stat().st_mode)
    assert mode == 0o600


def test_run_cap_blocks_before_overshoot(ledger):
    caps = Caps(run_usd=D("5"))
    ledger.reserve("run-1", "openai", "gpt-4o", D("2.00"), caps)
    ledger.reserve("run-1", "openai", "gpt-4o", D("2.50"), caps)
    assert ledger.spent("run-1") == D("4.50")

    with pytest.raises(CapExceeded) as excinfo:
        ledger.reserve("run-1", "openai", "gpt-4o", D("0.60"), caps)

    exc = excinfo.value
    assert exc.scope == "run"
    assert exc.cap == D("5")
    assert exc.committed == D("4.50")
    assert exc.requested == D("0.60")
    assert "run cap exceeded" in str(exc)


def test_reservation_equal_to_cap_is_allowed(ledger):
    caps = Caps(run_usd=D("5"))
    ledger.reserve("run-1", "openai", "gpt-4o", D("5"), caps)
    assert ledger.spent("run-1") == D("5")


def test_run_cap_is_scoped_per_run(ledger):
    caps = Caps(run_usd=D("5"))
    ledger.reserve("run-1", "openai", "gpt-4o", D("5"), caps)
    ledger.reserve("run-2", "openai", "gpt-4o", D("5"), caps)
    assert ledger.spent("run-1") == D("5")
    assert ledger.spent("run-2") == D("5")


def test_hour_cap_is_rolling_across_runs(ledger, clock):
    caps = Caps(hour_usd=D("5"))
    ledger.reserve("run-1", "openai", "gpt-4o", D("3"), caps)
    ledger.reserve("run-2", "openai", "gpt-4o", D("2"), caps)

    with pytest.raises(CapExceeded) as excinfo:
        ledger.reserve("run-3", "openai", "gpt-4o", D("0.01"), caps)
    assert excinfo.value.scope == "hour"

    # Exactly one hour later the originals still sit on the boundary.
    clock.advance(3600)
    with pytest.raises(CapExceeded):
        ledger.reserve("run-4", "openai", "gpt-4o", D("0.01"), caps)

    # One second past the window they roll out.
    clock.advance(1)
    ledger.reserve("run-5", "openai", "gpt-4o", D("4"), caps)
    assert ledger.window_spent(3600) == D("4")


def test_day_cap_is_rolling(ledger, clock):
    caps = Caps(day_usd=D("10"))
    ledger.reserve("run-1", "openai", "gpt-4o", D("6"), caps)
    clock.advance(3601)
    ledger.reserve("run-2", "openai", "gpt-4o", D("4"), caps)

    with pytest.raises(CapExceeded) as excinfo:
        ledger.reserve("run-3", "openai", "gpt-4o", D("0.01"), caps)
    assert excinfo.value.scope == "day"

    clock.advance(86401)
    ledger.reserve("run-4", "openai", "gpt-4o", D("10"), caps)


def test_settle_uses_actual_for_committed_spend(ledger):
    caps = Caps(run_usd=D("3"))
    reservation = ledger.reserve("run-1", "openai", "gpt-4o", D("2"), caps)
    assert ledger.settle(reservation, D("1.25")) is False
    assert ledger.spent("run-1") == D("1.25")

    ledger.reserve("run-1", "openai", "gpt-4o", D("1.50"), caps)
    with pytest.raises(CapExceeded):
        ledger.reserve("run-1", "openai", "gpt-4o", D("0.30"), caps)


def test_overshoot_is_recorded_and_flagged(ledger):
    caps = Caps(run_usd=D("10"))
    reservation = ledger.reserve("run-1", "openai", "gpt-4o", D("2"), caps)
    assert ledger.settle(reservation, D("2.50")) is True
    assert ledger.spent("run-1") == D("2.50")

    summary = ledger.summary("run-1")
    assert summary["actual_usd"] == D("2.50")
    assert summary["reserved_usd"] == D("0")
    assert summary["settled"] == 1


def test_release_frees_capacity(ledger):
    caps = Caps(run_usd=D("5"))
    reservation = ledger.reserve("run-1", "openai", "gpt-4o", D("5"), caps)
    ledger.release(reservation)
    assert ledger.spent("run-1") == D("0")
    ledger.reserve("run-1", "openai", "gpt-4o", D("5"), caps)

    summary = ledger.summary("run-1")
    assert summary["released"] == 1
    assert summary["open"] == 1
    assert summary["committed_usd"] == D("5")


def test_settle_and_release_validate_state(ledger):
    with pytest.raises(ReservationNotFound):
        ledger.settle("nope", D("1"))
    with pytest.raises(ReservationNotFound):
        ledger.release("nope")

    reservation = ledger.reserve("run-1", "openai", "gpt-4o", D("1"), Caps())
    ledger.settle(reservation, D("1"))
    with pytest.raises(InvalidReservationState):
        ledger.settle(reservation, D("1"))
    with pytest.raises(InvalidReservationState):
        ledger.release(reservation)


def test_summary_counts_all_states(ledger):
    caps = Caps()
    open_id = ledger.reserve("run-1", "openai", "gpt-4o", D("1"), caps)
    settled_id = ledger.reserve("run-1", "openai", "gpt-4o", D("2"), caps)
    released_id = ledger.reserve("run-1", "openai", "gpt-4o", D("3"), caps)
    ledger.settle(settled_id, D("1.5"))
    ledger.release(released_id)

    summary = ledger.summary("run-1")
    assert summary["reservations"] == 3
    assert summary["open"] == 1
    assert summary["settled"] == 1
    assert summary["released"] == 1
    assert summary["reserved_usd"] == D("1")
    assert summary["actual_usd"] == D("1.5")
    assert summary["committed_usd"] == D("2.5")
    assert open_id


def test_window_spent_excludes_old_settled_actuals(ledger, clock):
    reservation = ledger.reserve("run-1", "openai", "gpt-4o", D("2"), Caps())
    ledger.settle(reservation, D("2"))
    assert ledger.window_spent(3600) == D("2")
    clock.advance(3601)
    assert ledger.window_spent(3600) == D("0")


def test_concurrent_reservations_never_exceed_cap(tmp_path):
    clock = FakeClock()
    ledger = Ledger(tmp_path / "concurrent.db", clock=clock)
    caps = Caps(hour_usd=D("5"))
    barrier = threading.Barrier(20)

    def worker(index: int) -> str:
        barrier.wait(timeout=10)
        try:
            ledger.reserve(f"run-{index}", "openai", "gpt-4o", D("1"), caps)
        except CapExceeded:
            return "blocked"
        return "reserved"

    with ThreadPoolExecutor(max_workers=20) as pool:
        results = list(pool.map(worker, range(20)))

    assert results.count("reserved") == 5
    assert results.count("blocked") == 15
    assert ledger.window_spent(3600) == D("5")
