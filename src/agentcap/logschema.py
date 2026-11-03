"""Machine-readable harness log contracts: what AgentCap reads, per harness.

Every adapter in :mod:`agentcap.harness` and every quota reader in
:mod:`agentcap.quota` depends on a handful of keys in someone else's log
format.  When a harness renames or drops one, AgentCap must notice loudly (a
test failure) instead of silently reading zero tokens or zero quota.

``CONTRACTS`` records, per harness, the version whose real output was captured
and, per event, the dotted key paths AgentCap reads.  ``missing_fields`` is the
drift check: it reports which of those paths are gone from a captured event.

Stdlib only, no imports from other agentcap modules, so it can be used from
tests and from tooling that only reads the contract table.
"""

from __future__ import annotations

from typing import Any

CONTRACTS: dict[str, dict[str, Any]] = {
    "opencode": {
        "tested_version": "opencode 2.x (captured 2026-10-30)",
        "fields": {
            "tool_use": ("type", "part.tool", "part.state.status", "part.state.input"),
            "step_finish": (
                "type",
                "part.tokens.input",
                "part.tokens.output",
                "part.tokens.cache.read",
                "part.cost",
            ),
        },
    },
    "agy": {
        "tested_version": "agy 1.4.11",
        "fields": {
            "step_update/tool": (
                "event",
                "step_update.state",
                "step_update.step_type",
                "step_update.tool_info.name",
                "step_update.tool_info.parameters",
                "step_update.tool_info.output",
            ),
            "step_update/agent_response": (
                "event",
                "step_update.state",
                "step_update.usage.total_tokens",
            ),
            "result": ("event", "result.status"),
        },
    },
    "claude": {
        "tested_version": "Claude Code 2.1.316",
        "fields": {
            "statusline": (
                "rate_limits.five_hour.used_percentage",
                "rate_limits.five_hour.resets_at",
                "rate_limits.seven_day.used_percentage",
                "rate_limits.seven_day.resets_at",
            ),
            "pretooluse": ("hook_event_name", "tool_name", "tool_input"),
        },
    },
    "codex": {
        "tested_version": "codex rollout (2026-10-29)",
        "fields": {
            "function_call": (
                "type",
                "payload.type",
                "payload.name",
                "payload.arguments",
            ),
            "function_call_output": ("type", "payload.type", "payload.output"),
            "token_count": (
                "type",
                "payload.type",
                "payload.rate_limits.primary.used_percent",
                "payload.rate_limits.primary.window_minutes",
                "payload.rate_limits.secondary.used_percent",
            ),
        },
    },
}


def get_path(obj: Any, dotted: str) -> tuple[bool, Any]:
    """Walk ``dotted`` through nested dicts: ``(True, value)`` or ``(False, None)``.

    A missing key, or an intermediate value that is not a dict, counts as
    absent.  Present keys are returned even when their value is ``None``.
    """

    cur = obj
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return (False, None)
        cur = cur[part]
    return (True, cur)


def missing_fields(harness: str, event: str, obj: Any) -> list[str]:
    """Contract paths for ``harness``/``event`` that are absent from ``obj``."""

    return [path for path in CONTRACTS[harness]["fields"][event]
            if not get_path(obj, path)[0]]


__all__ = ["CONTRACTS", "get_path", "missing_fields"]
