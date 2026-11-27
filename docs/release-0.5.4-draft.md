# 0.5.4 draft notes (not released)

Goes into CHANGELOG.md as `## 0.5.4` when 0.5.4 ships with the other free scripts.

## 0.5.4
`agentcap waste` counts what was really spent. On 30 days of our own logs the total fell from $826.67 to $720.06 (13% was counted twice) and cache misses from $127.54 to $103.08; 29 repeat streaks became 8.
- **Resumed and forked sessions are counted once.** A resumed Claude Code session copies the earlier messages into its own file; the same request (message id + request id) is now counted only in the session that made it. A forked Codex thread replays its parent's tool calls; a call id already seen is skipped.
- **Codex code-mode polls are waits, not loops.** An `exec` script whose only tool calls are `tools.write_stdin` with empty input or `tools.wait` (Codex polling a running command) counts toward neither the repeat nor the error streak, in `waste` and in the live loop breaker. Typing into the process, or any other tool call in the script, still counts. Before, one run showed as `exec x57`.
- **`waste` skips waits before it builds streaks**, so a `sleep N` check between identical calls no longer splits a repeat streak in the report.
- **Opus is shown as a model choice**, dimmed, not in the red waste colour.
- README: try `waste` first with `uvx`, nothing installed; the "error streaks only on API keys" line was wrong (subscriptions use a `PostToolUseFailure` hook) and is fixed.
- **Not yet released:** PyPI and the latest GitHub Release are still 0.5.2; the plugins are 0.5.3.

