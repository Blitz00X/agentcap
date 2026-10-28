"""Pure token-usage parsers for OpenAI and Gemini wire formats.

Nothing in this module performs I/O: every function takes already-received
JSON or bytes and returns a :class:`Usage` value (or ``None`` when the payload
carries no usage block).  That keeps the accounting rules easy to test and
impossible to accidentally couple to HTTP.

Token conventions (matching the providers):

* OpenAI chat: ``prompt_tokens`` includes cached tokens; ``cached_tokens`` is
  billed at the cached rate; ``completion_tokens`` includes reasoning tokens.
* OpenAI responses: ``input_tokens`` / ``output_tokens`` with
  ``input_tokens_details.cached_tokens``.
* Gemini: ``promptTokenCount`` includes ``cachedContentTokenCount``;
  ``thoughtsTokenCount`` is billed as output and added to
  ``candidatesTokenCount``.
"""

from __future__ import annotations

import codecs
import json
from dataclasses import dataclass
from typing import Any, Iterable, Iterator, Mapping

__all__ = [
    "Usage",
    "SSEDecoder",
    "OpenAIStreamUsage",
    "GeminiStreamUsage",
    "openai_chat_usage",
    "openai_responses_usage",
    "gemini_usage",
    "parse_openai_chat_stream",
    "parse_openai_responses_stream",
    "parse_gemini_stream",
    "parse_openai_chat_usage",
    "parse_openai_responses_usage",
    "parse_gemini_usage",
]


def _token(value: Any) -> int:
    """Coerce a token count to a non-negative int; unknown values become 0."""

    if isinstance(value, bool) or value is None:
        return 0
    if isinstance(value, int):
        return value if value > 0 else 0
    if isinstance(value, float) and value.is_integer():
        return int(value) if value > 0 else 0
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.isdigit():
            return int(stripped)
    return 0


def _mapping(value: Any) -> Mapping[str, Any] | None:
    return value if isinstance(value, Mapping) else None


