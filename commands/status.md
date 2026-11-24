---
description: "Show this session's quota share"
allowed-tools: Bash(python3 "${CLAUDE_PLUGIN_ROOT}/scripts/agentcap_cc.py" status *)
disable-model-invocation: true
---
!`python3 "${CLAUDE_PLUGIN_ROOT}/scripts/agentcap_cc.py" status $ARGUMENTS --session "${CLAUDE_SESSION_ID}"`

Relay the agentcap output above to the user verbatim in one short message. Do not run any other tool.
