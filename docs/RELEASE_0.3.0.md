# AgentCap 0.3.0 — release evidence (2026-11-04)

Context: this run continued the `hardening-0.3` branch from `b7ae12a` after the
Claude baseline in `~/Downloads/agentcap-0.3-kanit/`. No commit, push, merge,
tag, PyPI upload, deploy or share was performed.

## S2 — wheel, sdist, clean install

```bash
uv build --out-dir /tmp/opencode/agentcap-dist
```

Result: `agentcap_cli-0.2.2-py3-none-any.whl` and `agentcap_cli-0.2.2.tar.gz`
built. The wheel contains `agentcap/data/pricing.json`; the sdist contains the
package, tests and devtools.

Clean installs used a fresh venv, an empty `HOME` and `env -i`:

```bash
env -i PATH=... HOME=/tmp/opencode/agentcap-s2/home \
  uv pip install .../agentcap_cli-0.2.2-py3-none-any.whl
env -i ... agentcap --version        # agentcap 0.2.2
env -i ... agentcap demo             # exit 3, cap_exceeded, 43 requests, $0.0500
```

The sdist was installed the same way in a second fresh venv: `agentcap 0.2.2`,
`agentcap demo` exit 3. Both paths ran the bundled fake upstream; no provider
key and no model call.

## S3 — real harness binaries, local fake upstream

All runs used `AGENTCAP_*_UPSTREAM=http://127.0.0.1:9310`, dummy API keys and
an isolated `HOME`/config directory. No auth/OAuth file was read and no real
model was called.

| Harness | Command shape | Result |
| --- | --- | --- |
| Claude Code 2.1.318 | `agentcap run --cap 0.50 -- claude -p ...` | 1 request, $0.0845, exit 0 |
| Codex CLI 0.166.0 | `agentcap run --cap 0.50 -- codex exec --skip-git-repo-check ...` | cap hit, exit 3, $0.50 (fake upstream returned no stream usage, so the full reservation was charged) |
| opencode 2.2.4, `$` mode | `agentcap run --cap 0.50 -- opencode run -m agentcap/gpt-4o-mini ...` | 2 requests, $0.0033, exit 0 |
| opencode 2.2.4, `--tokens` | `agentcap run --tokens 100000 -- opencode run --format json -m fake/gpt-4o-mini ...` | 6,900 tokens parsed, exit 0 |
| agy 1.5.0, `$` mode | `agentcap run --cap 0.50 -- agy --model gemini-3.8-flash-low -p ...` | 2 requests (1 refused), $0.0121, exit 0 |
| agy 1.5.0, `--tokens` | `agentcap run --tokens 100000 -- agy ...` | 6,964 tokens parsed, exit 0 |

Free tier only observes harness caps (`stopping is Pro`); enforcement itself is
covered by the deterministic fake-harness tests, not by these smoke runs.

### Defect found and fixed

`--tokens`/`--log-cap` with opencode failed with:

> Managed service port 49374 on 127.0.0.1 is already in use ... configure
> another port

`src/agentcap/adapters.py` already forces `--standalone` in `$` mode, but the
harness adapter did not. `OpenCodeAdapter.argv` now inserts `--standalone`
(once) before `--format json`; the real opencode 2.2.4 token-mode run then
succeeded. Test: `tests/test_harness.py::test_opencode_argv_forces_json_and_standalone`.

## S4 — security boundaries

### Loopback only

```bash
agentcap serve --port 9411 --cap 1
ss -ltnp | grep 9411
```

Result: `LISTEN 127.0.0.1:9411`; `GET /_agentcap/status` returned 200. The
proxy never binds a public interface.

### No secrets in state or logs

A child sent `Authorization: Bearer sk-SECRET-MARKER-123` and a prompt
containing `SECRET-PROMPT-MARKER-456` through the proxy. After the run:

```bash
grep -rIl "SECRET-MARKER\|SECRET-PROMPT" /tmp/opencode/s4/home
```

Result: no match. The ledger stored only run id, provider, model, reservation
and status. State modes are now `0700` for directories and `0600` for files.

### Self-approve bypass variants

`hook_main` now normalizes shell quotes, backslash escapes and `$IFS` before
matching, so these are denied and leave `job.json` untouched:

