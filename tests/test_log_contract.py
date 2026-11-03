"""Harness log contracts: captured fixtures must still satisfy what we read.

Each harness has real, captured output under ``tests/fixtures/harness_logs/``
(see :data:`agentcap.logschema.CONTRACTS` for the captured versions).  These
tests do two things:

1. classify every fixture line into a contract event and assert the contract
   paths AgentCap reads are still present (so a harness renaming a key fails
   here instead of silently reading zero);
2. run the real adapters/readers over the same fixtures, so the contract can
   never drift into describing fields nothing actually uses;
3. prove the drift check itself catches a removed key and a renamed top-level
   key.

If a fixture ever stops matching a path, the contract is right and the fixture
(or the harness) changed: report it, do not weaken the contract.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any, Callable

import pytest

from agentcap.harness import AgyAdapter, OpenCodeAdapter, ToolCall, Usage
from agentcap.logschema import CONTRACTS, missing_fields
from agentcap.quota import claude_readings, codex_readings

FIXTURES = Path(__file__).parent / "fixtures" / "harness_logs"

JSONL_FIXTURES = {
    "opencode": "opencode-2026-10-30.jsonl",
    "agy": "agy-1.4.11-2026-10-30.jsonl",
    "codex": "codex-rollout-2026-10-29.jsonl",
}

CLAUDE_FIXTURES = {
    "statusline": "claude-2.1.316-statusline.json",
    "pretooluse": "claude-2.1.316-pretooluse.json",
}


def _jsonl(name: str) -> list[dict[str, Any]]:
    text = (FIXTURES / name).read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def _json(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _fields(harness: str) -> dict[str, tuple[str, ...]]:
    return CONTRACTS[harness]["fields"]


def _classify_opencode(obj: Any) -> str | None:
    kind = obj.get("type") if isinstance(obj, dict) else None
    return kind if kind in _fields("opencode") else None


def _classify_agy(obj: Any) -> str | None:
    if not isinstance(obj, dict):
        return None
    if obj.get("event") == "result":
        return "result"
    step = obj.get("step_update")
    if obj.get("event") != "step_update" or not isinstance(step, dict):
        return None
    if step.get("state") != "DONE" or step.get("step_type") not in ("tool", "agent_response"):
        return None
    return f"step_update/{step['step_type']}"


def _classify_codex(obj: Any) -> str | None:
    payload = obj.get("payload") if isinstance(obj, dict) else None
    kind = payload.get("type") if isinstance(payload, dict) else None
    return kind if kind in _fields("codex") else None


CLASSIFIERS: dict[str, Callable[[Any], str | None]] = {
    "opencode": _classify_opencode,
    "agy": _classify_agy,
    "codex": _classify_codex,
}


def classified(harness: str) -> list[tuple[str, dict[str, Any]]]:
    """``(event, object)`` for every fixture payload of ``harness``."""

    if harness == "claude":
        return [(event, _json(name)) for event, name in CLAUDE_FIXTURES.items()]
    classify = CLASSIFIERS[harness]
    out: list[tuple[str, dict[str, Any]]] = []
    for obj in _jsonl(JSONL_FIXTURES[harness]):
        event = classify(obj)
        if event is not None:
            out.append((event, obj))
    return out


def _delete_last_key(obj: dict[str, Any], dotted: str) -> None:
    parts = dotted.split(".")
    parent: Any = obj
    for part in parts[:-1]:
        parent = parent[part]
    del parent[parts[-1]]


# --------------------------------------------------------------- 1. contract
@pytest.mark.parametrize("harness", sorted(CONTRACTS))
def test_every_contract_event_is_covered_by_a_fixture(harness):
    version = CONTRACTS[harness]["tested_version"]
    assert isinstance(version, str) and version.strip()

    seen = {event for event, _ in classified(harness)}
    assert seen == set(_fields(harness)), (
        f"{harness}: fixture lines cover {sorted(seen)}, contract says "
        f"{sorted(_fields(harness))}"
    )


@pytest.mark.parametrize("harness", sorted(CONTRACTS))
def test_fixture_lines_still_have_every_contract_path(harness):
    problems: list[str] = []
    for event, obj in classified(harness):
        missing = missing_fields(harness, event, obj)
        if missing:
            problems.append(f"{event}: {missing}")
    assert problems == [], f"{harness} fixtures drifted from the contract: {problems}"


# ---------------------------------------------------------- 2. real readers
def test_opencode_adapter_still_extracts_from_fixture():
    adapter = OpenCodeAdapter()
    parsed = [ev for obj in _jsonl(JSONL_FIXTURES["opencode"]) for ev in adapter.parse(obj)]
    calls = [ev for ev in parsed if isinstance(ev, ToolCall)]
    usages = [ev for ev in parsed if isinstance(ev, Usage)]
    assert len(calls) >= 1
    assert len(usages) >= 1
    assert any(usage.tokens > 0 for usage in usages)


def test_agy_adapter_still_extracts_from_fixture():
    adapter = AgyAdapter()
    parsed = [ev for obj in _jsonl(JSONL_FIXTURES["agy"]) for ev in adapter.parse(obj)]
    calls = [ev for ev in parsed if isinstance(ev, ToolCall)]
    usages = [ev for ev in parsed if isinstance(ev, Usage)]
    assert len(calls) >= 1
    assert len(usages) >= 1
    assert any(usage.tokens > 0 for usage in usages)


def test_claude_readings_still_extract_from_statusline_fixture():
    readings = claude_readings(_json(CLAUDE_FIXTURES["statusline"]))
    assert {"5h", "week"} <= set(readings)
    assert readings["5h"].used == 12
    assert readings["week"].used == 31


def test_codex_readings_still_extract_from_token_count_fixture():
    line = next(obj for obj in _jsonl(JSONL_FIXTURES["codex"])
                if _classify_codex(obj) == "token_count")
    readings = codex_readings(line, now=0)
    assert {"5h", "week"} <= set(readings)
    assert readings["5h"].used == 7.0
    assert readings["week"].used == 21.0


# ------------------------------------------------------------- 3. drift check
@pytest.mark.parametrize("harness", sorted(CONTRACTS))
def test_drift_check_reports_a_removed_contract_path(harness):
    event, obj = classified(harness)[0]
    path = _fields(harness)[event][0]
    drifted = copy.deepcopy(obj)
    _delete_last_key(drifted, path)

    assert missing_fields(harness, event, drifted) == [path]
    # Sanity: the untouched copy is still complete, so the report is the deletion.
    assert missing_fields(harness, event, obj) == []


def test_renamed_opencode_top_level_key_breaks_the_envelope():
    adapter = OpenCodeAdapter()
    obj = next(o for o in _jsonl(JSONL_FIXTURES["opencode"]) if o.get("type") == "tool_use")
    assert adapter.envelope(obj) is True

    drifted = copy.deepcopy(obj)
    drifted["parts"] = drifted.pop("part")
    assert adapter.envelope(drifted) is False
