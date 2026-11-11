# AgentCap 0.4 — stop-path matrix (stage 1)

Date: 11 Nov 2026 · Branch: `quota-sources-0.4` · Scope: quota mode for Codex CLI
(ChatGPT login) and Claude Code (Pro/Max). No push, no release.

The matrix lists every stop path, the code that owns it, the test that proves it
end to end (when one exists), the command to reproduce it, and the exit code it
must produce. New integration tests live in `tests/test_stop_matrix.py`; the
guards' unit tests are named where the integration path is covered elsewhere.

## How to run

```bash
cd /home/kutay/agentcap-04
/home/kutay/agentcap-ha-venv/bin/python -m pytest            # full suite
/home/kutay/agentcap-ha-venv/bin/python -m pytest tests/test_stop_matrix.py -v
```

Last full run (11 Nov 2026, after the stage-1 commits): **1041 passed,
2 skipped, exit 0, 172 s**.

## Matrix

| # | Path | Owner | Integration test | Exit | Evidence |
|---|------|-------|------------------|------|----------|
| 1 | Quota stop, Codex | `QuotaRunner` (SIGINT) | `test_stop_matrix.py::test_codex_quota_stop_exits_3_with_receipt_and_leaves_no_survivor`, `test_quota.py::test_e2e_codex_sigint_at_quota` | 3 | receipt `q*.json` (`reason=quota_exceeded`, `measure=account_increase`, `final_read=complete`), `quota_exceeded` alert, no surviving agent/helper |
| 2 | Quota stop, Claude (hook deny) | `hook_main` + `QuotaRunner` | `test_quota.py::test_e2e_claude_denied_at_quota`, `::test_e2e_interactive_claude_is_not_sigint_when_hook_denies` | 3 | `denies.log`, `continue:false` deny, session stays open, receipt |
| 3 | Loop breaker (same tool 4×) | `LoopWatch`, `_loop_hook` | `test_stop_matrix.py::test_codex_loop_breaker_exits_4_with_receipt`, `test_quota.py::test_e2e_codex_loop_breaker_stops_subscription_run`, `::test_e2e_claude_loop_breaker_denies_and_stops` | 4 | receipt `reason=loop_detected`, "same tool call 4 times" |
| 4 | Error breaker (8 failing calls) | `post_main`, `_error_hook` | `test_stop_matrix.py::test_claude_error_breaker_stops_after_eight_failures` (interactive + headless) | 4 | `denies.log`, receipt `reason=loop_detected`, `loop.json.kind=errors` |
| 5 | Process-tree stop ladder | `procstop.stop_tree` | `test_stop_matrix.py::test_codex_stop_escalates_past_ignored_sigint_and_sigterm`; `test_procstop.py::test_stop_tree_escalates_to_sigkill`, `::test_stop_tree_sigint_kills_grandchild`, `::test_stop_tree_kills_orphans_after_leader_exits`, `::test_never_signals_own_group` | 3 | elapsed ≥ 5 s proves the 5 s SIGINT wait + 3 s SIGTERM wait ran before SIGKILL; all PIDs gone |
| 6 | Lifeline / PDEATHSIG | `lifeline._lifeline_exec`, `GroupWatchdog` | `test_lifeline.py::test_child_dies_when_its_parent_is_sigkilled`, `::test_agent_group_dies_with_a_sigkilled_agentcap_in_quota_mode` | child exit | SIGKILL of AgentCap leaves no agent tool process |
| 7 | Escaped child sweep (setsid/double-fork) | `ProcessTreeGuard.sweep` | `test_stop_matrix.py::test_codex_quota_stop_exits_3_with_receipt_and_leaves_no_survivor`; `test_quota.py::test_quota_runner_sweeps_a_setsid_descendant`; `test_procstop.py::test_setsid_grandchild_is_swept_after_leader_exit`, `::test_double_forked_daemon_is_swept`, `::test_sweep_never_sigkills_a_reused_pid` | 3 / 0 | "left the agent's process group" note; setsid/double-fork PID gone; reused PIDs never signalled |
| 8 | Approve (continue, +50%, max 2) | `approve_job`, runner poll | `test_quota.py::test_approve_raises_quota_50pct_at_most_twice`, `::test_runner_picks_up_approval`, `::test_approve_refuses_a_job_already_stopped_by_sigint`, `::test_approve_at_full_quota_says_quota_no_longer_stops` | continues / 0 | message "limit raised to X%"; third approve refused; a signalled job is not resumable |
| 9 | Plugin PreToolUse deny/continue + headless | `plugin/`, `hook_main` | `test_cc_plugin.py::test_quota_deny_ends_the_turn`, `::test_loop_pro_denies_fourth_identical_call`, `::test_hook_garbage_fails_open`; `test_quota.py::test_e2e_headless_claude_still_sigint`, `::test_headless_claude_quota_warns_interactive_only` | 4 / 0 | deny JSON has `continue:false`; headless quota % is declared unsupported (no statusline) |
| 10 | Parallel quota job refused | `failsafe.JobLock` | `test_exit_codes.py::test_regression_another_quota_job_holding_the_lock_is_still_2`; `test_failsafe.py::test_joblock_refuses_second_holder`; account-level: `test_quota.py::test_e2e_codex_parallel_session_warns` | 2 | "another AgentCap quota job is already running (pid N)" |
| 11 | Missing/stale schema fails closed | `SchemaWatch`, `CodexTailer`, strict checks | `test_stop_matrix.py::test_codex_missing_schema_fails_closed_with_exit_5`, `test_quota.py::test_codex_strict_stops_when_token_count_has_no_rate_limits`, `::test_codex_exec_strict_stops_when_no_session_log_appears`, `test_failsafe.py::test_harness_unknown_schema_stops_agent` | 5 | receipt `reason=guard_failed`; `observed=false`; "no subscription rate_limits" message |
| 12 | Unknown state is not shown as protected | `QuotaRunner` warnings | `test_quota.py::test_e2e_claude_api_key_login_warns`, `::test_codex_tui_without_session_log_only_warns`, `test_failsafe.py::test_codex_tailer_counts_unreadable_log` | 0 | "protection not verified" / "no subscription rate_limits" — never a receipt that claims a stop |
| 13 | Receipt / summary exit codes | `QuotaRunner._receipt` | `test_stop_matrix.py::test_codex_quota_stop_…` (3), `::test_codex_loop_breaker_…` (4), `::test_codex_missing_schema_…` (5), `::test_observe_only_mode_writes_no_receipt`; `test_exit_codes.py` | 3/4/5 | receipt file `0600`, `type=agentcap_quota_receipt`, `exit_code`/`reason` match; Free writes none |
| 14 | Codex response timeout (new, 11 Nov live finding) | `CodexTailer.track` + runner | `test_quota.py::test_codex_response_timeout_stops_and_reads_the_interrupted_usage`, `::test_codex_response_timeout_free_only_warns`, `::test_codex_response_timeout_off`, `::test_codex_long_effort_note` | 4 | `response_timeout` alert; usage written on SIGINT is read (`final_read=complete`) |
| 15 | SIGTERM/SIGHUP to AgentCap | runner signal handlers | `test_quota.py::test_sigterm_and_sighup_stop_the_agent_and_write_the_receipt` | child's code | "received; stopping the agent and writing the receipt"; job `done.json`; receipt `reason=terminated` |

