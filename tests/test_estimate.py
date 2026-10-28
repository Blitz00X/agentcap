from __future__ import annotations

import json
import math
from decimal import Decimal

import pytest

from agentcap.estimate import (
    apply_input_safety_factor,
    count_media_parts,
    estimate_input_tokens,
    estimate_request,
    max_output_tokens,
    reservation_usd,
)
from agentcap.pricing import load_pricing

TEXT_FIELDS = ("messages", "input", "contents", "system", "tools")


@pytest.fixture()
def store():
    return load_pricing()


def _expected_estimate(body: dict) -> int:
    chars = 0
    for field in TEXT_FIELDS:
        if field in body and body[field] is not None:
            chars += len(
                json.dumps(
                    body[field],
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
            )
    base = math.ceil(chars / 3)
    return math.ceil(base * 1.10) + 50 + count_media_parts(body) * 2000


def test_empty_body_has_flat_overhead_only():
    assert estimate_input_tokens({}) == 50


def test_chat_body_estimate_matches_formula():
    body = {
        "model": "gpt-4o-mini",
        "messages": [
            {"role": "system", "content": "Be terse."},
            {"role": "user", "content": "Hello world " * 100},
        ],
    }
    assert estimate_input_tokens(body) == _expected_estimate(body)
    assert estimate_input_tokens(body) > len("Hello world " * 100) / 3


def test_tools_and_system_are_counted():
    without_tools = {"messages": [{"role": "user", "content": "hi"}]}
    with_tools = {
        **without_tools,
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": "lookup",
                    "description": "Look something up in a very large schema",
                    "parameters": {"type": "object", "properties": {"q": {"type": "string"}}},
                },
            }
        ],
        "system": "You are a helpful agent.",
    }
    assert estimate_input_tokens(with_tools) > estimate_input_tokens(without_tools)


def test_openai_image_and_file_parts_each_add_flat_tokens():
    body = {
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "describe"},
                    {"type": "image_url", "image_url": {"url": "https://example.com/a.png"}},
                    {
                        "type": "file",
                        "file": {"file_id": "file-123", "filename": "a.pdf"},
                    },
                ],
            }
        ]
    }
    assert count_media_parts(body) == 2
    assert estimate_input_tokens(body) == _expected_estimate(body)


def test_responses_input_image_part_is_counted():
    body = {
        "input": [
            {
                "role": "user",
                "content": [
                    {"type": "input_text", "text": "what is this"},
                    {"type": "input_image", "image_url": "data:image/png;base64,AAAA"},
                ],
            }
        ]
    }
    assert count_media_parts(body) == 1
    assert estimate_input_tokens(body) == _expected_estimate(body)


def test_gemini_inline_and_file_data_are_counted():
    body = {
        "contents": [
            {
                "role": "user",
                "parts": [
                    {"text": "compare"},
                    {"inline_data": {"mime_type": "image/png", "data": "AAAA"}},
                    {"file_data": {"mime_type": "application/pdf", "file_uri": "gs://b/a.pdf"}},
                ],
            }
        ]
    }
    assert count_media_parts(body) == 2
    assert estimate_input_tokens(body) == _expected_estimate(body)


def test_gemini_camel_case_inline_data_is_counted():
    body = {"contents": [{"parts": [{"inlineData": {"mimeType": "image/png", "data": "x"}}]}]}
    assert count_media_parts(body) == 1


def test_max_output_prefers_max_completion_tokens(store):
    price = store.get("openai/gpt-4o-mini")
    body = {"messages": [], "max_tokens": 50, "max_completion_tokens": 100}
    assert max_output_tokens("openai", body, price) == 100


def test_max_output_chat_max_tokens(store):
    price = store.get("openai/gpt-4o-mini")
    assert max_output_tokens("openai", {"messages": [], "max_tokens": 123}, price) == 123


def test_max_output_responses_api(store):
    price = store.get("openai/gpt-4o")
    body = {"input": "hello", "max_output_tokens": 777}
    assert max_output_tokens("openai", body, price) == 777


def test_max_output_gemini_camel_case(store):
    price = store.get("google/gemini-2.5-flash")
    body = {"contents": [], "generationConfig": {"maxOutputTokens": 4096}}
    assert max_output_tokens("google", body, price) == 4096


def test_max_output_gemini_snake_case(store):
    price = store.get("google/gemini-2.5-flash")
    body = {"contents": [], "generation_config": {"max_output_tokens": 2048}}
    assert max_output_tokens("gemini", body, price) == 2048


def test_max_output_falls_back_to_model_default(store):
    price = store.get("openai/gpt-4o-mini")
    assert max_output_tokens("openai", {"messages": []}, price) == price.max_output_tokens
    gemini = store.get("google/gemini-2.5-flash")
    assert max_output_tokens("google", {"contents": []}, gemini) == gemini.max_output_tokens


def test_max_output_multiplies_n(store):
    price = store.get("openai/gpt-4o-mini")
    body = {"messages": [], "max_tokens": 100, "n": 3}
    assert max_output_tokens("openai", body, price) == 300


def test_max_output_multiplies_gemini_candidate_count(store):
    price = store.get("google/gemini-2.5-flash")
    body = {
        "contents": [],
        "generationConfig": {"maxOutputTokens": 100, "candidateCount": 2},
    }
    assert max_output_tokens("google", body, price) == 200


def test_invalid_n_is_treated_as_one(store):
    price = store.get("openai/gpt-4o-mini")
    body = {"messages": [], "max_tokens": 100, "n": 0}
    assert max_output_tokens("openai", body, price) == 100


def test_unknown_provider_rejected(store):
    with pytest.raises(ValueError):
        max_output_tokens("anthropic", {}, store.get("openai/gpt-4o"))


def test_estimate_request_is_consistent(store):
    price = store.get("openai/gpt-4o-mini")
    body = {"messages": [{"role": "user", "content": "hi"}], "max_tokens": 64}
    input_tokens, output_tokens, usd = estimate_request("openai", body, price)
    assert input_tokens == estimate_input_tokens(body)
    assert output_tokens == max_output_tokens("openai", body, price)
    assert usd == reservation_usd(price, input_tokens, output_tokens)
    assert usd > 0


def test_input_safety_factor_rounds_up_and_preserves_zero():
    assert apply_input_safety_factor(0, Decimal("1.25")) == 0
    assert apply_input_safety_factor(1, Decimal("1.25")) == 2
    assert apply_input_safety_factor(64, Decimal("1.25")) == 80
    assert apply_input_safety_factor(65, Decimal("1.25")) == 82
    assert apply_input_safety_factor(100, Decimal("1")) == 100


def test_input_safety_factor_rejects_invalid_values():
    with pytest.raises(TypeError):
        apply_input_safety_factor(10, 1.25)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        apply_input_safety_factor(-1, Decimal("1.25"))
    with pytest.raises(ValueError):
        apply_input_safety_factor(10, Decimal("NaN"))
    with pytest.raises(ValueError):
        apply_input_safety_factor(10, Decimal("-1"))

