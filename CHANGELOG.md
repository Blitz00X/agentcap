# Changelog

## 0.2.1
- **Interactive Claude Code stays open on a stop.** The quota and loop stops deny the tool call and end the turn (`continue: false`), in the plugin and in `agentcap run --quota -- claude`. An interactive session is no longer sent SIGINT while the hook works. Headless `claude -p` runs and Codex are still stopped with SIGINT. If the hook never fires, SIGINT is still the fallback.
- **`agentcap approve` / `/agentcap:approve` (Pro).** Raises a stopped job's or session's limit by 50%, at most twice, and clears a loop-breaker streak so the run can continue. Free never blocks, so there is nothing to approve.
- **Receipts name the last tool call.** Only the tool name is stored, never its input.
- **`agentcap waste`:**
  - Cache misses are split into main session and subagents.
  - Subagent misses get a `subagentPromptCacheTtl: "1h"` tip.
  - New `STARTING CONTEXT` line: first-request tokens per subagent.
- **Fix: cache-miss $ in `waste`.** A 5-minute re-write was priced at the 1-hour write rate (2x input). It now uses 1.25x input for 5-minute writes and 2x for 1-hour writes, so 5-minute misses were overstated before. History import now prices 1-hour cache writes at 2x input; before, they were priced at 1.25x.
- README: a Codex $20 plan example, a comparison with spend-stop and RelayPlane, and the main vs subagent cache facts as Anthropic stated them.
- Version 0.2.1 (package and plugin manifest).

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
