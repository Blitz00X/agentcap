---
description: Add AgentCap's read-only statusLine to ~/.claude/settings.json (keeps and chains your current one)
argument-hint: 
allowed-tools: Bash(python3:*)
disable-model-invocation: true
---
!`python3 "${CLAUDE_PLUGIN_ROOT}/scripts/agentcap_cc.py" setup $ARGUMENTS --session "${CLAUDE_SESSION_ID}"`

Relay the agentcap output above to the user verbatim in one short message. Do not run any other tool.
