"""A tiny local fake upstream for offline AgentCap experiments.

Run it with::

    python -m agentcap.devtools.fake_upstream --port 9000

and point AgentCap at it::

    AGENTCAP_GEMINI_UPSTREAM=http://127.0.0.1:9000 \
        agentcap run --cap 0.05 -- python my_agent.py

It imitates just enough of the OpenAI and Gemini wire formats to exercise
metering, streaming and output clamping:

* ``POST /v1/chat/completions`` (JSON, or SSE with a final usage chunk when
  ``stream_options.include_usage`` is set)
* ``POST /v1/responses`` (JSON with ``usage``)
* ``POST /v1beta/models/<model>:generateContent`` (JSON with ``usageMetadata``)
* ``POST /v1beta/models/<model>:streamGenerateContent`` (SSE when ``alt=sse``,
  otherwise a JSON array; final ``usageMetadata``)

Prompt usage is realistic rather than fixed: ``max(1, ceil(text characters /
4))`` where the text characters are the concatenated ``content``, ``input``,
``prompt``, ``text``, ``system`` and tool-description strings in the request.
Output usage is ``min(requested output limit, 2000)`` tokens, so a clamped
request visibly produces a different bill.  For Gemini, ``thinkingBudget`` is
honoured too: ``thoughtsTokenCount`` is ``min(thinkingBudget, 2000)`` and is
billed as output, exactly like the real API.  Every response carries
``x-fake-output-limit`` with the limit that was actually received (``none``
when the request had no output limit).

Request bodies and headers are never logged or stored.
"""

from __future__ import annotations

import argparse
import json
import math
from typing import Any, AsyncIterator, Mapping

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

__all__ = ["create_app", "main"]

PROMPT_TOKENS_PER_CHAR = 4
MAX_OUTPUT_TOKENS = 2000
LIMIT_HEADER = "x-fake-output-limit"
SSE_MEDIA_TYPE = "text/event-stream"

# Keys whose string values are prompt text.  Structural fields (``model``,
# ``role``, ``type`` ...) are deliberately excluded: they are not billed as
# prompt text by the real APIs.
_TEXT_KEYS = frozenset(
    {
        "text",
        "content",
        "input",
        "prompt",
        "system",
        "system_instruction",
        "systemInstruction",
        "description",
    }
)


def _text_chars(value: Any, *, is_text: bool = False) -> int:
    """Count characters of prompt text anywhere in a request body."""

    if isinstance(value, str):
        return len(value) if is_text else 0
    if isinstance(value, Mapping):
        total = 0
        for key, child in value.items():
            child_is_text = isinstance(key, str) and key in _TEXT_KEYS
            total += _text_chars(child, is_text=child_is_text)
        return total
    if isinstance(value, (list, tuple)):
        # A list under a text key may be a list of strings (embeddings input)
        # or a list of message/part objects (whose own text keys are counted).
        return sum(
            _text_chars(child, is_text=is_text and isinstance(child, str))
            for child in value
        )
    return 0


def _prompt_tokens(payload: Mapping[str, Any]) -> int:
    return max(1, math.ceil(_text_chars(payload) / PROMPT_TOKENS_PER_CHAR))


def _positive_int(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, float) and value.is_integer() and value > 0:
        return int(value)
    if isinstance(value, str) and value.strip().isdigit():
        number = int(value.strip())
        return number if number > 0 else None
    return None


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _openai_chat_limit(payload: Mapping[str, Any]) -> int | None:
    return _positive_int(
        payload.get("max_completion_tokens", payload.get("max_tokens"))
    )


def _openai_responses_limit(payload: Mapping[str, Any]) -> int | None:
    return _positive_int(payload.get("max_output_tokens"))


def _gemini_limit(payload: Mapping[str, Any]) -> int | None:
    config = _mapping(payload.get("generationConfig"))
    if not config:
        config = _mapping(payload.get("generation_config"))
    return _positive_int(
        config.get("maxOutputTokens", config.get("max_output_tokens"))
    )


