---
description: Raise this session's AgentCap limit by 50% (Pro, at most twice) after a stop
allowed-tools: Bash(python3:*)
disable-model-invocation: true
---
!`python3 "${CLAUDE_PLUGIN_ROOT}/scripts/agentcap_cc.py" approve --session "${CLAUDE_SESSION_ID}"`

Relay the agentcap output above to the user verbatim in one short message. Do not run any other tool.