- `agentcap 'approve'`, `agentcap "approve"`, `agentcap a''pprove`
- `agentcap \approve`, `agentcap${IFS}approve`
- `env -u CLAUDECODE -u AGENTCAP_GUARDED /home/k/.local/bin/agentcap 'approve'`
- `python -m agentcap approve`

Tests: `tests/test_self_approve.py::test_hook_denies_obfuscated_self_approve`.
Hard limit unchanged: the agent runs as the same OS user and can still edit
state or kill AgentCap; use a separate user/container against a hostile agent.

### State permissions

New `src/agentcap/private.py` centralizes `0700` directories and `0600` files.
It is used by quota state (`job.json`, `status.json`, settings, breaker files),
the job lock, the ledger, receipts, history and the budget file. Tests assert
the modes in `test_quota.py`, `test_failsafe.py`, `test_harness.py`,
`test_ledger.py`, `test_history.py` and `test_budget.py`.

F6 (fixed on `hardening-0.3`): `ensure_private_dir` no longer chmods an existing
directory. Only directories it creates, and AgentCap's own default
directories, become `0700`; the parent of `$AGENTCAP_DB`, a receipt dir or a
project dir keeps its mode. Tests: `tests/test_private_dirs.py`.

### Parent-death lifeline (T2)

A Linux child is spawned through `agentcap/_lifeline_exec.py`, which arms
`PR_SET_PDEATHSIG` and then `exec`s the real command. AgentCap also starts
`agentcap/_lifeline_watch.py` (stdlib only, `python -I`, its own session) on a
pipe only AgentCap can write. If AgentCap is SIGKILLed or OOM-killed, the
kernel kills the direct child, and the watchdog sees EOF without `done` and
SIGKILLs the agent's process group.

The watchdog came out of the independent review below: the parent-death signal
alone only reaches the process AgentCap started. Codex's npm wrapper
(`bin/codex.js`) falls back to spawning the native binary as a **child** on
Node without `process.execve` (this machine: Node 18.19.1 and 20.19.6), so the
real agent, like any tool process in the agent's group, kept running after
AgentCap's SIGKILL.

`lifeline.spawn` sends an exec failure back over a close-on-exec pipe and
raises the same `OSError` subclass Popen raised, so a missing or
non-executable command is still "could not start the command" (exit 127)
instead of a run with a "no model requests observed" warning.

Tests: `tests/test_lifeline.py` — real parent SIGKILL; a SIGKILLed real CLI in
`--cap`, `--tokens` and `--quota` mode whose agent's same-group tool process
must die; a disarmed watchdog that must not kill; the error pipe must not leak
into the agent; missing command via the CLI. Missing/non-executable command
tests also in `test_runner.py`, `test_harness.py` and `test_quota.py`.

### T3 — setsid/daemonized descendants

Linux AgentCap sets `PR_SET_CHILD_SUBREAPER` for the duration of a run, so
orphaned descendants (including a child that calls `setsid` or double-forks)
reparent to AgentCap instead of init. `ProcessTreeGuard` snapshots existing
children before the run, walks `/proc` PPid trees, verifies each PID against
its start time (no recycled-PID kills), and sweeps new descendants with
SIGTERM then SIGKILL after a normal exit or a stop. The `(pid, start)` identity
is held through TERM, the wait and KILL; a PID reused after its process exits
is never signalled, including in later sweep rounds. Parent, state and start
time come from one `/proc/<pid>/stat` read, so they always describe the same
process. It restores the previous subreaper setting. Children that existed
before the run, and anything they start, are never targeted.

Adopted helpers are also reaped: as subreaper AgentCap inherits the agent's
orphaned background jobs, and an exited one stays a zombie in the agent's
process group until its new parent reaps it. A zombie still answers
`killpg(pgid, 0)`, so before the review fix every run whose agent left a
background job sat out `reap_group`'s two 2 s waits (measured 5.62 s vs 1.47 s
on `b7ae12a`), `stop_tree` could escalate to SIGKILL after 5+3+2 s, and the
harness printed "killed leftover processes" for a job that had already
finished. `waitpid(-pgid, WNOHANG)` after the leader is reaped fixes all three
(re-measured 1.55 s).