def _gemini_thinking_budget(payload: Mapping[str, Any]) -> int | None:
    config = _mapping(payload.get("generationConfig"))
    if not config:
        config = _mapping(payload.get("generation_config"))
    thinking = _mapping(config.get("thinkingConfig"))
    if not thinking:
        thinking = _mapping(config.get("thinking_config"))
    value = thinking.get("thinkingBudget", thinking.get("thinking_budget"))
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value.strip())
    return None


def _completion_tokens(limit: int | None) -> int:
    return min(limit, MAX_OUTPUT_TOKENS) if limit else MAX_OUTPUT_TOKENS


def _limit_header(limit: int | None) -> dict[str, str]:
    return {LIMIT_HEADER: str(limit) if limit is not None else "none"}


def _usage(limit: int | None, prompt_tokens: int) -> dict[str, int]:
    output = _completion_tokens(limit)
    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": output,
        "total_tokens": prompt_tokens + output,
    }


def _gemini_usage(
    limit: int | None, prompt_tokens: int, thinking: int | None = None
) -> dict[str, int]:
    output = _completion_tokens(limit)
    thoughts = (
        min(thinking, MAX_OUTPUT_TOKENS)
        if thinking is not None and thinking > 0
        else 0
    )
    usage = {
        "promptTokenCount": prompt_tokens,
        "candidatesTokenCount": output,
        "totalTokenCount": prompt_tokens + output + thoughts,
    }
    if thoughts:
        # Gemini bills thinking as output; a real response carries both counts.
        usage["thoughtsTokenCount"] = thoughts
    return usage


async def _json_body(request: Request) -> Mapping[str, Any] | None:
    try:
        payload = json.loads(await request.body())
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
        return None
    return payload if isinstance(payload, Mapping) else None


def _invalid_body() -> JSONResponse:
    return JSONResponse(
        {"error": {"message": "fake upstream: body is not a JSON object"}},
        status_code=400,
    )


def _not_found(what: str) -> JSONResponse:
    return JSONResponse(
        {"error": {"message": f"fake upstream: unsupported {what}"}},
        status_code=404,
    )


# ------------------------------------------------------------------- OpenAI
async def openai_chat(request: Request) -> Response:
    payload = await _json_body(request)
    if payload is None:
        return _invalid_body()
    limit = _openai_chat_limit(payload)
    model = payload.get("model") if isinstance(payload.get("model"), str) else "fake-model"
    headers = _limit_header(limit)
    usage = _usage(limit, _prompt_tokens(payload))

    if payload.get("stream") is not True:
        body = {
            "id": "chatcmpl-fake",
            "object": "chat.completion",
            "created": 0,
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "fake response"},
                    "finish_reason": "stop",
                }
            ],
            "usage": usage,
        }
        return JSONResponse(body, headers=headers)

    options = _mapping(payload.get("stream_options"))
    include_usage = options.get("include_usage") is True

    async def events() -> AsyncIterator[bytes]:
        def send(data: Mapping[str, Any]) -> bytes:
            return f"data: {json.dumps(data)}\n\n".encode("utf-8")

        base = {
            "id": "chatcmpl-fake",
            "object": "chat.completion.chunk",
            "created": 0,
            "model": model,
        }
        yield send(
            {
                **base,
                "choices": [
                    {
                        "index": 0,
                        "delta": {"role": "assistant", "content": "fake "},
                        "finish_reason": None,
                    }
                ],
            }
        )
        yield send(
            {
                **base,
                "choices": [
                    {
                        "index": 0,
                        "delta": {"content": "response"},
                        "finish_reason": None,
                    }
                ],
            }
        )
        yield send(
            {
                **base,
                "choices": [
                    {"index": 0, "delta": {}, "finish_reason": "stop"}
                ],
            }
        )
        if include_usage:
            yield send({**base, "choices": [], "usage": usage})
        yield b"data: [DONE]\n\n"

    return StreamingResponse(
        events(),
        media_type=SSE_MEDIA_TYPE,
        headers={**headers, "cache-control": "no-cache"},
    )