## Live status (11 Nov 2026)

Real ChatGPT Plus / Claude Pro accounts, Codex 0.166-0.167.1, Claude Code interactive:

| Run | Result |
| --- | --- |
| Codex 5h 1% (`exec`, no tools) | exit 3, no leftover process |
| Codex weekly 1% (low effort, agentOS MCP tools) | weekly 85 -> 86, exit 3, exact baseline |
| Codex parallel session | warning printed |
| B1: Codex 5h 2% (low effort, MCP tools), commit 01c7937 | exit 3 in 22 s, receipt `final_read=complete`; found the stale-baseline undercount fixed in 01c7937 |
| B2: Codex max effort, `--response-timeout 2m` | cut after 2 min, exit 4 `response_timeout`, no leftover; the interrupted response's usage did not show (5h stayed 3%) |
| B4: Codex Free observe-only, 5h 1% | not stopped, exit 0, "Pro's quota mode would have stopped this job at 1%"; 62-min-old same-window baseline used |
| B3: Codex `approve` x3 mid-run, 5h 1% | 1% -> 1.5% -> 2.25%, third refused, stopped at 3.0% (whole-% step), exit 3 |
| B5: Codex multi-turn with shell tools + `sleep 901 &`, 5h 3% | exit 3, no leftover sleep or codex (Codex's sandbox likely ended the sleep itself) |
| B6: interactive Codex TUI, 5h 2% | exit 3, no leftover; first run left the shell in raw mode, fixed in 72bf25f, re-run clean |
| Claude interactive 5h 2% | 17 -> 19, tool call denied, session stayed open, exit 3 |

Codex quota stopping is done. Still open (each needs Kutay's approval first): Claude approve flow,
weekly window, headless warning, own statusline chain, plugin self-guard, plugin `/agentcap:quota`.

- **Codex sandbox in an agent shell:** shell tool calls fail with
  `bwrap: loopback: Failed RTM_NEWADDR: Operation not permitted`
  (`kernel.apparmor_restrict_unprivileged_userns=1`) and the Codex CLI still exits 0, so a live
  run must inspect the tool output. Tool-using live tests run in Kutay's own terminal; MCP tools
  work without the sandbox. Do not use `-s danger-full-access` for these tests.
- **Claude headless** (`claude -p`) has no quota signal by design (`HEADLESS_CLAUDE_QUOTA_WARNING`);
  only the loop/error breakers apply there.

## Reproduce one path

```bash
cd /home/kutay/agentcap-04
pytest tests/test_stop_matrix.py -k "quota_stop" -v            # 1
pytest tests/test_quota.py -k "claude_denied_at_quota" -v      # 2
pytest tests/test_stop_matrix.py -k "loop_breaker" -v          # 3
pytest tests/test_stop_matrix.py -k "error_breaker" -v         # 4
pytest tests/test_stop_matrix.py -k "escalates" -v             # 5
pytest tests/test_lifeline.py -k "sigkilled" -v                # 6
pytest tests/test_stop_matrix.py -k "no_survivor" -v           # 7
pytest tests/test_quota.py -k "approve" -v                     # 8
pytest tests/test_cc_plugin.py -v                              # 9
pytest tests/test_failsafe.py -k "lock" -v                     # 10
pytest tests/test_stop_matrix.py -k "missing_schema" -v        # 11
pytest tests/test_quota.py -k "api_key_login or no_session_log" -v  # 12
pytest tests/test_stop_matrix.py -k "receipt" -v               # 13
pytest tests/test_quota.py -k "response_timeout" -v            # 14
pytest tests/test_quota.py -k "sigterm" -v                     # 15
```

## Known limits (unchanged by this stage)

- Percent sources are whole integers; a `--quota 3%` stop can land anywhere in
  ~1–3% of real use.
- The receipt measures the **account's** increase, not the job's own spend.
- `final_read=incomplete` means the last response's usage was not written yet;
  the receipt says so instead of presenting the number as final.
- Descendants that leave the process group *before* AgentCap dies are swept as
  subreaper; a process that changes uid or enters another pid namespace is not
  reachable (best-effort cleanup, documented in `procstop.py`).
