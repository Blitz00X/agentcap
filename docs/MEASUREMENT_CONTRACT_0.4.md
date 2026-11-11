# AgentCap 0.4 — measurement contract and protection states

Date: 11 Nov 2026 · Branch: `quota-sources-0.4` · Scope: quota mode for Claude
Code (Pro/Max login) and Codex CLI (ChatGPT login). No push, no release.

This document is the contract every quota surface renders: the statusline,
`agentcap quota`, the `PreToolUse` hook, the runner's terminal output, the
Claude Code plugin and the receipt. If a sentence here and the code disagree,
the code is wrong.

## 1. One reading record

Every quota observation is one `agentcap.quota.Reading`:

| Field | Meaning |
|---|---|
| `source` | `claude` (statusline `rate_limits`) or `codex` (`token_count` rollout) |
| `read_at` | Unix seconds when the source produced this reading |
| `max_age` | How old this source may be and still count as protection |
| `window` | `week` or `5h` |
| `percent` | Account-level used percentage, 0–100 (`used` is a read-only legacy alias) |
| `resolution` | The step the source reports in; `1.0` (whole percent) for both |
| `observed` | `true` for a real observation; a placeholder is never counted |
| `resets_at` | The source's window reset time, when it gives one |

`claude_readings()` and `codex_readings()` are the only producers. The
statusline, the read-only view, the hook, the runner and the receipt all read
this record (`to_dict()` / `from_dict()`); none of them builds a parallel dict
with its own field names. `from_dict()` still accepts the pre-0.4
`{"used": ...}` shape so old sample logs keep importing.

**Freshness.** `read_at` moves only when a genuinely new reading arrives. A
cached `status.json`, a second read of `claude_last.json`, or a statusline
render without `rate_limits` keeps the old `read_at`, so its age keeps growing.

| Source | `max_age` | Why |
|---|---|---|
| Claude statusline | 60 s | `refreshInterval` is 5 s; 12 missed renders is a lost source |
| Codex rollout | 600 s | usage is written when a response ends; 2× the 300 s response timeout |

The first-reading grace is 90 s (`no_data_warn_s`).

**Resolution.** Both sources report whole percent. Every measured number is
printed with `±1-3%`: one source step plus up to one model response that has
not been written yet. No surface prints more decimal places than that, and no
surface presents the number as exact.

## 2. The four states

One function, `protection_state()`, maps `(started_at, now, reading, stopped,
source_lost)` to exactly one state. `state_text()` is the only source of the
user-visible wording.

| Condition | Internal state | User text | Exit |
|---|---|---|---|
| No reading yet, less than 90 s since start | `starting` | `waiting for the first reading` | 0 |
| Reading exists and `now - read_at <= max_age` | `protected` | `protection active` | 0 |
| Reading older than `max_age` | `stale` | `data stale (protection not verified)` | 0 (job continues; strict Codex guard may exit 5) |
| No reading after the 90 s grace | `stale` | `data stale (protection not verified)` | 0 (strict Codex `exec` exits 5) |
| Source says it cannot provide this window (API-key login, changed schema) | `stale` | `data stale (protection not verified)` | Codex strict: 5; Claude: 0 with the warning |
| A quota/loop/response-timeout stop happened | `stopped` | `stopped` | 3 / 4 |
| A guard failure stopped the job | `stale` | `data stale (protection not verified)` | 5 |

Rules:

- No reading is not a zero reading. Before the first reading the status is
  `waiting for the first reading`, never `protection active`.
- A stale or missing source can never render as `protection active`, at any
  tier, in any surface.
- Strict Codex failures keep the existing fail-closed exit 5
  (`guard_failed`); the contract only changes what is *said*, not the guard.
- Free/observe-only renders `watch only (Pro stops at the limit)` instead of
  `protection active`, because nothing will stop there.

## 3. Startup uncertainty

Before the first successful reading:

- the runner prints `quota mode (...): waiting for the first reading; will stop
  this job at N% ...`;
- the plugin prints `waiting for the first reading`;
- no receipt file is written and no `quota_receipt` alert is sent. A run that
  measured nothing may not leave a document that looks like a measurement or a
  stop. Guard failures still write a receipt (`observed=false`, `share_pct=null`,
  `measure="none"`, `state="stale"`) because the failure itself is the fact.

## 4. Window reset

A drop in `used_percent` is classified as:

