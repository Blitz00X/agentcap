"""R8: F8 edge cases, driven by recorded upstream responses (no live calls).

1. 401 and 429 alternating: the auth streak must still fill (429/5xx neither
   count nor reset it).
2. A 403 that is not about the key/identity (region block, moderation) is a
   separate reason, ``upstream_refused``, advised "provider refused the request".
3. A child that exits 0 right after a background rejection keeps reason
   ``child_exit``; the rejection is reported as a separate warning.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest

from agentcap.alerts import format_alert
from agentcap.proxy import classify_rejection
from agentcap.runner import EXIT_REASONS

from test_proxy_auth import CHAT, Counting, Proxy, _child, _run

FIXTURES = Path(__file__).parent / "fixtures" / "upstream"
CHAT_PATH = "/openai/v1/chat/completions"


def recorded(name: str) -> tuple[int, dict[str, Any]]:
    data = json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))
    return int(data["status"]), data["body"]


def replay(*names: str) -> Counting:
    """Answer with the recorded responses in order, repeating the last one."""

    answers = [recorded(name) for name in names]

    def responder(_request: httpx.Request) -> httpx.Response:
        index = min(responder.calls, len(answers) - 1)
        responder.calls += 1
        status, body = answers[index]
        return httpx.Response(status, json=body)

    responder.calls = 0
    return Counting(responder)


def _body(status_body: tuple[int, dict[str, Any]]) -> tuple[int, bytes]:
    status, body = status_body
    return status, json.dumps(body).encode()


# --------------------------------------------------------------- classifier
@pytest.mark.parametrize(
    "name,expected",
    [
        ("openrouter_401_user_not_found", "auth"),
        ("anthropic_403_permission_error", "auth"),
        ("openai_403_region_blocked", "refused"),
        ("openrouter_403_moderation", "refused"),
        ("openrouter_429_rate_limited", None),
    ],
)
def test_recorded_rejections_are_classified(name, expected):
    assert classify_rejection(*_body(recorded(name))) == expected


def test_a_403_without_a_body_is_not_blamed_on_the_key():
    assert classify_rejection(403, b"") == "refused"
    assert classify_rejection(403, b"<html>Forbidden</html>") == "refused"


def test_existing_auth_shapes_stay_auth():
    assert classify_rejection(402, b"") == "auth"
    assert classify_rejection(401, b"") == "auth"
    gemini_403 = {"error": {"code": 403, "message": "Permission denied.", "status": "PERMISSION_DENIED"}}
    assert classify_rejection(403, json.dumps(gemini_403).encode()) == "auth"
    credits_403 = {"error": {"message": "Insufficient credits", "code": 403}}
    assert classify_rejection(403, json.dumps(credits_403).encode()) == "auth"


# ------------------------------------------------- 1. 401/429 alternating
async def test_alternating_401_and_429_still_stops_on_the_third_401(tmp_path):
    names = ["openrouter_401_user_not_found", "openrouter_429_rate_limited"] * 3
    upstream = replay(*names)
    proxy = Proxy(tmp_path, httpx.MockTransport(upstream))

    statuses = [(await proxy.post(CHAT_PATH, CHAT)).status_code for _ in range(5)]

    assert statuses == [401, 429, 401, 429, 401]
    assert proxy.loop_kills == []
    assert len(proxy.auth_kills) == 1
    kill = proxy.auth_kills[0]
    assert kill["reason"] == "upstream_auth_failed"
    assert kill["count"] == 3


async def test_429_neither_counts_nor_resets_the_auth_streak(tmp_path):
    upstream = replay(
        "openrouter_401_user_not_found",
        "openrouter_429_rate_limited",
        "openrouter_429_rate_limited",
    )
    proxy = Proxy(tmp_path, httpx.MockTransport(upstream))
    for _ in range(3):
        await proxy.post(CHAT_PATH, CHAT)
    assert proxy.proxy.auth_reject_streak == 1
    assert proxy.auth_kills == []


async def test_a_5xx_does_not_reset_the_auth_streak(tmp_path):
    answers = iter([401, 503, 401, 500, 401])

    def responder(_r):
        status = next(answers)
        return httpx.Response(status, json={"error": {"message": "x", "code": status}})

    proxy = Proxy(tmp_path, httpx.MockTransport(responder))
    for _ in range(5):
        await proxy.post(CHAT_PATH, CHAT)
    assert [k["reason"] for k in proxy.auth_kills] == ["upstream_auth_failed"]


async def test_a_success_still_resets_the_auth_streak(tmp_path):
    ok = {"choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": 1}}
    answers = iter([401, 401, 200, 401, 401])

    def responder(_r):
        status = next(answers)
        if status == 200:
            return httpx.Response(200, json=ok)
        return httpx.Response(401, json=recorded("openrouter_401_user_not_found")[1])

    proxy = Proxy(tmp_path, httpx.MockTransport(responder))
    for i in range(5):
        await proxy.post(CHAT_PATH, {**CHAT, "messages": [{"role": "user", "content": f"t{i}"}]})
    assert proxy.auth_kills == []
    assert proxy.proxy.auth_reject_streak == 2


# ------------------------------------------- 2. region / moderation 403
@pytest.mark.parametrize("name", ["openai_403_region_blocked", "openrouter_403_moderation"])
async def test_non_key_403_stops_as_upstream_refused(tmp_path, name):
    upstream = replay(name)
    proxy = Proxy(tmp_path, httpx.MockTransport(upstream))
    for _ in range(4):
        assert (await proxy.post(CHAT_PATH, CHAT)).status_code == 403

    assert proxy.loop_kills == []
    assert len(proxy.auth_kills) == 1
    kill = proxy.auth_kills[0]
    assert kill["reason"] == "upstream_refused"
    assert kill["type"] == "upstream_refused"
    assert kill["count"] == 3
    # The key was fine: the auth streak never moved.
    assert proxy.proxy.auth_reject_streak == 0

    rejected = [e for e in proxy.events if e["type"] == "upstream_rejected"]
    assert {e["rejection"] for e in rejected} == {"refused"}
    first = rejected[0]
    text = format_alert({**first, "run_id": "r"})
    assert "provider refused the request" in text
    assert "check the API key" not in text
    stop = format_alert({**kill, "run_id": "r"})
    assert "refused" in stop and "Stopping the agent" in stop
    assert "API key" not in stop


async def test_refused_403_between_401s_does_not_count_as_auth(tmp_path):
    upstream = replay(
        "openrouter_401_user_not_found",
        "openai_403_region_blocked",
        "openrouter_401_user_not_found",
        "openai_403_region_blocked",
    )
    proxy = Proxy(tmp_path, httpx.MockTransport(upstream))
    for _ in range(4):
        await proxy.post(CHAT_PATH, CHAT)
    assert proxy.proxy.auth_reject_streak == 2
    assert proxy.auth_kills == []


async def test_key_related_403_keeps_the_api_key_advice(tmp_path):
    upstream = replay("anthropic_403_permission_error")
    proxy = Proxy(tmp_path, httpx.MockTransport(upstream))
    for _ in range(3):
        await proxy.post(CHAT_PATH, CHAT)
    [kill] = proxy.auth_kills
    assert kill["reason"] == "upstream_auth_failed"
    rejected = [e for e in proxy.events if e["type"] == "upstream_rejected"]
    assert {e["rejection"] for e in rejected} == {"auth"}
    first = rejected[0]
    assert "check the API key" in format_alert(first)


def test_upstream_refused_exit_code_is_a_setup_refusal():
    assert EXIT_REASONS["upstream_refused"] == 2


def test_runner_stops_a_region_blocked_run_as_upstream_refused(tmp_path, monkeypatch):
    upstream = replay("openai_403_region_blocked")
    runner, code, output = _run(tmp_path, monkeypatch, _child(), httpx.MockTransport(upstream))
    assert code == 2
    assert runner.summary.reason == "upstream_refused"
    assert "reason: upstream_refused" in output
    assert "provider refused the request" in output
    assert "check the API key" not in output


# ------------------------------- 3. exit 0 right after a background rejection
def test_exit_0_after_a_background_rejection_keeps_child_exit(tmp_path, monkeypatch):
    upstream = replay("openrouter_401_user_not_found")
    runner, code, output = _run(
        tmp_path, monkeypatch, _child(n=1, exit_code=0), httpx.MockTransport(upstream)
    )
    assert code == 0
    assert runner.summary.exit_code == 0
    assert runner.summary.reason == "child_exit"
    assert "reason: child_exit" in output
    assert "reason: upstream_auth_failed" not in output
    assert "agentcap: WARNING: the upstream rejected a request during this run" in output
    assert "HTTP 401" in output
    receipt = runner.receipt_payload(runner.summary)
    assert receipt["reason"] == "child_exit"


def test_nonzero_exit_after_a_rejection_still_names_it(tmp_path, monkeypatch):
    upstream = replay("openrouter_401_user_not_found")
    runner, code, _output = _run(
        tmp_path, monkeypatch, _child(n=1, exit_code=1), httpx.MockTransport(upstream)
    )
    assert code == 1
    assert runner.summary.reason == "upstream_auth_failed"


def test_nonzero_exit_after_a_refusal_is_upstream_refused(tmp_path, monkeypatch):
    upstream = replay("openrouter_403_moderation")
    runner, code, output = _run(
        tmp_path, monkeypatch, _child(n=1, exit_code=1), httpx.MockTransport(upstream)
    )
    assert code == 1
    assert runner.summary.reason == "upstream_refused"
    assert "provider refused the request" in output