@dataclass(frozen=True)
class Usage:
    """Normalised usage: ``input`` includes cached tokens."""

    input_tokens: int
    cached_input_tokens: int = 0
    output_tokens: int = 0

    def __post_init__(self) -> None:
        for name in ("input_tokens", "cached_input_tokens", "output_tokens"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")

    @property
    def uncached_input_tokens(self) -> int:
        return max(self.input_tokens - self.cached_input_tokens, 0)

    def as_dict(self) -> dict[str, int]:
        return {
            "input_tokens": self.input_tokens,
            "cached_input_tokens": self.cached_input_tokens,
            "output_tokens": self.output_tokens,
        }


# --------------------------------------------------------------------- OpenAI


def openai_chat_usage(payload: Mapping[str, Any] | None) -> Usage | None:
    """Usage from an OpenAI Chat Completions (or legacy Completions) payload."""

    payload = _mapping(payload)
    if payload is None:
        return None
    usage = _mapping(payload.get("usage"))
    if usage is None:
        return None
    if not any(
        key in usage
        for key in ("prompt_tokens", "completion_tokens", "prompt_tokens_details")
    ):
        return None

    cached = 0
    details = _mapping(usage.get("prompt_tokens_details"))
    if details is not None:
        cached = _token(details.get("cached_tokens"))

    return Usage(
        input_tokens=_token(usage.get("prompt_tokens")),
        cached_input_tokens=cached,
        output_tokens=_token(usage.get("completion_tokens")),
    )


def openai_responses_usage(payload: Mapping[str, Any] | None) -> Usage | None:
    """Usage from an OpenAI Responses API payload (or ``response.completed``)."""

    payload = _mapping(payload)
    if payload is None:
        return None
    usage = _mapping(payload.get("usage"))
    if usage is None:
        return None
    if not any(
        key in usage
        for key in ("input_tokens", "output_tokens", "input_tokens_details")
    ):
        return None

    cached = 0
    details = _mapping(usage.get("input_tokens_details"))
    if details is not None:
        cached = _token(details.get("cached_tokens"))

    return Usage(
        input_tokens=_token(usage.get("input_tokens")),
        cached_input_tokens=cached,
        output_tokens=_token(usage.get("output_tokens")),
    )


# --------------------------------------------------------------------- Gemini


def gemini_usage(payload: Mapping[str, Any] | None) -> Usage | None:
    """Usage from Gemini ``usageMetadata`` (stream chunks are cumulative)."""

    payload = _mapping(payload)
    if payload is None:
        return None
    metadata = _mapping(payload.get("usageMetadata"))
    if metadata is None:
        # Accept snake_case for robustness; Google emits camelCase.
        metadata = _mapping(payload.get("usage_metadata"))
    if metadata is None:
        return None
    if not any(
        key in metadata
        for key in (
            "promptTokenCount",
            "candidatesTokenCount",
            "thoughtsTokenCount",
            "cachedContentTokenCount",
            "prompt_token_count",
        )
    ):
        return None

    def pick(*names: str) -> int:
        for name in names:
            if name in metadata:
                return _token(metadata.get(name))
        return 0

    candidates = pick("candidatesTokenCount", "candidates_token_count")
    thoughts = pick("thoughtsTokenCount", "thoughts_token_count")
    return Usage(
        input_tokens=pick("promptTokenCount", "prompt_token_count"),
        cached_input_tokens=pick("cachedContentTokenCount", "cached_content_token_count"),
        output_tokens=candidates + thoughts,
    )


# ------------------------------------------------------------------ SSE helper


def _event_data(raw: str) -> str | None:
    """Extract the ``data:`` payload from one raw SSE event block."""

    lines: list[str] = []
    for line in raw.split("\n"):
        if line.startswith("data:"):
            value = line[len("data:"):]
            if value.startswith(" "):
                value = value[1:]
            lines.append(value)
    if not lines:
        return None
    return "\n".join(lines)


class SSEDecoder:
    """Incremental ``text/event-stream`` decoder safe across chunk splits."""

    def __init__(self) -> None:
        self._decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self._buffer = ""

    def feed(self, chunk: bytes | str) -> list[str]:
        if isinstance(chunk, bytes):
            self._buffer += self._decoder.decode(chunk)
        else:
            self._buffer += chunk
        self._buffer = self._buffer.replace("\r\n", "\n")
        return self._drain(final=False)

    def finish(self) -> list[str]:
        self._buffer += self._decoder.decode(b"", final=True)
        self._buffer = self._buffer.replace("\r\n", "\n")
        return self._drain(final=True)

    def _drain(self, *, final: bool) -> list[str]:
        events: list[str] = []
        while True:
            index = self._buffer.find("\n\n")
            if index < 0:
                break
            raw = self._buffer[:index]
            self._buffer = self._buffer[index + 2:]
            data = _event_data(raw)
            if data is not None:
                events.append(data)
        if final and self._buffer.strip():
            data = _event_data(self._buffer)
            self._buffer = ""
            if data is not None:
                events.append(data)
        return events


def _sse_payloads(chunks: Iterable[bytes | str]) -> Iterator[str]:
    decoder = SSEDecoder()
    for chunk in chunks:
        yield from decoder.feed(chunk)
    yield from decoder.finish()


class OpenAIStreamUsage:
    """Accumulate usage from OpenAI SSE streams (chat and responses)."""

    def __init__(self) -> None:
        self._decoder = SSEDecoder()
        self._usage: Usage | None = None
        self._finished = False

    @property
    def usage(self) -> Usage | None:
        return self._usage

    def feed(self, chunk: bytes | str) -> Usage | None:
        for data in self._decoder.feed(chunk):
            self._consume(data)
        return self._usage

    def finish(self) -> Usage | None:
        if not self._finished:
            self._finished = True
            for data in self._decoder.finish():
                self._consume(data)
        return self._usage

    def _consume(self, data: str) -> None:
        text = data.strip()
        if not text or text == "[DONE]":
            return
        try:
           payload: Any = json.loads(text)
        except (json.JSONDecodeError, ValueError):
            return
        payload = _mapping(payload)
        if payload is None:
            return

        # Responses API: the terminal event carries response.usage.
        if payload.get("type") == "response.completed":
            usage = openai_responses_usage(_mapping(payload.get("response")))
            if usage is not None:
                self._usage = usage
            return

        # Chat Completions: the final chunk has empty choices plus usage.
        usage = openai_chat_usage(payload)
        if usage is not None:
            self._usage = usage


def _iter_json_objects(text: str) -> Iterator[str]:
    """Yield top-level ``{...}`` objects, tolerating ``[`` and ``,`` wrappers."""

    depth = 0
    start = -1
    in_string = False
    escaped = False
    for index, char in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            if depth == 0:
                start = index
            depth += 1
        elif char == "}":
            if depth > 0:
                depth -= 1
                if depth == 0 and start >= 0:
                    yield text[start:index + 1]
                    start = -1


class GeminiStreamUsage:
    """Accumulate the last ``usageMetadata`` from either Gemini stream format.

    Gemini emits SSE when ``alt=sse`` is set and a plain JSON array otherwise.
    The array body is parsed once complete; leading ``[`` / ``,`` separators and
    chunk splits at arbitrary byte boundaries are tolerated.
    """

    def __init__(self) -> None:
        self._decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self._mode: str | None = None
        self._sse = SSEDecoder()
        self._text = ""
        self._usage: Usage | None = None
        self._finished = False

    @property
    def usage(self) -> Usage | None:
        return self._usage

    def feed(self, chunk: bytes | str) -> Usage | None:
        text = chunk if isinstance(chunk, str) else self._decoder.decode(chunk)
        if self._mode is None:
            stripped = text.lstrip()
            if stripped:
                if stripped.startswith("data:") or stripped.startswith(":"):
                    self._mode = "sse"
                else:
                    # JSON array, a lone object, or comma separated objects.
                    self._mode = "json"
        if self._mode == "sse":
            for data in self._sse.feed(text):
                self._consume(data)
        else:
            self._text += text
        return self._usage

    def finish(self) -> Usage | None:
        if self._finished:
            return self._usage
        self._finished = True
        if self._mode == "sse":
            for data in self._sse.finish():
                self._consume(data)
        else:
            self._text += self._decoder.decode(b"", final=True)
            self._scan_json(self._text)
        return self._usage

    def _scan_json(self, text: str) -> None:
        for raw in _iter_json_objects(text):
            try:
                payload = json.loads(raw)
            except (json.JSONDecodeError, ValueError):
                continue
            self._consume_payload(payload)

    def _consume(self, data: str) -> None:
        stripped = data.strip()
        if not stripped or stripped == "[DONE]":
            return
        try:
            payload = json.loads(stripped)
        except (json.JSONDecodeError, ValueError):
            return
        self._consume_payload(payload)

    def _consume_payload(self, payload: Any) -> None:
        usage = gemini_usage(_mapping(payload))
        if usage is not None:
            self._usage = usage


# -------------------------------------------------------- convenience wrappers


def parse_openai_chat_stream(chunks: Iterable[bytes | str]) -> Usage | None:
    parser = OpenAIStreamUsage()
    for chunk in chunks:
        parser.feed(chunk)
    return parser.finish()


def parse_openai_responses_stream(chunks: Iterable[bytes | str]) -> Usage | None:
    return parse_openai_chat_stream(chunks)


def parse_gemini_stream(chunks: Iterable[bytes | str]) -> Usage | None:
    parser = GeminiStreamUsage()
    for chunk in chunks:
        parser.feed(chunk)
    return parser.finish()


# Descriptive aliases for embedders that prefer the "parse_*_usage" spelling.
parse_openai_chat_usage = openai_chat_usage
parse_openai_responses_usage = openai_responses_usage
parse_gemini_usage = gemini_usage
