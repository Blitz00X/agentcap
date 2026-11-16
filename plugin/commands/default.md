---
description: Give every new Claude Code session (also claude -p) a limit: 10% 5h, until 70% 5h, or off
argument-hint: <N%> | until <N%>  [week|5h] | off
allowed-tools: Bash(python3:*)
disable-model-invocation: true
---
!`python3 "${CLAUDE_PLUGIN_ROOT}/scripts/agentcap_cc.py" default $ARGUMENTS --session "${CLAUDE_SESSION_ID}"`

Relay the agentcap output above to the user verbatim in one short message. Do not run any other tool.
