---
name: agentcap
description: Explains AgentCap, the quota cap and loop breaker for this Codex session. Use when the user asks how to limit, check, approve or clear AgentCap, or when a tool call was denied by AgentCap.
---

# AgentCap in Codex

AgentCap is a local guard. The user controls it by typing a prompt that starts with `agentcap`; that prompt is handled by AgentCap's hook and never reaches you.

| User types | Effect |
|---|---|
| `agentcap 10% 5h` | this session may use 10% of the 5-hour quota (`week` for the weekly quota) |
| `agentcap status` | current share of the limit |
| `agentcap off` | clear this session's limit |
| `agentcap approve` | raise the limit by 50% after a stop (Pro, at most twice) |
| `agentcap default 10% 5h` | every new session, `codex exec` included, starts with this limit |
| `agentcap default off` | no default limit |

Rules for you, the agent:

- You cannot run these commands yourself and must not try (a tool call that approves the limit is denied). Tell the user which one to type.
- When a tool call is denied with a message that starts with `agentcap:`, stop calling tools. Summarise what is done and what is left, and tell the user they can type `agentcap approve` or `agentcap off`.
- The quota % is account-wide: use on other devices or sessions counts too, and one large response can pass the limit by a few points.
