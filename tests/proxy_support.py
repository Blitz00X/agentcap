"""Shared helpers for the proxy tests (mocked upstreams only, never the network)."""

from __future__ import annotations

import contextlib
import json
from decimal import Decimal
from typing import Any, AsyncIterator, Callable, Iterable, Sequence

import httpx

from agentcap.ledger import Caps, Ledger
from agentcap.proxy import ProxyConfig, create_app

D = Decimal


class ChunkStream(httpx.AsyncByteStream):
    """An upstream response body delivered in exactly the given chunks."""

    def __init__(self, chunks: Iterable[bytes]) -> None:
        self.chunks = list(chunks)

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self.chunks:
            yield chunk


class Upstream:
    """Records every forwarded request and returns mocked responses."""

    def __init__(
        self,
        responder: Callable[[httpx.Request], httpx.Response] | httpx.Response,
    ) -> None:
        self.responder = responder
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if callable(self.responder):
            return self.responder(request)
        return self.responder

    @property
    def calls(self) -> int:
        return len(self.requests)

    def bodies(self) -> list[bytes]:
        return [request.content for request in self.requests]


def build(
    tmp_path,
    upstream: Upstream,
    *,
    run_id: str = "run-1",
    caps: Caps | None = None,
    features: Iterable[str] | None = None,
    name: str = "ledger.db",
    events: list[dict[str, Any]] | None = None,
    pricing=None,
    ledger: Ledger | None = None,
    **config_kwargs: Any,
):
    """Create (app, ledger, events) wired to a MockTransport upstream."""

    ledger = ledger or Ledger(tmp_path / name)
    if events is None:
        collected: list[dict[str, Any]] = []
        sink = collected.append
    elif callable(events):
        collected = events  # type: ignore[assignment]
        sink = events
    else:
        collected = events
        sink = collected.append
    config = ProxyConfig(
        run_id=run_id,
        caps=caps or Caps(),
        features=features,
        pricing=pricing,
        **config_kwargs,
    )
    app = create_app(
        config,
        ledger,
        upstream_transport=httpx.MockTransport(upstream),
        events=sink,
    )
    return app, ledger, collected


@contextlib.asynccontextmanager
async def proxy_client(app):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://proxy"
    ) as client:
        try:
            yield client
        finally:
            await app.state.http_client.aclose()


def sse(payload: Any) -> bytes:
    return f"data: {json.dumps(payload)}\n\n".encode()


def openai_chat_body(model: str = "gpt-4o-mini", **extra: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "model": model,
        "messages": [{"role": "user", "content": "hello"}],
    }
    body.update(extra)
    return body


def openai_chat_response(
    *,
    prompt_tokens: int = 1000,
    completion_tokens: int = 500,
    cached_tokens: int = 0,
) -> httpx.Response:
    usage: dict[str, Any] = {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": prompt_tokens + completion_tokens,
    }
    if cached_tokens:
        usage["prompt_tokens_details"] = {"cached_tokens": cached_tokens}
    return httpx.Response(
        200,
        json={
            "id": "chatcmpl-mock",
            "object": "chat.completion",
            "choices": [
                {"index": 0, "message": {"role": "assistant", "content": "hi"}}
            ],
            "usage": usage,
        },
    )


def gemini_usage_payload(
    *,
    prompt: int = 1000,
    candidates: int = 200,
    thoughts: int = 0,
    cached: int = 0,
) -> dict[str, Any]:
    metadata = {
        "promptTokenCount": prompt,
        "candidatesTokenCount": candidates,
    }
    if thoughts:
        metadata["thoughtsTokenCount"] = thoughts
    if cached:
        metadata["cachedContentTokenCount"] = cached
    return metadata


def costs(events: Sequence[dict[str, Any]], event_type: str) -> list[dict[str, Any]]:
    return [event for event in events if event["type"] == event_type]