Real CLI evidence:

```text
$ agentcap run --cap 1 -- python agent.py   # agent.py spawns setsid sleep 60
agentcap: killed 1 process(es) that left the agent's process group
  (setsid/daemonized helpers).
exit code: 0
$ test -e /proc/<escaped-pid>  # false
```

A stop path (`timeout --signal=INT`) produced the same line and exit 130; the
escaped PID was gone. Tests: `test_procstop.py` (adopted child after leader
exit, while the leader is alive, and baseline-child protection),
`test_harness.py`, `test_runner.py` and `test_quota.py` integration cases.

**Honest limit:** if AgentCap itself receives SIGKILL, the direct child dies via
`PR_SET_PDEATHSIG` and the watchdog kills the agent's process group, but a
process that had already left the group (`setsid`/daemonized) before AgentCap
died can survive; the dead process cannot sweep its tree, and a same-user agent
can kill the watchdog. Closing that last case needs a cgroup/pid-namespace
sandbox or a container (or a separate OS user). Non-Linux platforms keep
group-only cleanup and have no lifeline. This is recorded in the README rather
than claimed as solved.

### Other checks

- Upstream overrides accept `https://` or loopback `http://` only; remote
  plaintext is refused and printed URLs have userinfo/query stripped.
- Websocket upgrades are rejected (405) instead of being forwarded unmetered.
- Query strings (`?key=...`) are forwarded but never written to state.

## S5 — payment sandbox

Read-only reachability checks with dummy values:

```bash
curl -X POST https://sandbox-api.polar.sh/v1/customer-portal/license-keys/validate \
  -d '{"key":"dummy-not-a-key","organization_id":"<valid v4 uuid>"}'
```

- sandbox DNS resolves; endpoint answers `422` for a malformed UUID and `404`
  for a valid-shaped dummy organization (organization not found).
- CLI activation against the sandbox with a dummy key exits `1` with
  "License key not found or no longer valid", and writes no license file.

**Blocker:** a successful sandbox activation needs a Polar sandbox
organization, product and licence key (or token). Those are external account
resources; none were created and no real key was used.

## S6 — version, changelog, release artifacts

- `pyproject.toml`, `src/agentcap/__init__.py`, `.claude-plugin/plugin.json`
  and `CHANGELOG.md` now agree on **0.3.0**.
- `tests/test_release.py` fails if these drift or if `Unreleased` returns.
- README corrections: ledger path is `~/.local/state/...`; proxy-mode stop
  (`SIGTERM`→`SIGKILL`) is now explicitly separated from quota/harness
  (`SIGINT`→`SIGTERM`→`SIGKILL`).
- `agentcap approve` wording: after a loop/error stop it names the cleared
  breaker; at 100% it says the quota no longer stops the job instead of
  "150% of the quota" (both the CLI job path and `scripts/agentcap_cc.py`).

Build and clean install of 0.3.0 (rebuilt after the independent review; the
earlier `f525fc47…`/`ef2cd721…` artifacts are superseded):

```bash
uv build --offline --out-dir /tmp/opus-kalan/dist
# agentcap_cli-0.3.0-py3-none-any.whl
# agentcap_cli-0.3.0.tar.gz
```

SHA256:

```text
d665b550a8207f03b098853513b8059ee835eb2be578455d92f7e68ba7d93e65  agentcap_cli-0.3.0-py3-none-any.whl
44fb93105053542e082f47b17e203b4d77dc661a59421b5b0eefcf3fbbcf7589  agentcap_cli-0.3.0.tar.gz
```

The wheel contains `agentcap/_lifeline_exec.py`, `agentcap/_lifeline_watch.py`,
`agentcap/lifeline.py`, `agentcap/private.py` and `agentcap/data/pricing.json`.

Fresh venv + empty HOME + `env -i`, once from the wheel and once from the
sdist (dependencies downloaded from PyPI; nothing uploaded): `agentcap 0.3.0`;
`agentcap demo` exit 3 (`cap_exceeded`); `agentcap run --cap 1 -- <missing>`
exit 127 with "could not start the command (No such file or directory)" and no
`lifeline:` or "no model requests" line; `kill -9` of a running `agentcap run`
took the agent's same-group tool process with it; state under
`~/.local/state/agentcap` is `0700/0600`.

