---
name: agentcap
description: Explains AgentCap, the quota cap and loop breaker for this Codex session. Use when the user asks how to limit, check, approve or clear AgentCap, or when a tool call was denied by AgentCap.
---

# AgentCap in Codex

AgentCap is a local guard. The user controls it by typing a prompt that starts with `agentcap`; that prompt is handled by AgentCap's hook and never reaches you.

| User types | Effect |
|---|---|
| `agentcap 10% 5h` | this session may use 10 more points of the 5-hour quota (`week` for the weekly quota) |
| `agentcap until 70% 5h` | stop when the 5-hour quota reaches 70% |
| `agentcap status` | current share of the limit |
| `agentcap off` | clear this session's limit |
| `agentcap approve` | raise the limit by 50% after a stop (Pro, at most twice) |
| `agentcap default 10% 5h` | every new session, `codex exec` included, starts with this limit (`until 70%` works too) |
| `agentcap default off` | no default limit |

Rules for you, the agent:

- You cannot run these commands yourself and must not try (a tool call that approves the limit is denied). Tell the user which one to type.
- When a tool call is denied with a message that starts with `agentcap:` (a quota, loop or error stop), stop calling tools. Summarise what is done and what is left, and tell the user they can type `agentcap approve` or `agentcap off`.
- A denial that starts with `agentcap guard (not a quota stop)` only refuses that one command (approving the limit yourself, or changing AgentCap's state or installed plugin files). Do not retry it; continue the task another way. Reading files, editing the project and running its tests are not affected.
- The quota % is account-wide: use on other devices or sessions counts too, and one large response can pass the limit by a few points.