async def openai_responses(request: Request) -> Response:
    payload = await _json_body(request)
    if payload is None:
        return _invalid_body()
    limit = _openai_responses_limit(payload)
    model = payload.get("model") if isinstance(payload.get("model"), str) else "fake-model"
    usage = _usage(limit, _prompt_tokens(payload))
    body = {
        "id": "resp_fake",
        "object": "response",
        "created_at": 0,
        "model": model,
        "status": "completed",
        "output": [
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "fake response"}],
            }
        ],
        "usage": {
            "input_tokens": usage["prompt_tokens"],
            "output_tokens": usage["completion_tokens"],
            "total_tokens": usage["total_tokens"],
        },
    }
    return JSONResponse(body, headers=_limit_header(limit))


# ------------------------------------------------------------------- Gemini
def _gemini_chunks(
    limit: int | None, prompt_tokens: int, thinking: int | None = None
) -> list[dict[str, Any]]:
    return [
        {
            "candidates": [
                {
                    "content": {
                        "role": "model",
                        "parts": [{"text": "fake "}],
                    }
                }
            ]
        },
        {
            "candidates": [
                {
                    "content": {
                        "role": "model",
                        "parts": [{"text": "response"}],
                    },
                    "finishReason": "STOP",
                }
            ],
            "usageMetadata": _gemini_usage(limit, prompt_tokens, thinking),
        },
    ]


async def gemini_model(request: Request) -> Response:
    rest = request.path_params.get("rest", "")
    action = rest.rsplit(":", 1)[-1] if ":" in rest else ""
    payload = await _json_body(request)
    if payload is None:
        return _invalid_body()
    limit = _gemini_limit(payload)
    thinking = _gemini_thinking_budget(payload)
    headers = _limit_header(limit)
    prompt_tokens = _prompt_tokens(payload)

    if action == "generateContent":
        chunks = _gemini_chunks(limit, prompt_tokens, thinking)
        final = chunks[-1]
        return JSONResponse(
            {
                "candidates": final["candidates"],
                "usageMetadata": final["usageMetadata"],
            },
            headers=headers,
        )

    if action == "streamGenerateContent":
        chunks = _gemini_chunks(limit, prompt_tokens, thinking)
        if request.query_params.get("alt") == "sse":

            async def events() -> AsyncIterator[bytes]:
                for chunk in chunks:
                    yield f"data: {json.dumps(chunk)}\n\n".encode("utf-8")

            return StreamingResponse(
                events(),
                media_type=SSE_MEDIA_TYPE,
                headers={**headers, "cache-control": "no-cache"},
            )
        return JSONResponse(chunks, headers=headers)

    return _not_found("Gemini action")


# ------------------------------------------------------------------- routes
async def healthz(_request: Request) -> Response:
    return JSONResponse({"status": "ok"})


def create_app() -> Starlette:
    """Build the fake upstream ASGI app (no state, no logging)."""

    return Starlette(
        routes=[
            Route("/healthz", healthz, methods=["GET"]),
            Route(
                "/v1/chat/completions",
                openai_chat,
                methods=["POST"],
            ),
            Route("/v1/responses", openai_responses, methods=["POST"]),
            Route(
                "/v1beta/models/{rest:path}",
                gemini_model,
                methods=["POST"],
            ),
        ]
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m agentcap.devtools.fake_upstream",
        description="Local fake OpenAI/Gemini upstream for AgentCap testing.",
    )
    parser.add_argument(
        "--port", type=int, default=0, help="port to listen on (default: random)"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.port < 0 or args.port > 65535:
        print("fake_upstream: --port must be between 0 and 65535")
        return 2
    # 127.0.0.1 only: never expose a fake upstream to the network.
    uvicorn.run(
        create_app(),
        host="127.0.0.1",
        port=args.port,
        log_level="warning",
        access_log=False,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
