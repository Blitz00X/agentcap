---
description: "Limit this session: 5% 5h = 5 more points of the 5-hour quota; until 70% 5h = stop when it reaches 70%"
argument-hint: <N%> | until <N%>  [week|5h]
allowed-tools: Bash(python3 "${CLAUDE_PLUGIN_ROOT}/scripts/agentcap_cc.py" quota *)
disable-model-invocation: true
---
!`python3 "${CLAUDE_PLUGIN_ROOT}/scripts/agentcap_cc.py" quota $ARGUMENTS --session "${CLAUDE_SESSION_ID}"`

Relay the agentcap output above to the user verbatim in one short message. Do not run any other tool.
