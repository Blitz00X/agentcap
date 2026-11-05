# Changelog

## 0.3.0
- **Fail-closed guards for unattended runs.** A second quota job on the same account is refused (exit 2; `AGENTCAP_ALLOW_PARALLEL=1` opts out); unknown opencode/agy log schemas, an unreadable Codex rollout log and AgentCap internal errors stop the agent with exit 5 on Pro instead of running unguarded.
- **Whole-process-tree stops.** Quota/harness stops send SIGINT to the process group, then SIGTERM, then SIGKILL (5 s / 3 s / 2 s); proxy (`--cap`) stops send SIGTERM then SIGKILL (5 s). Helpers left in the group are reaped after a normal exit. A process that changes its uid or enters another PID namespace is still out of scope.
- **Parent-death lifeline.** On Linux the guarded process starts through a small shim that arms `PR_SET_PDEATHSIG`, and a watchdog process SIGKILLs the agent's whole process group if AgentCap dies. So a SIGKILLed or OOM-killed AgentCap takes the agent with it, including a real agent started by a launcher (Codex's npm wrapper spawns the native binary as a child on Node without `process.execve`) and the agent's tool processes. A command that cannot start is still reported as "could not start the command" (exit 127).
- **Escaped-descendant sweep (T3).** On Linux AgentCap sets `PR_SET_CHILD_SUBREAPER` for the run, adopts descendants that call `setsid()`/double-fork, and sweeps them (SIGTERM → SIGKILL, PID start-time checked) after a normal exit or stop. The same `(pid, start)` identity is held through TERM, wait and KILL, so a reused PID is never signalled. Adopted helpers that already exited are reaped, so they no longer stretch the stop/cleanup waits. Unrelated children present before the run, and anything they start, are excluded. Daemons the agent starts (a dev server, a `tmux` server, a Gradle daemon) are part of its tree and are stopped at the end of the run. A descendant AgentCap may not signal (a root-owned `sudo -b` child, a setuid program) is tried once, not waited on (before: up to ~16 s of extra exit delay), and reported as `agentcap: could not stop N process(es) (permission denied): pid ...`; it is left running and not counted as killed. Residual: a process that left the agent's group (`setsid`/daemonized) before AgentCap itself is SIGKILLed can still escape; use a container/cgroup against hostile agents.
- **Self-approval guard.** `agentcap approve` refuses inside an agent session (`AGENTCAP_QUOTA_JOB`, `AGENTCAP_GUARDED`, `CLAUDECODE`); the Claude Code hook denies a Bash call that runs `agentcap approve`, including quoted, escaped and `$IFS`-obfuscated forms. Honest limit: a determined agent with the same OS user can still edit state; use a separate user/container against hostile agents.
- **Harness log contracts.** opencode, agy, Claude Code statusline/PreToolUse and Codex rollout fields are pinned in `agentcap/logschema.py` and checked against recorded fixtures, with an opt-in live check (`AGENTCAP_LIVE_CONTRACT=1`).
- **Private local state.** `~/.local/state/agentcap`, quota job files, the ledger, receipts and history are created 0700/0600 instead of inheriting a permissive umask. Only directories AgentCap creates, and its own default directories (`~/.local/state/agentcap`, `~/.local/share/agentcap`, `~/.config/agentcap` and their subdirectories), are made 0700; an existing directory chosen through `AGENTCAP_DB`, `AGENTCAP_RECEIPT_DIR`, `AGENTCAP_QUOTA_DIR`, `AGENTCAP_OPS_DIR` or a path argument (your `$HOME`, a project or a shared dir) keeps its mode, and no chmod follows a symlink.
- **Clearer approve message.** Approving after a loop/error stop says which breaker was cleared; at 100% it says the quota no longer stops the job instead of showing "150% of the quota".
- **opencode harness runs use `--standalone`.** Token/quota mode no longer fails when another opencode instance owns the shared managed-service port.
- Fix: proxy loop breaker missed tool loops when tool-call ids look like `call-<uuid>` (OpenRouter/opencode); hyphenated and UUID ids are now treated as volatile.
- Fix: harness token/quota cap alerts no longer print `$?` placeholders; quota alerts show the limit and a rounded share.
- Fix: harness quota share never goes backwards between polls (final summary showed 0.00%).
- Fix: no spurious "reported an API error" line after agentcap's own stop.
- Fix: quota deny is a pause the model resumes after `agentcap approve`, not a permanent stop.
- Fix: same-window % jitter (12→13→12) no longer counts as a window reset (inflated share to 13%).
- Fix: failing tool calls are counted live via Claude Code `PostToolUse`/`PostToolUseFailure` hooks, so parallel batches reach the error breaker.
- Fix: 429/5xx retries (identical resends) count neither as a repeated step nor as failing tool calls.
- Fix (F8): an upstream that rejects the credentials or account (HTTP 401/402/403, Gemini 400 `API_KEY_INVALID`) is reported plainly (`upstream rejected the request (HTTP 401: User not found.) — check the API key for <host>; nothing was spent.`, provider message truncated and key-redacted) instead of as a runaway loop; the agent's resends no longer count toward the loop breaker. Three rejections in a row stop the run with reason `upstream_auth_failed`, exit 2; if the agent gives up first its exit code is kept.
- `--quota` help now lists agy and says opencode exposes no quota.
- **opencode and agy (Antigravity CLI) support.** `agentcap run --tokens N -- opencode run ...` / `-- agy -p ...` reads the agent's own JSON event stream (`opencode run --format json`, `agy --output-format stream-json`) and enforces a token cap, the loop breaker (same tool call 4x) and the error breaker (8 failing tool calls in a row); stop is SIGINT. `--log-cap USD` caps on opencode's own cost estimate (for subscriptions such as opencode Go).
- **agy quota mode:** `agentcap run --quota 5% --window 5h -- agy -p ...` reads `remaining_fraction` from `agy -p /quota --output-format json` (no agent turn, no quota spent) for the model's group. opencode exposes no quota %, so `--quota` is refused for it with a pointer to `--tokens`/`--log-cap`.
- **$ mode:** `agentcap run --cap 1 -- opencode run -m agentcap/<model> ...` injects a provider through `OPENCODE_CONFIG_CONTENT` (user config untouched, run forced `--standalone`); `agy` is routed through the Gemini API route (`modelProvider: gemini`, `GEMINI_API_KEY`).
- Version 0.3.0 (package and plugin manifest).

## 0.2.2
- Codex quota under 1% (`--quota 0.5%`) now prints a warning at start. Codex reports quota in whole percent, so such a cap cannot trigger until a full 1% is used (seen in a real ChatGPT-plan run). README note added.
- Fix: `agentcap approve` on a Codex or headless job that was already stopped with SIGINT said "the run continues" and used up an approval. It now says the job was already stopped and changes nothing. Found in a real Codex run on a ChatGPT plan on 30 Oct.

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
