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

TEXT_FIELDS = (
    "messages",
    "input",
    "contents",
    "system",
    "instructions",
    "systemInstruction",
    "system_instruction",
    "tools",
    "toolConfig",
    "tool_config",
    "cachedContent",
    "cached_content",
    "prediction",
)


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


GEMINI_SYSTEM_PROMPT = "You are a careful coding agent with a long system prompt. " * 400
CODEX_INSTRUCTIONS = "You are Codex, an agentic coding assistant with a long prompt. " * 400


def _gemini_cli_body(system_prompt: str) -> dict:
    return {
        "model": "gemini-2.5-flash",
        "contents": [{"role": "user", "parts": [{"text": "fix the failing test"}]}],
        "systemInstruction": {"parts": [{"text": system_prompt}]},
    }


def _responses_body(instructions: str) -> dict:
    return {
        "model": "gpt-5-codex",
        "input": [
            {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "fix the failing test"}],
            }
        ],
        "instructions": instructions,
    }


def test_gemini_system_instruction_is_reserved_as_prompt():
    # Gemini CLI sends its large system prompt in ``systemInstruction`` only.
    body = _gemini_cli_body(GEMINI_SYSTEM_PROMPT)
    prompt_tokens = math.ceil(len(GEMINI_SYSTEM_PROMPT) / 3)

    assert estimate_input_tokens(body) >= prompt_tokens
    assert estimate_input_tokens(body) == _expected_estimate(body)

    price = load_pricing().get("google/gemini-2.5-flash")
    input_tokens, _output, usd = estimate_request("google", body, price)
    reserved = apply_input_safety_factor(input_tokens, Decimal("1.25"))
    assert reserved >= prompt_tokens
    assert usd >= reservation_usd(price, prompt_tokens, 0)


def test_gemini_snake_case_system_instruction_is_reserved_as_prompt():
    body = {
        "contents": [{"role": "user", "parts": [{"text": "hi"}]}],
        "system_instruction": {"parts": [{"text": GEMINI_SYSTEM_PROMPT}]},
    }
    assert estimate_input_tokens(body) >= math.ceil(len(GEMINI_SYSTEM_PROMPT) / 3)
    assert estimate_input_tokens(body) == _expected_estimate(body)


def test_openai_responses_instructions_are_reserved_as_prompt():
    # Codex sends its long system prompt in ``instructions`` only.
    body = _responses_body(CODEX_INSTRUCTIONS)
    prompt_tokens = math.ceil(len(CODEX_INSTRUCTIONS) / 3)

    assert estimate_input_tokens(body) >= prompt_tokens
    assert estimate_input_tokens(body) == _expected_estimate(body)

    price = load_pricing().get("openai/gpt-5.3-codex")
    input_tokens, _output, usd = estimate_request("openai", body, price)
    reserved = apply_input_safety_factor(input_tokens, Decimal("1.25"))
    assert reserved >= prompt_tokens
    assert usd >= reservation_usd(price, prompt_tokens, 0)


def test_nested_system_instruction_is_counted():
    body = {
        "requests": [
            {
                "contents": [{"parts": [{"text": "hi"}]}],
                "systemInstruction": {"parts": [{"text": GEMINI_SYSTEM_PROMPT}]},
            }
        ]
    }
    assert estimate_input_tokens(body) >= math.ceil(len(GEMINI_SYSTEM_PROMPT) / 3)


def _gemini_tool_body(description: str, param_count: int) -> dict:
    parameters = {
        "type": "object",
        "properties": {
            f"param_{index}": {
                "type": "string",
                "description": f"value for parameter {index}",
            }
            for index in range(param_count)
        },
        "required": ["param_0"],
    }
    return {
        "contents": [{"role": "user", "parts": [{"text": "use a tool"}]}],
        "tools": [
            {
                "functionDeclarations": [
                    {
                        "name": "read_many_files",
                        "description": description,
                        "parameters": parameters,
                    }
                ]
            }
        ],
    }


def test_gemini_function_declarations_are_counted():
    small = _gemini_tool_body("Read a file.", 1)
    large = _gemini_tool_body("Read a file. " * 200, 60)

    def dump(body: dict) -> str:
        return json.dumps(
            body["tools"], ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )

    schema_tokens = math.ceil(len(dump(large)) / 3)
    added_chars = len(dump(large)) - len(dump(small))

    assert estimate_input_tokens(large) >= schema_tokens
    assert estimate_input_tokens(large) > estimate_input_tokens(small)
    # Only the schema differs, so the reservation must grow by at least the
    # added schema text's token estimate.
    assert (
        estimate_input_tokens(large) - estimate_input_tokens(small)
        >= math.ceil(added_chars / 3)
    )


def test_shared_prompt_object_is_counted_once():
    shared = [{"role": "user", "content": "hello there " * 500}]
    body = {"messages": shared, "system": shared}
    assert estimate_input_tokens(body) == estimate_input_tokens({"messages": shared})


def test_tool_config_and_prediction_are_counted():
    base = {"contents": [{"role": "user", "parts": [{"text": "hi"}]}]}
    extra = {
        **base,
        "toolConfig": {
            "functionCallingConfig": {
                "mode": "ANY",
                "allowedFunctionNames": ["read_file"],
            }
        },
        "prediction": {"content": {"parts": [{"text": "predicted answer " * 50}]}},
    }
    assert estimate_input_tokens(extra) > estimate_input_tokens(base)


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
    # Thinking is billed separately from maxOutputTokens; a thinking-capable
    # model without an explicit budget gets the conservative default.
    assert max_output_tokens("google", body, price) == 4096 + 8192


def test_max_output_gemini_snake_case(store):
    price = store.get("google/gemini-2.5-flash")
    body = {"contents": [], "generation_config": {"max_output_tokens": 2048}}
    assert max_output_tokens("gemini", body, price) == 2048 + 8192


def test_max_output_gemini_includes_explicit_thinking_budget(store):
    price = store.get("google/gemini-2.5-flash")
    body = {
        "contents": [],
        "generationConfig": {
            "maxOutputTokens": 1000,
            "thinkingConfig": {"thinkingBudget": 2000},
        },
    }
    assert max_output_tokens("google", body, price) == 3000


def test_max_output_gemini_zero_thinking_budget_disables_default(store):
    price = store.get("google/gemini-2.5-flash")
    body = {
        "contents": [],
        "generationConfig": {
            "maxOutputTokens": 500,
            "thinkingConfig": {"thinkingBudget": 0},
        },
    }
    assert max_output_tokens("google", body, price) == 500


def test_max_output_falls_back_to_model_default(store):
    price = store.get("openai/gpt-4o-mini")
    assert max_output_tokens("openai", {"messages": []}, price) == price.max_output_tokens
    gemini = store.get("google/gemini-2.5-flash")
    assert (
        max_output_tokens("google", {"contents": []}, gemini)
        == gemini.max_output_tokens + 8192
    )


def test_max_output_multiplies_n(store):
    price = store.get("openai/gpt-4o-mini")
    body = {"messages": [], "max_tokens": 100, "n": 3}
    assert max_output_tokens("openai", body, price) == 300


def test_max_output_multiplies_gemini_candidate_count(store):
    price = store.get("google/gemini-2.5-flash")
    body = {
        "contents": [],
        "generationConfig": {
            "maxOutputTokens": 100,
            "candidateCount": 2,
            "thinkingConfig": {"thinkingBudget": 50},
        },
    }
    assert max_output_tokens("google", body, price) == (100 + 50) * 2


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

