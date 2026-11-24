# AgentCap for Claude Code

Stop a Claude Code session before it loops or eats your Max/Pro quota.

AgentCap watches every tool call through Claude Code hooks. It warns, and on Pro ends the turn,
when a session reaches its share of your weekly or 5-hour quota, repeats the same tool call 4 times
in a row, or fails 8 tool calls in a row. Every stop says why, in plain numbers.

## Install

```
/plugin marketplace add Blitz00X/agentcap
/plugin install agentcap@agentcap
/agentcap:setup        # once: adds AgentCap's read-only statusLine (your own line keeps showing)
```

Needs `python3` on PATH. No pip install, no account, no network access.

## Use

```
/agentcap:quota 5%          # this session may use 5 more points of the weekly quota
/agentcap:quota 2% 5h       # or 2 more points of the 5-hour window
/agentcap:quota until 70% 5h  # or stop when the 5-hour window reaches 70%
/agentcap:status            # where this session stands and where it stops
/agentcap:default 10% 5h    # give every new session (also claude -p) a limit; `off` removes it
/agentcap:approve           # Pro, after a stop: raise the limit by 50% (at most twice)
/agentcap:off               # clear this session's limit
/agentcap:unsetup           # restore your previous statusLine and delete AgentCap's settings backup
```

You can also ask in plain words. Claude Code shows a permission prompt with the stop point, and
the limit is set only after you approve:

- "Use at most 10% of the weekly quota in this session."
- "Stop when the 5-hour limit reaches 70%."
- "Why did AgentCap stop my last turn?"

The agent may only set or tighten a limit. Raising it, clearing it and approving a stop stay
yours: AgentCap denies any tool call by the agent that would run its script or edit its state.

## Free and Pro

- **Free (MIT):** the live quota meter, a warning at 80% of the limit, and one notice when a limit,
  a loop or an error streak is reached ("AgentCap Pro would stop the agent here"). Nothing is
  blocked.
- **Pro ($7 one-time during the beta, $12 after):** the same detectors end the turn
  (`PreToolUse` deny with `continue: false`), block new prompts while the session is over its
  limit, and write a receipt. Pro is unlocked by a licence key that you activate with the separate
  `agentcap` CLI (`pipx install agentcap-cli`, then `agentcap license activate`). Buy at
  https://agentcap.launchbench.studio/#pricing. 14-day refund.

## What the plugin runs, changes, stores and sends

- **Runs:** `python3 ${CLAUDE_PLUGIN_ROOT}/scripts/agentcap_cc.py` on SessionStart,
  UserPromptSubmit, PreToolUse, PostToolUse, PostToolUseFailure and Stop, and for the slash
  commands above. Standard library only. While a limit is set it may start
  `claude -p /usage` in the background, at most every 30 seconds, to read the current quota
  (a local Claude Code command; no model call). Its statusLine command also runs your previous
  user-level statusLine command, unchanged, so your own line keeps showing.
- **Changes:** `/agentcap:setup` sets only the `statusLine` key in `~/.claude/settings.json` and
  keeps an owner-only backup of that file (`settings.json.agentcap-backup`).
  `/agentcap:unsetup` restores your previous statusLine and deletes the backup.
- **Stores, on your machine only** (`~/.local/state/agentcap/quota/`): session ids, quota
  percentages and reset times, counters, the tool name and a 16-character hash of each tool call,
  and receipts (numbers and the last tool name). Never prompts, tool inputs, tool output, tokens or
  credentials. Loop counters are deleted after 7 days, session files after 30 days, receipts after
  90 days.
- **Reads:** the `rate_limits` values Claude Code passes to the statusLine, Claude Code's own
  `/usage` output, and the licence file `~/.config/agentcap/license.json` if it exists.
- **Sends:** nothing. The plugin makes no network requests. The separate `agentcap` CLI checks a
  Pro key with Polar's licence API about once a week; Free users make no licence requests at all.

Privacy policy: https://agentcap.launchbench.studio/privacy

## Limits

- The quota % is account-level: other sessions, devices and claude.ai use in the same window count
  against this session's limit.
- A stop can be one response (or one 30-second `/usage` check) late, so a session can pass its
  limit by a few points.
- macOS and Windows are not tested yet; Windows needs WSL.

## Support

- Issues: https://github.com/Blitz00X/agentcap/issues
- Security reports: https://github.com/Blitz00X/agentcap/security/policy
- Source and the full CLI documentation: https://github.com/Blitz00X/agentcap

MIT License. Copyright (c) 2026 Kutay Karahan.
