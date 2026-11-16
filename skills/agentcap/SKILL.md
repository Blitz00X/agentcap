---
name: agentcap
description: Sets an AgentCap quota limit for this Claude Code session when the user asks in plain words, e.g. "use at most 50% of the 5-hour quota", "stop at 70% of the weekly limit", "don't spend more than 10% this session". Also explains AgentCap stops and denials.
---

# AgentCap in Claude Code

AgentCap limits how much of the subscription quota (5-hour or weekly window) this session may use.

## When the user asks for a limit

Turn the request into one command and run it with the Bash tool, alone, exactly in this form:

| User says | Run |
|---|---|
| "use at most 50% of the 5-hour quota", "spend 10 more points this session" | `echo agentcap set 50% 5h` |
| "stop when the 5-hour quota reaches 70%", "don't go past 70%" | `echo agentcap set until 70% 5h` |
| same, weekly | add `week` instead of `5h` |

- `N% 5h` means *N more points* from where the window is now; `until N% 5h` means *stop at N%*. If the user's words are ambiguous ("50% of the quota"), pick the form that matches better and say which one you chose.
- Claude Code then asks the user to approve the command (AgentCap shows where the session would stop). The limit is set only after the user approves. Report the stop point AgentCap returns.
- You may only set or tighten a limit. Raising it, clearing it and approving a stop belong to the user: tell them to run `/agentcap:approve`, `/agentcap:quota <N%> 5h` or `/agentcap:off`. Never try to change AgentCap's files or run its plugin script.

## When AgentCap stops you

A tool call denied with a message that starts with `agentcap:` (quota, loop or error stop) means: stop calling tools, summarise what is done and what is left, and tell the user about `/agentcap:approve` (+50%), `/agentcap:quota` or `/agentcap:off`.

The quota % is account-wide: use on other devices or sessions counts too, and one large response can pass the limit by a few points.
