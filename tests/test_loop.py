from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

import pytest

from agentcap.ledger import Caps, Ledger
from agentcap.loop import LoopDetector, VelocityGuard, canonical_body, request_hash


class FakeClock:
    def __init__(self, now: float = 500.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def test_excluded_fields_do_not_change_hash():
    base = {"model": "gpt-4o", "messages": [{"role": "user", "content": "hi"}]}
    noisy = {
        **base,
        "stream": True,
        "user": "someone",
        "metadata": {"trace": "abc"},
        "stream_options": {"include_usage": True},
    }
    assert request_hash(base) == request_hash(noisy)
    assert canonical_body(base) == canonical_body(noisy)


def test_key_order_does_not_change_hash():
    left = {"model": "x", "messages": [{"role": "user", "content": "a"}], "temperature": 0}
    right = {"temperature": 0, "messages": [{"role": "user", "content": "a"}], "model": "x"}
    assert request_hash(left) == request_hash(right)


def test_different_content_changes_hash():
    assert request_hash({"messages": [{"content": "a"}]}) != request_hash(
        {"messages": [{"content": "b"}]}
    )


def test_loop_detected_at_threshold():
    clock = FakeClock()
    detector = LoopDetector(threshold=3, window_s=60, clock=clock)
    digest = request_hash({"messages": [{"content": "again"}]})

    assert detector.record(digest) is False
    assert detector.record(digest) is False
    assert detector.record(digest) is True
    assert detector.record(digest) is True


def test_distinct_hashes_do_not_cross_trigger():
    clock = FakeClock()
    detector = LoopDetector(threshold=2, window_s=60, clock=clock)
    assert detector.record("a") is False
    assert detector.record("b") is False
    assert detector.record("a") is True
    assert detector.record("b") is True


def test_old_events_roll_out_of_window():
    clock = FakeClock()
    detector = LoopDetector(threshold=3, window_s=10, clock=clock)
    assert detector.record("h") is False
    assert detector.record("h") is False
    clock.now += 11
    assert detector.record("h") is False
    assert detector.record("h") is False
    assert detector.record("h") is True


def test_record_request_convenience_and_reset():
    clock = FakeClock()
    detector = LoopDetector(threshold=2, window_s=60, clock=clock)
    body = {"messages": [{"content": "x"}]}
    assert detector.record_request(body) is False
    assert detector.record_request({**body, "stream": True}) is True
    detector.reset()
    assert detector.record_request(body) is False


def test_invalid_configuration():
    with pytest.raises(ValueError):
        LoopDetector(threshold=0)
    with pytest.raises(ValueError):
        LoopDetector(window_s=0)


def test_thread_safe_recording():
    clock = FakeClock()
    detector = LoopDetector(threshold=10, window_s=60, clock=clock)
    barrier = threading.Barrier(10)

    def worker() -> bool:
        barrier.wait(timeout=10)
        return detector.record("same")

    with ThreadPoolExecutor(max_workers=10) as pool:
        results = list(pool.map(lambda _: worker(), range(10)))

    assert results.count(True) == 1


def test_velocity_guard_uses_window_spend(tmp_path):
    clock = FakeClock()
    ledger = Ledger(tmp_path / "ledger.db", clock=clock)
    ledger.reserve("run-1", "openai", "gpt-4o", Decimal("2"), Caps())

    guard = VelocityGuard("5", 3600, ledger)
    assert guard.current_usd() == Decimal("2")
    assert guard.would_exceed(Decimal("3")) is False
    assert guard.would_exceed(Decimal("3.01")) is True
    assert guard.exceeded() is False

    ledger.reserve("run-1", "openai", "gpt-4o", Decimal("3.5"), Caps())
    assert guard.exceeded() is True


def test_velocity_guard_rejects_float_money():
    with pytest.raises(TypeError):
        VelocityGuard(5.0, 3600, ledger=None)
