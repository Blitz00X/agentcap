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
from agentcap.proxy import ProxyConfig, _Settlement, create_app

from proxy_support import (
    ChunkStream,
    D,
    Upstream,
    build,
    costs,
    openai_chat_body,
    proxy_client,
    sse,
)

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
def test_cancelled_request_charges_full_reservation_and_leaves_no_open_reservation(
    tmp_path, stream
):
    body = openai_chat_body(max_completion_tokens=200, stream=stream)
    input_only = _expected_input_only(body)

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

    disconnect = costs(events, "client_disconnect")
    assert len(disconnect) == 1
    reserved = Decimal(disconnect[0]["reserved_usd"])
    assert Decimal(disconnect[0]["charged_usd"]) == reserved
    assert summary["actual_usd"] == reserved
    assert ledger.spent("run-1") == reserved
    assert reserved > input_only  # no more "output = 0" assumption

    # The full reservation is still below the cap: no false kill.
    assert proxy.killed is False
    assert "cap_exceeded" not in [event["type"] for event in events]


def test_stream_abort_after_content_charges_full_reservation(tmp_path):
    """M-2: content delivered before an abort must not be free.

    Drive the production ``_stream`` iterator and close it after the content
    chunk, before the final message_delta carries output usage.  The proxy must
    charge the full reservation, not just the prompt.
    """
    chunks = [
        sse({"type": "message_start", "message": {"usage": {
            "input_tokens": 1000, "output_tokens": 1,
            "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}}}),
        sse({"type": "content_block_delta", "index": 0,
             "delta": {"type": "text_delta", "text": "X" * 4000}}),
        sse({"type": "message_delta", "usage": {"output_tokens": 5000}}),
    ]
    upstream = Upstream(httpx.Response(
        200, headers={"content-type": "text/event-stream"},
        stream=ChunkStream(chunks),
    ))
    app, ledger, events = build(tmp_path, upstream, caps=Caps(run_usd=D("1")))
    proxy = app.state.proxy
    price = STORE.get("anthropic/claude-sonnet-5-5")
    reserved = Decimal("0.040960")
    reservation_id = ledger.reserve(
        "run-1", "anthropic", "claude-sonnet-5-5", reserved, Caps(run_usd=D("1"))
    )
    request = httpx.Request(
        "POST", "https://api.anthropic.com/v1/messages",
        content=b'{"model":"claude-sonnet-5-5","max_tokens":1000,"stream":true}',
    )

    async def scenario():
        response = await proxy._stream(
            request,
            reservation_id=reservation_id,
            reserved=reserved,
            settlement=_Settlement(price=price, input_tokens=1000),
            model="claude-sonnet-5-5",
            kind="anthropic",
            provider="anthropic",
        )
        iterator = response.body_iterator
        await iterator.__anext__()   # message_start
        await iterator.__anext__()   # content chunk
        await iterator.aclose()      # client aborts before message_delta
        await proxy._client.aclose()

    asyncio.run(scenario())

    settled = costs(events, "request_settled")
    assert settled, [event["type"] for event in events]
    assert Decimal(settled[-1]["cost_usd"]) == reserved
    assert "usage_missing" in [event["type"] for event in events]
    assert ledger.summary("run-1")["open"] == 0
