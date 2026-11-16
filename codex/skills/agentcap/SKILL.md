---
name: agentcap
description: Sets an AgentCap quota limit for this Codex session when the user asks in plain words ("use at most 50% of the 5-hour quota", "stop at 70% of the weekly limit"), and explains AgentCap commands, stops and denials.
---

# AgentCap in Codex

AgentCap is a local guard that limits how much of the ChatGPT plan quota (5-hour or weekly window) this session may use.

## When the user asks for a limit in plain words

Turn the request into one command and run it with the shell tool, alone, with `sandbox_permissions: "require_escalated"` and a short justification such as `AgentCap: stop this session at 70% of the 5-hour quota`:

| User says | Run |
|---|---|
| "use at most 50% of the 5-hour quota", "spend 10 more points this session" | `echo agentcap set 50% 5h` |
| "stop when the 5-hour quota reaches 70%", "don't go past 70%" | `echo agentcap set until 70% 5h` |
| same, weekly | `week` instead of `5h` |

- `N% 5h` means *N more points* from where the window is now; `until N% 5h` means *stop at N%*. If the words are ambiguous, pick the closer form and say which one you chose.
- Codex asks the user to approve the command; that is the user's one button. AgentCap sets the limit only after the command ran and shows where the session stops. If AgentCap denies the command and tells you how to run it, do exactly that once.
- You may only set or tighten a limit. Raising, clearing and approving belong to the user.

## Commands the user types

The user can also type these as a prompt; AgentCap's hook handles them and they never reach you.

| User types | Effect |
|---|---|
| `agentcap 10% 5h` | this session may use 10 more points of the 5-hour quota (`week` for the weekly quota) |
| `agentcap until 70% 5h` | stop when the 5-hour quota reaches 70% |
| `agentcap status` | current share of the limit |
| `agentcap off` | clear this session's limit |
| `agentcap approve` | raise the limit by 50% after a stop (Pro, at most twice) |
| `agentcap default 10% 5h` | every new session, `codex exec` included, starts with this limit (`until 70%` works too) |
| `agentcap default off` | no default limit |

## Rules

- You cannot run the user's commands yourself and must not try (a tool call that approves the limit is denied). Tell the user which one to type.
- When a tool call is denied with a message that starts with `agentcap:` (a quota, loop or error stop), stop calling tools. Summarise what is done and what is left, and tell the user they can type `agentcap approve` or `agentcap off`.
- A denial that starts with `agentcap guard (not a quota stop)` only refuses that one command (approving or loosening the limit yourself, or changing AgentCap's state or installed plugin files). Do not retry it; continue the task another way. Reading files, editing the project and running its tests are not affected.
- The quota % is account-wide: use on other devices or sessions counts too, and one large response can pass the limit by a few points.