| Evidence | Classification | Effect |
|---|---|---|
| `resets_at` moved forward by more than 60 s | new window | carry the closed window's usage, clear the baseline to 0, `resets_seen += 1` |
| `resets_at` unknown and the value dropped by more than 0.5 | new window | same |
| Same reset time, value dropped | whole-percent jitter | keep the high-water mark, no reset |
| Any other rise | normal usage | share grows |

On a reset the baseline is cleared, the new window starts from zero, and the
closed window's measured usage is carried once. The reset itself adds nothing:
`share = carried + max(0, used - baseline)` is never negative. The runner prints
`the weekly/5-hour window reset; ... the reset itself adds no usage`, the
plugin appends `— window reset (not counted as usage)` for one render, and the
receipt records `window_resets` and the note `(not counted as usage)`.

## 5. Lost source

| Lost thing | Detection | State | Exit |
|---|---|---|---|
| Claude statusline stops rendering | no new `read_at` for 60 s | `stale` | 0 |
| Statusline renders but has no `rate_limits` | `has_rate_limits=false` after an API call | `stale` | 0 |
| Codex rollout unreadable | 3 consecutive read errors | `stale` + guard message | 5 (strict) |
| Codex `token_count` with no `rate_limits` | 2 token counts, 0 readings | `stale` + guard message | 5 (strict) |
| `codex exec` writes no rollout | 90 s after start | `stale` + guard message | 5 (strict) |

## 6. One text on every surface

| Surface | Renders |
|---|---|
| Statusline | `agentcap: job 5.1% / 5% (week) ±1-3% — protection active` (or `watch only`) |
| `agentcap quota` | `claude week: 41.0% used ±1-3%, resets in ... (seen 5s ago) — protection active` |
| Hook deny | `... used 5.1% of the weekly subscription quota ±1-3% ...` plus the stale note when the last reading is old |
| Plugin `/agentcap:status` | `agentcap: session 5.1% / 5% (weekly) ±1-3% — protection active` |
| Runner summary | `the account's weekly quota rose 5.1% ±1-3% while this job ran` |
| Receipt | `share_pct` (or `null`), `state`, `state_text`, `read_at`, `reading_age_s`, `max_age_s`, `source`, `resolution_pct`, `uncertainty_pct` |

## 7. Exit table

| Situation | Exit |
|---|---|
| Job exits under the limit, protection fresh | agent's code |
| Quota hit, Codex SIGINT or Claude hook deny | 3 |
| Loop/error breaker | 4 |
| Codex response timeout | 4 |
| Guard failure, strict schema loss, unreadable log | 5 |
| Observe-only (Free), any outcome | agent's code |
| No reading and a normal exit | agent's code, **no receipt** |

## 8. Tests

`tests/test_measurement_contract.py`:

- `test_reading_record_fields_table` — every source, all seven fields, roundtrip, legacy shape.
- `test_protection_state_table` — the six condition rows of §2.
- `test_stale_reading_is_not_refreshed_by_reading_it` — fake clock: fresh at +30 s, stale at +61 s, `read_at` never moves on re-read, moves on a new reading.
- `test_startup_writes_no_receipt_and_claims_no_protection` — no receipt, no "protection active".
- `test_lost_statusline_goes_stale_and_the_receipt_says_so` — real fake-CLI run, stale receipt.
- `test_codex_source_loss_still_fails_closed_at_exit_5` — strict exit 5, `share_pct=null`, never protected.
- `test_reset_clears_the_baseline_and_is_not_consumption` — table: jitter vs reset, baseline 0, never negative.
- `test_codex_reset_is_shown_and_not_counted_as_usage` — integration: `share=3.0`, reset message, receipt.
- `test_resolution_is_printed_without_fake_precision` — `±1-3%` on all text surfaces, receipt fields.
- `test_four_states_come_from_one_function_and_one_text` — plugin/status/summary/receipt share `STATE_TEXT`.

Run:

```bash
cd /home/kutay/agentcap-04
/home/kutay/agentcap-ha-venv/bin/python -m pytest tests/test_measurement_contract.py -v
```

## 9. Known limits (unchanged)

- Percent is account-level: parallel sessions count against the job.
- Codex writes usage when a response ends; the last response can overshoot by
  up to one request, which is why the range is `±1-3%`, not `±1%`.
- The receipt measures the account's increase, not the job's own spend.
- Claude headless (`claude -p`) exposes no quota signal; the state there is
  `stale`, and the loop/error breakers are the only active guards.
