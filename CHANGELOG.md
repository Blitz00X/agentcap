# Changelog

## 0.2.0
- **Safer license activation**: `agentcap license activate` with no key prompts with hidden input on a terminal; `agentcap license activate -` (or piped stdin) reads the key from stdin; `AGENTCAP_LICENSE_KEY` is honoured. A key passed as an argument still works but lands in shell history, so docs now recommend the prompt.
- **Pricing copy**: Pro is $12 one-time during launch week (until 10 Nov), then $19.
- **`agentcap waste`** (Free, read-only): subagent/Opus share, cache misses, repeated tool calls and error streaks from local Claude Code and Codex logs, with a "Pro would have stopped this at $X" line.
- **`agentcap report --by agent`**: subagent lineage tree (Claude sidechains, Codex sub-threads).
- **Quota-mode loop breaker** (`agentcap run --quota N% -- claude|codex`): same tool call (name + input) 4 times in a row or 8 failing tool calls in a row stops the job on Pro (exit 4, `loop_detected`); Free prints what Pro would have done. Polling tools (`wait`, `sleep`, `write_stdin`, `TaskOutput`, `BashOutput`, ...) never count.
- **Claude Code plugin loop breaker**: `PreToolUse` denies the 4th identical call with `continue: false` (Pro) or shows one notice (Free); new `PostToolUse`/`PostToolUseFailure` hooks stop after 8 failing calls. State is hashes and counters only, pruned after 7 days on `SessionStart`.
- **Behaviour change: proxy loop threshold 5 → 4.** `agentcap run --cap` (API proxy) now blocks the 4th identical request instead of the 5th, matching every other breaker and the docs.
- Fix: `agentcap --help` crashed (unescaped `%` in the `budget` help text).
- Plugin manifest version 0.2.0; VS Code/Cursor extension `agentcap-0.2.0.vsix`.
- README and landing repositioned around "see where your agent wastes money, stop the loop".
