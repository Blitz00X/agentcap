"""Runaway detection on realistic, growing-history agent transcripts."""

from __future__ import annotations

import random
from decimal import Decimal

from agentcap.loop import RunawayDetector, last_tool_result_error, tail_hash

from conftest import body as anthropic_body


class Clock:
    def __init__(self) -> None:
        self.t = 1_000_000.0

    def __call__(self) -> float:
        return self.t


def claude_turn(history, tool_input, result, is_error, n):
    """Append one Claude Code turn (assistant tool_use + user tool_result)."""
    tid = f"toolu_{n:024d}"
    history.append({"role": "assistant", "content": [
        {"type": "text", "text": "Let me run the tests again."},
        {"type": "tool_use", "id": tid, "name": "Bash", "input": tool_input}]})
    history.append({"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": tid, "content": result, "is_error": is_error}]})
    return {"model": "claude-sonnet-5-5", "max_tokens": 8000,
            "system": "You are Claude Code.", "messages": list(history)}


def openai_turn(history, args, output, n):
    cid = f"call_{n:020d}"
    history.append({"role": "assistant", "content": None, "tool_calls": [
        {"id": cid, "type": "function", "function": {"name": "shell", "arguments": args}}]})
    history.append({"role": "tool", "tool_call_id": cid, "content": output})
    return {"model": "gpt-5", "messages": list(history)}


def run(detector, clock, bodies, step_s=15.0, spend_per_turn=Decimal("0.02")):
    for i, b in enumerate(bodies, start=1):
        clock.t += step_s
        trip = detector.check(b)
        if trip:
            return i, trip
        detector.record_spend(spend_per_turn)
    return None, None


def test_old_exact_hash_never_fires_on_growing_history_but_new_one_does():
    clock = Clock()
    d = RunawayDetector(clock=clock)
    hist = [{"role": "user", "content": "fix the failing test"}]
    bodies = (claude_turn(hist, {"command": "pytest -x"},
                          f"FAILED tests/test_x.py::test_a - AssertionError (run {i}) at 2026-10-30T09:{i:02d}:00Z",
                          True, i) for i in range(40))
    turn, trip = run(d, clock, bodies)
    assert trip is not None and trip["kind"] == "repeat" and turn == 4


def test_claude_varied_failing_commands_trip_tool_error_streak():
    clock = Clock()
    d = RunawayDetector(clock=clock, max_burn_per_min=None)
    hist = [{"role": "user", "content": "deploy it"}]
    bodies = (claude_turn(hist, {"command": f"npm run build -- --attempt {i}"},
                          f"Error: module 'x{i}' not found", True, i) for i in range(40))
    turn, trip = run(d, clock, bodies)
    assert trip["kind"] == "tool_errors" and turn == 8


def test_openai_tool_loop_with_growing_history_trips():
    clock = Clock()
    d = RunawayDetector(clock=clock)
    hist = [{"role": "user", "content": "find the config"}]
    bodies = (openai_turn(hist, '{"cmd":"cat config.yaml"}', "cat: config.yaml: No such file or directory", i)
              for i in range(40))
    turn, trip = run(d, clock, bodies)
    assert trip is not None and turn <= 8


def test_burn_rate_trips_on_expensive_fast_turns():
    clock = Clock()
    d = RunawayDetector(clock=clock, max_burn_per_min=Decimal("2.00"))
    hist = [{"role": "user", "content": "refactor everything"}]
    bodies = (claude_turn(hist, {"command": f"sed -i s/a{i}/b{i}/ f{i}.py"}, f"ok edited f{i}.py", False, i)
              for i in range(100))
    turn, trip = run(d, clock, bodies, step_s=10, spend_per_turn=Decimal("0.40"))
    assert trip["kind"] == "burn" and turn <= 13  # $2.40/min vs $2.00/min limit over 2 min


def test_normal_sessions_do_not_false_positive():
    rng = random.Random(7)
    for seed in range(50):
        clock = Clock()
        d = RunawayDetector(clock=clock)
        hist = [{"role": "user", "content": f"implement feature {seed}"}]
        bodies = []
        for i in range(120):
            # Realistic: mostly distinct reads/edits, occasional failing test
            # runs that get fixed, and repeated `pytest` with *changing* output.
            if i % 6 == 5:
                bodies.append(claude_turn(hist, {"command": "pytest -q"},
                                          f"{rng.randint(1, 3)} failed, {i} passed", True, i))
            elif i % 6 == 0 and i:
                bodies.append(claude_turn(hist, {"command": "pytest -q"}, f"{i + 1} passed", False, i))
            else:
                bodies.append(claude_turn(hist, {"command": f"cat src/mod_{rng.randint(0, 999)}.py"},
                                          f"def f{i}(): return {i}", False, i))
        turn, trip = run(d, clock, bodies, step_s=rng.uniform(6, 25),
                         spend_per_turn=Decimal(str(round(rng.uniform(0.01, 0.15), 3))))
        assert trip is None, (seed, turn, trip)


def test_proxy_style_bodies_and_helpers():
    b = anthropic_body()
    assert tail_hash(b) is not None
    assert last_tool_result_error(b) is None
    assert tail_hash({"model": "x"}) is None


async def test_pro_proxy_blocks_growing_history_loop(harness):
    from agentcap.license import PRO_FEATURES
    h = harness(features=PRO_FEATURES)
    hist = [{"role": "user", "content": "fix it"}]
    codes = []
    for i in range(6):
        b = claude_turn(hist, {"command": "make"}, f"make: *** [all] Error 2 ({i})", True, i)
        b["max_tokens"] = 10
        r = await h.messages(b)
        codes.append(r.status_code)
        if r.status_code == 429:
            assert "runaway loop detected" in r.text
            break
    assert codes[-1] == 429 and codes[:-1] == [200] * (len(codes) - 1)
