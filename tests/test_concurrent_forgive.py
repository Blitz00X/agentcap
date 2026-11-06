"""R7: a 429'd request forgives only its own loop check, never a parallel one.

Before 0.3.1 ``RunawayDetector.forgive_last()`` kept one snapshot slot: with
two requests in flight (A checked, B checked, A answered 429) it undid B's
check instead of A's.  Mocked upstreams only.
"""

from __future__ import annotations

import asyncio
import json

import httpx

from agentcap.loop import RunawayDetector, request_hash

from test_proxy_auth import Proxy

A = {"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "request A"}]}
B = {
    "model": "gpt-4o-mini",
    "messages": [
        {"role": "user", "content": "run the tests"},
        {"role": "assistant", "content": None,
         "tool_calls": [{"id": "c1", "type": "function",
                         "function": {"name": "sh", "arguments": "{\"cmd\": \"pytest\"}"}}]},
        {"role": "tool", "tool_call_id": "c1", "content": "Error: command not found"},
    ],
}
OK = {"choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": 1}}


def _hits(det: RunawayDetector, body) -> int:
    return len(det._exact._hits.get(request_hash(body), ()))


def _detector(**kw) -> RunawayDetector:
    return RunawayDetector(max_burn_per_min=None, **kw)


# ------------------------------------------------------------- detector level
def test_forgiving_a_does_not_undo_a_parallel_check_of_b():
    det = _detector()
    assert det.check(A, token="a") is None
    assert det.check(B, token="b") is None
    det.forgive("a")  # A got a 429; B is still in flight

    assert det._error_streak == 1, "B's failing tool result was undone"
    assert _hits(det, B) == 1, "B's repeat hit was undone"
    assert _hits(det, A) == 0, "A's own hit was not forgiven"


def test_forgive_right_after_its_own_check_restores_everything():
    det = _detector(tool_error_threshold=2, repeat_threshold=50)
    assert det.check(B, token="b0") is None
    for i in range(10):  # the same failing step retried after 429s
        assert det.check(B, token=f"r{i}") is None
        det.forgive(f"r{i}")
    assert det._error_streak == 1 and det._tail_streak == 1
    assert _hits(det, B) == 1


def test_forgive_is_idempotent_and_ignores_unknown_tokens():
    det = _detector()
    det.check(B, token="b")
    det.forgive("nope")
    det.forgive(None)
    assert det._error_streak == 1 and _hits(det, B) == 1
    det.check(A, token="a")
    det.forgive("a")
    det.forgive("a")
    assert det._error_streak == 1 and _hits(det, B) == 1


def test_parallel_429s_cannot_hide_a_real_loop():
    # B repeats the same step; a parallel request A is rate-limited each
    # time.  B must still trip at its 4th identical step.
    det = _detector(repeat_threshold=4)
    trips = []
    for i in range(4):
        det.check(A, token=f"a{i}")
        trips.append(det.check(B, token=f"b{i}"))
        det.forgive(f"a{i}")
    assert trips[:3] == [None, None, None]
    assert trips[3] is not None and trips[3]["kind"] == "repeat"


def test_parallel_checks_keep_independent_counters_when_b_is_forgiven_first():
    det = _detector()
    det.check(B, token="b")
    det.check(A, token="a")
    det.forgive("b")  # B retried, A normal
    assert _hits(det, A) == 1
    assert _hits(det, B) == 0
    assert det._error_streak == 0


def test_pending_records_are_released_and_bounded():
    from agentcap import loop

    det = _detector(repeat_threshold=10_000)
    det.check(A, token="a")
    det.done("a")
    assert len(det._pending) == 0
    det.forgive("a")  # already released: no effect
    assert _hits(det, A) == 1
    for i in range(loop._MAX_PENDING + 50):
        det.check({**A, "messages": [{"role": "user", "content": f"m{i}"}]}, token=i)
    assert len(det._pending) == loop._MAX_PENDING


async def test_proxy_releases_the_check_record_of_a_normal_answer(tmp_path):
    proxy = Proxy(tmp_path, httpx.MockTransport(lambda _r: httpx.Response(200, json=OK)))
    for i in range(5):
        body = {**A, "messages": [{"role": "user", "content": f"x{i}"}]}
        assert (await proxy.post("/openai/v1/chat/completions", body)).status_code == 200
    assert len(proxy.proxy.loop._pending) == 0


def test_forgive_last_still_works_for_sequential_callers():
    det = _detector(tool_error_threshold=2, repeat_threshold=50)
    det.check(B)
    for _ in range(5):
        det.check(B)
        det.forgive_last()
    assert det._error_streak == 1 and _hits(det, B) == 1


# ---------------------------------------------------------------- proxy level
async def test_two_parallel_requests_one_429_retry_counters_stay_independent(tmp_path):
    a_in_upstream = asyncio.Event()
    b_in_upstream = asyncio.Event()
    a_calls = 0

    async def upstream(request: httpx.Request) -> httpx.Response:
        nonlocal a_calls
        body = json.loads(request.content)
        if body["messages"][-1]["content"] == "request A":
            a_calls += 1
            if a_calls == 1:
                a_in_upstream.set()
                await asyncio.wait_for(b_in_upstream.wait(), 5)  # B is checked and in flight
                return httpx.Response(429, json={"error": {"message": "rate limited"}})
            return httpx.Response(200, json=OK)
        b_in_upstream.set()
        await asyncio.sleep(0.05)  # A's 429 is handled while B is still in flight
        return httpx.Response(200, json=OK)

    proxy = Proxy(tmp_path, httpx.MockTransport(upstream))
    det = proxy.proxy.loop
    assert det is not None

    task_a = asyncio.create_task(proxy.post("/openai/v1/chat/completions", A))
    await asyncio.wait_for(a_in_upstream.wait(), 5)
    task_b = asyncio.create_task(proxy.post("/openai/v1/chat/completions", B))
    first_a, resp_b = await asyncio.gather(task_a, task_b)
    assert (first_a.status_code, resp_b.status_code) == (429, 200)

    # B's check survived A's forgiveness.
    assert det._error_streak == 1
    assert _hits(det, B) == 1
    assert _hits(det, A) == 0

    # A's retry goes through and counts exactly once.
    retry = await proxy.post("/openai/v1/chat/completions", A)
    assert retry.status_code == 200
    assert _hits(det, A) == 1
    assert _hits(det, B) == 1
    assert proxy.loop_kills == []