Full suite (isolated `HOME`, `env -i`):

```bash
python -m pytest -o addopts="" -q -p no:cacheprovider -rs \
  --junitxml=/tmp/opus-kalan/final-junit.xml
```

Result: **721 passed, 2 skipped in 93.64 s** (JUnit: 723 tests, 0 failures,
0 errors). The two skips are the opt-in
live log-contract tests (`tests/test_log_contract_live.py:30,43`), which make a
real model call and are out of scope for this run.

## Independent review of the T2/T3 changes (Opus, 2026-11-04 evening)

Scope: procstop PID reuse, process cleanup, the lifeline, the same-user
boundary. Every fix went red first, then green; no real model call, no paid
API, no auth file read. Details and command output:
`~/Downloads/agentcap-0.3-kanit/opus-kalan-sonuc.md`.

| # | Defect | Fix | Regression tests |
| --- | --- | --- | --- |
| F0 | Adopted zombies kept the agent's group "alive": +4 s on every run whose agent left a background job, stop escalation to SIGKILL, false "killed leftover processes" | `_reap_group_zombies` (`waitpid(-pgid, WNOHANG)` after the leader is reaped) in `reap_group`, `_wait_group_gone`, `_wait_gone`, `stop_tree` | 3 in `test_procstop.py`, 1 runner, 1 harness |
| F7 | The parent-death signal reached only the direct child; a launcher's real agent (Codex npm wrapper on Node 18/20) and same-group tool processes survived AgentCap's SIGKILL | `GroupWatchdog` + `_lifeline_watch.py` in all three run paths | SIGKILLed real CLI in `--cap`, `--tokens`, `--quota` mode; disarmed watchdog |
| F1 | A missing command printed `lifeline: cannot exec` plus "no model requests observed" and API-key hints | `lifeline.spawn`: exec errno over a close-on-exec pipe, re-raised as Popen's `OSError` | runner (ENOENT, EACCES), harness, quota, CLI message, no fd leak |
| F3 | Parent and start time came from two `/proc` reads | one `stat` read | `test_read_proc_takes_parent_and_start_time_from_one_read` |
| F4 | New children of a pre-run (baseline) child were swept | baseline subtrees excluded | `test_sweep_spares_new_children_of_a_baseline_process` |
| F2 | The shim ran with its package directory and the user's `PYTHONPATH` on `sys.path` | `python -I` | `test_shim_only_sees_the_standard_library` |

Not changed (decisions or documented limits): `ensure_private_dir` chmodding an
existing user-chosen parent directory to 0700 was left open here and fixed
later as F6 (see State permissions); a descendant that
AgentCap may not signal (root-owned) cost up to 4 × (2 s + 2 s) of sweep
waits, fixed later as F5 (below); the self-approve hook is bypassed by `$()`, `${x}`, variable
indirection or `eval`, and a same-user agent can kill or disarm the watchdog —
all inside the documented same-user limit.

F5 (fixed on `hardening-0.3`): a descendant whose signal fails with
`PermissionError` (a root-owned `sudo -b` child, a setuid program) is tried
once per `(pid, start)` identity and then neither waited for nor signalled
again, so it no longer adds up to ~16 s to the exit. If it is still running
when the sweep ends, `--cap`, `--tokens` and `--quota` runs print
`agentcap: could not stop N process(es) (permission denied): pid ...` and it
is not counted as killed; exit codes are unchanged. The denial is tied to the
identity, so a later descendant reusing that PID is handled normally.
`reap_group` returns at once when `killpg` fails with EPERM (no member could be
signalled). Still not handled: such a process is left running (AgentCap does
not escalate privileges), and `stop_tree`'s group ladder still waits its
timeouts if only an unsignallable member keeps the group alive. Tests: the F5
block in `tests/test_procstop.py` (mocked `/proc`, `os.kill` and clock) and one
warning test each in `test_runner.py`, `test_harness.py`, `test_quota.py`.

