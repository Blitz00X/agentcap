from __future__ import annotations

import json

import pytest

from agentcap.usage import (
    GeminiStreamUsage,
    OpenAIStreamUsage,
    SSEDecoder,
    Usage,
    gemini_usage,
    openai_chat_usage,
    openai_responses_usage,
    parse_gemini_stream,
    parse_openai_chat_stream,
)


def test_chat_usage_includes_cached_tokens():
    usage = openai_chat_usage(
        {
            "usage": {
                "prompt_tokens": 1000,
                "completion_tokens": 250,
                "prompt_tokens_details": {"cached_tokens": 400},
            }
        }
    )
    assert usage == Usage(1000, 400, 250)
    assert usage.uncached_input_tokens == 600


def test_chat_usage_without_usage_block_is_none():
    assert openai_chat_usage({"choices": []}) is None
    assert openai_chat_usage({"usage": None}) is None
    assert openai_chat_usage(None) is None


def test_chat_usage_tolerates_bad_token_values():
    usage = openai_chat_usage(
        {
            "usage": {
                "prompt_tokens": True,
                "completion_tokens": "12",
                "prompt_tokens_details": {"cached_tokens": -3},
            }
        }
    )
    assert usage == Usage(0, 0, 12)


def test_responses_usage_shape():
    usage = openai_responses_usage(
        {
            "usage": {
                "input_tokens": 800,
                "output_tokens": 120,
                "input_tokens_details": {"cached_tokens": 300},
            }
        }
    )
    assert usage == Usage(800, 300, 120)


def test_gemini_thoughts_billed_as_output_and_cached_input():
    usage = gemini_usage(
        {
            "usageMetadata": {
                "promptTokenCount": 1000,
                "candidatesTokenCount": 200,
                "thoughtsTokenCount": 50,
                "cachedContentTokenCount": 100,
            }
        }
    )
    assert usage == Usage(1000, 100, 250)


def test_gemini_snake_case_and_missing_block():
    assert gemini_usage({"usage_metadata": {"prompt_token_count": 10}}) == Usage(10, 0, 0)
    assert gemini_usage({"candidates": []}) is None


def test_usage_rejects_negative_tokens():
    with pytest.raises(ValueError):
        Usage(-1, 0, 0)


def test_sse_decoder_handles_chunk_split_mid_line():
    decoder = SSEDecoder()
    events = decoder.feed(b'data: {"a": ')
    assert events == []
    events += decoder.feed(b'1}\n\ndata: {"b": 2}\n\n')
    assert events == ['{"a": 1}', '{"b": 2}']
    assert decoder.finish() == []


def test_sse_decoder_handles_crlf_and_final_line_without_blank_line():
    decoder = SSEDecoder()
    assert decoder.feed(b'data: {"x": 1}\r\n\r\n') == ['{"x": 1}']
    assert decoder.feed(b'data: [DONE]') == []
    assert decoder.finish() == ["[DONE]"]


def test_openai_chat_stream_usage_from_final_chunk():
    chunks = [
        b'data: {"choices":[{"delta":{"content":"hel',
        b'lo"}}]}\n\ndata: {"choices":[],"usage":{"prompt_tokens":10',
        b'0,"completion_tokens":20,"prompt_tokens_details":{"cached_tokens":5}}}\n\n',
        b"data: [DONE]\n\n",
    ]
    assert parse_openai_chat_stream(chunks) == Usage(100, 5, 20)


def test_openai_stream_ignores_malformed_events():
    chunks = [
        b"data: not-json\n\n",
        b"event: ping\n\n",
        b'data: {"choices":[],"usage":{"prompt_tokens":7,"completion_tokens":3}}\n\n',
    ]
    assert parse_openai_chat_stream(chunks) == Usage(7, 0, 3)


def test_openai_stream_multibyte_split():
    payload = json.dumps(
        {
            "choices": [],
            "usage": {"prompt_tokens": 1, "completion_tokens": 2},
        },
        ensure_ascii=False,
    ).encode()
    # A snowman sits inside a content delta split across two byte chunks.
    data = b'data: {"choices":[{"delta":{"content":"\xe2\x98\x83"}}]}\n\n' + b"data: " + payload + b"\n\n"
    for split in range(1, len(data)):
        assert parse_openai_chat_stream([data[:split], data[split:]]) == Usage(1, 0, 2)


def test_openai_responses_stream_uses_response_completed():
    parser = OpenAIStreamUsage()
    parser.feed(
        b'event: response.created\ndata: {"type":"response.created","response":{}}\n\n'
    )
    assert parser.usage is None
    parser.feed(
        b'event: response.completed\n'
        b'data: {"type":"response.completed","response":{"usage":'
        b'{"input_tokens":90,"output_tokens":30,'
        b'"input_tokens_details":{"cached_tokens":10}}}}\n\n'
    )
    assert parser.finish() == Usage(90, 10, 30)


def test_gemini_sse_last_usage_wins():
    chunks = [
        b'data: {"candidates":[],"usageMetadata":{"promptTokenCount":100,'
        b'"candidatesTokenCount":10}}\n\n',
        b'data: {"candidates":[],"usageMetadata":{"promptTokenCount":100,'
        b'"candidatesTokenCount":40,"thoughtsTokenCount":5}}\n\n',
    ]
    assert parse_gemini_stream(chunks) == Usage(100, 0, 45)


def test_gemini_json_array_stream_split_anywhere():
    array = json.dumps(
        [
            {"candidates": [], "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 1}},
            {
                "candidates": [],
                "usageMetadata": {
                    "promptTokenCount": 10,
                    "candidatesTokenCount": 9,
                    "thoughtsTokenCount": 2,
                    "cachedContentTokenCount": 4,
                },
            },
        ]
    ).encode()
    for split in range(1, len(array)):
        assert parse_gemini_stream([array[:split], array[split:]]) == Usage(10, 4, 11)


def test_gemini_json_objects_without_brackets_and_commas():
    chunks = [
        b',{"usageMetadata":{"promptTokenCount":5,"candidatesTokenCount":1}}',
        b',{"usageMetadata":{"promptTokenCount":5,"candidatesTokenCount":2}}',
    ]
    assert parse_gemini_stream(chunks) == Usage(5, 0, 2)


def test_gemini_stream_tolerates_trailing_garbage():
    parser = GeminiStreamUsage()
    parser.feed(b'[{"usageMetadata":{"promptTokenCount":3,"candidatesTokenCount":1}}')
    parser.feed(b"] trailing")
    assert parser.finish() == Usage(3, 0, 1)
