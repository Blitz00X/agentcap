"""Client disconnects must never leave a reservation open."""

from __future__ import annotations

import asyncio
from decimal import Decimal

import httpx
import pytest

from agentcap.estimate import (
    apply_input_safety_factor,
    estimate_input_tokens,
)
from agentcap.ledger import Caps, Ledger
from agentcap.pricing import cost as pricing_cost
from agentcap.pricing import load_pricing
from agentcap.proxy import ProxyConfig, create_app

from proxy_support import D, costs, openai_chat_body

STORE = load_pricing()
CHAT_URL = "/openai/v1/chat/completions"


class SlowTransport(httpx.AsyncBaseTransport):
    """Blocks after the reservation is made, before upstream headers arrive."""

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.started.set()
        await self.release.wait()
        return httpx.Response(
            200,
            json={
                "choices": [],
                "usage": {"prompt_tokens": 1000, "completion_tokens": 500},
            },
        )


def _expected_input_only(body: dict) -> Decimal:
    price = STORE.get("openai/gpt-4o-mini")
    input_tokens = apply_input_safety_factor(
        estimate_input_tokens(body), Decimal("1.25")
    )
    return pricing_cost(price, input_tokens, 0, 0)


@pytest.mark.parametrize("stream", [False, True])
def test_cancelled_request_settles_input_only_and_leaves_no_reservation(
    tmp_path, stream
):
    body = openai_chat_body(max_completion_tokens=200, stream=stream)
    expected = _expected_input_only(body)

    async def scenario():
        transport = SlowTransport()
        ledger = Ledger(tmp_path / f"cancel-{stream}.db")
        events: list[dict] = []
        config = ProxyConfig(run_id="run-1", caps=Caps(run_usd=D("1")))
        app = create_app(
            config,
            ledger,
            upstream_transport=transport,
            events=events.append,
        )
        proxy = app.state.proxy
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://proxy"
        ) as client:
            task = asyncio.create_task(client.post(CHAT_URL, json=body))
            await asyncio.wait_for(transport.started.wait(), timeout=5)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        await app.state.http_client.aclose()
        return ledger, events, proxy

    ledger, events, proxy = asyncio.run(scenario())

    summary = ledger.summary("run-1")
    assert summary["open"] == 0
    assert summary["settled"] == 1
    assert summary["actual_usd"] == expected
    assert ledger.spent("run-1") == expected

    disconnect = costs(events, "client_disconnect")
    assert len(disconnect) == 1
    assert Decimal(disconnect[0]["charged_usd"]) == expected
    assert Decimal(disconnect[0]["reserved_usd"]) >= expected

    # The input-only charge is far below the cap: no false kill.
    assert proxy.killed is False
    assert "cap_exceeded" not in [event["type"] for event in events]