F8 (fixed on `hardening-0.3`): a real Codex CLI 0.166 run against OpenRouter
with a key the upstream rejected (HTTP 401 `User not found.`) was stopped as
`loop_detected` (exit 4) after Codex's "Reconnecting..." resends: the
identical resends hit the exact-hash loop detector, because only
408/429/5xx/529 answers were forgiven. Now 401, 402, 403 and Gemini's 400
`API_KEY_INVALID` are detected once, on the response returned by both the
streaming and non-streaming senders, for the OpenAI (chat/Responses),
Anthropic and Gemini routes. They are forgiven like retryable statuses and
printed as `upstream rejected the request (HTTP 401: User not found.) — check
the API key for openrouter.ai; nothing was spent.` Only a JSON
`error.message` is shown: control characters removed, key-shaped text
(`sk-…`, `AIza…`, bearer tokens, `key=…`, long digit-bearing tokens) redacted,
120 characters at most; headers are never read. Three rejections in a row
(no other answer between them) stop the run on Free and Pro with reason
`upstream_auth_failed` and exit 2, AgentCap's existing "setup problem, fix and
rerun" code. No new exit code: 5 means AgentCap itself could not guard, which
would blame AgentCap for a bad key. If the agent exits first, the reason is
still `upstream_auth_failed` and its exit code is kept. Real loops on 200
answers still stop at the 4th step (exit 4). Tests:
`tests/test_proxy_auth.py` (38, including the local fake Anthropic upstream;
`agentcap serve` reports only and never marks itself stopped).
Not verified against a live provider. A 403 that is not about credentials
(e.g. a region block) is treated the same way; the agent cannot make progress
in that case either.

### Known limit: adopted zombies wait for the end of the run (evaluated 2026-11-05)

As subreaper AgentCap adopts the agent's orphaned helpers; once one exits it
is AgentCap's zombie and only AgentCap can reap it. None is reaped while the
run is going on: helpers still in the agent's process group are reaped by
`_reap_group_zombies` once the leader's `Popen` has reaped the leader (F0), the
rest by the end-of-run sweep. A long run whose agent keeps leaving short-lived
detached helpers therefore accumulates zombies. Each holds a PID, counts
against the user's `ulimit -u` (RLIMIT_NPROC) and its cgroup's `pids.max`, and
still answers `kill(pid, 0)` until the run ends; then all are reaped.
Measured: 40 unreaped exited children raised the cgroup's `pids.current` from
29 to 69, and with `ulimit -u` set 20 above the user's task count `fork`
failed with EAGAIN after 19 of them.

Reaping them during the run was evaluated and is **not provably safe**:

- AgentCap's process has children whose exit status it must not take. During
  a run: the agent (`lifeline.spawn`, its own session; reaped by
  `Popen.poll/wait` in the monitor loop, `_terminate` and `stop_tree`), the
  group watchdog (its own session; `GroupWatchdog.disarm`) and, in harness
  quota mode, `agy -p /quota` (`subprocess.run` in AgentCap's own group,
  synchronous in the monitor loop, every 20 s). The proxy, alert and
  log-reader threads start nothing; the Claude statusline/hook helpers are
  children of the agent, not of AgentCap; `agentcap.ops` is not part of a run.
  An embedding program (the test suite runs `Runner` in-process) and libraries
  can add any child at any time.
- Taking such a child's status is silent. In CPython 3.11.15, 3.12.3 and
  3.14.4 (sources and a run on this machine; 3.13 has the same code)
  `Popen._internal_poll` maps ECHILD to `returncode = 0` and `_try_wait` maps
  `ChildProcessError` to status 0: a child that exited 7 reported 0 from both
  `poll()` and `wait()` after another `waitpid` took it. asyncio's child
  watchers report 255. A blanket `waitpid(-1)` or SIGCHLD reaper does this to
  every child.
- A per-PID reaper has to tell "adopted" from "forked by this process", and in
  the reported case it cannot. `/proc/<pid>/stat` holds only the current
  parent. Measured under `PR_SET_CHILD_SUBREAPER`: an adopted `setsid` helper
  whose shell had exited and a child started with
  `subprocess.Popen(..., start_new_session=True)` were both zombies with PPid =
  AgentCap, pgrp = session = their own PID and the same start tick (a zombie's
  `/proc/<pid>/environ` is empty). A registry of AgentCap's own spawns cannot
  be complete: `os.register_at_fork` sees `os.fork()` but not
  `subprocess.Popen` (`_posixsubprocess.fork_exec`) or `os.posix_spawn`
  (verified), and `multiprocessing`'s spawn, forkserver and resource tracker
  call `_posixsubprocess.fork_exec` directly.
- What *is* provable, and was not shipped: a process forked by any thread of
  AgentCap's process stays in AgentCap's session or, after `setsid()`, in a
  session it leads. A zombie child whose session ID is neither AgentCap's nor
  its own PID (any member of the agent's session, its process group included,
  and any non-leader of another session: a double-fork daemon, a background
  job of a detached shell) therefore cannot be AgentCap's own child, and
  reaping exactly those needs no registry. It still leaves the `setsid` helper
  itself, a session leader, for the end-of-run sweep, which is the reported
  case, so it is a candidate for a later release rather than part of 0.3.0.

F0 re-checked: `_reap_group_zombies` (`waitpid(-pgid, WNOHANG)`) reaps only
AgentCap's children in the agent's process group. A group never spans two
sessions; the agent leads its own session, the watchdog leads another (now
asserted by a test), and any other child of AgentCap's process is in
AgentCap's session or leads a new one, so none of them can be in that group.
Besides adopted helpers it can only take the leader's status, and every caller
runs it after the leader's `Popen` reaped the leader, except `Runner.run`'s
cleanup when the leader outlived the stop (the SIGKILL wait expired) or an
exception ended the run; there the exit code is already decided and nothing
reads the leader's status afterwards. Theoretical residual: if the agent's
group has emptied and the kernel gives its number to a new group of
AgentCap's within one 50 ms poll, `waitpid(-pgid)` (and `killpg`) would reach
that group; that needs a full PID wrap (pid_max 4194304 here) in that window.

Tests: `tests/test_child_status.py` (`--cap`, `--tokens` and `--quota` runs:
the agent leaves an in-group background job, a `setsid` helper and a
double-fork daemon behind; children started during the run in AgentCap's
group, in a new session and in a new group keep exit codes 7, 9 and 11 after
1.5 s of monitor ticks; the agent keeps exit code 5; nothing adopted outlives
the run) and `tests/test_procstop.py::test_group_reaping_takes_only_the_agents_adopted_zombie`.
RED was checked by injecting reapers into the three monitor loops from a
scratch pytest plugin (not committed): blanket `waitpid(-1)`, "every zombie
child except the agent and the watchdog" and "every zombie child outside
AgentCap's session" each failed all three modes, having taken the statuses 7,
9 and 11; the session-ID rule above passed and reaped the background job and
the daemon during the run. Replacing F0 with `waitpid(-1)` fails the F0 test.

## Remaining human steps

1. Review the uncommitted diff (no commit was made). The commit must include
   the untracked files `src/agentcap/_lifeline_exec.py`,
   `src/agentcap/_lifeline_watch.py`, `src/agentcap/lifeline.py`,
   `src/agentcap/private.py`, `tests/test_lifeline.py`, `tests/test_release.py`
   and this document.
2. Decide whether the 0.3.0 version bump and CHANGELOG are the release notes.
3. Commit, merge to `main`, tag `v0.3.0`, push, upload to PyPI, update the
   landing/plugin listings — all require Kutay's approval.
4. Run the opt-in live contract test (`AGENTCAP_LIVE_CONTRACT=1`) with a model
   budget if a live check is required; it was deliberately not run here.
5. Complete a Polar sandbox activation with sandbox org/product/key if a real
   payment-path E2E is required.
6. Residual hardening: a process that left the agent's group (`setsid`/
   daemonized) before AgentCap itself is SIGKILLed can still escape; a
   cgroup/container or separate OS user is required for that threat model
   (documented in the README).
7. Optional: decide whether to ship the session-ID reaper described under
   "Known limit: adopted zombies wait for the end of the run". It would reap
   the agent's in-session helpers and double-fork daemons during a run;
   `setsid` helpers would still wait for the end of the run.
