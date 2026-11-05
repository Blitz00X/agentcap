# AgentCap operations tracking (`python -m agentcap.ops`)

A persistent record of what a coordinator dispatched, what each model run
really did, which independent tests and commits verified it, and a notification
queue that survives restarts and sender outages. It is local and model-agnostic:
it never calls a model or a paid API, and it never holds a notification secret.

## Concepts

| Thing | Meaning |
|---|---|
| task | A unit of work the coordinator dispatches (`--id`, title, git worktree, optional dependencies). Re-adding an identical task is a no-op; redefining it is refused. |
| run | One execution of a model command for a task. Unique 32-hex id, own evidence directory. A run records what the *process* did, not whether the task succeeded. |
| verification | The coordinator's own test command plus commit checks for a completed run. Only a passed verification makes the task `success`. |
| commit | A meaningful commit git showed after a run (or during recovery/verification), queued once per commit hash. |
| active model | The model explicitly assigned to a worktree. `run --model` must match it; switching needs `model set` and is refused while a run is active. |
| signal | A quota/permission event reported during a run (Claude Code hook, wrapper, `ops signal`). |
| notification | Outbox row, one per logical event (`run_finished`, `commit`, `task_verified`, `permission_needed`, `quota_reached`, `probe`), deduplicated by key, never deleted. |

State lives in `$AGENTCAP_OPS_DIR` (default `~/.local/state/agentcap/ops`, mode 0700):
`ops.sqlite3` (0600), `runs/<run-id>/{stdout.log,stderr.log,evidence.json}`,
`verifications/<id>/{stdout.log,stderr.log,verification.json}`, `locks/`,
`hook-backups/`. The sender's own settings and tokens are never stored there.

## Run and task states

| Run state | When |
|---|---|
| `completed` | the process exited 0 and no quota/permission denial was recorded; the task becomes `unverified` |
| `quota` | a mapped quota exit code, a quota signal during a non-zero run, or an opt-in quota pattern on the output tail |
| `permission` | a mapped permission exit code, a permission signal during a non-zero run, an opt-in permission pattern, or Claude Code's own `permission_denials` result even when it exits 0 |
| `failure` | any other non-zero exit, a kill by signal, a timeout, or a command that could not start |
| `interrupted` | the runner died before finishing; set by recovery. The exit code is kept only if the kernel had already reported it, never invented |

An answered permission prompt does not turn a clean exit into a failure: a
signal explains a non-zero run, while a run that exits 0 stays `completed`
(Claude Code's explicit `permission_denials` list is the exception above).

| Task state | When |
|---|---|
| `queued` | recorded, not started yet (or waiting for a dependency) |
| `running` | its last run is still running |
| `unverified` | its last run completed and waits for `task verify` |
| `success` | a verification passed (the database refuses any other way) |
| `failure` | the run failed, or its verification failed |
| `quota`, `permission`, `interrupted` | the last run ended in that state |

A task that is `unverified` or `success` is not dispatched again without
`--rerun`; a failed or quota/permission task may be rerun normally.

The exit code stored and returned is the one the kernel reported
(`Popen.wait`): `ops run` exits with it (128+N when signal N killed the model,
127/126 when it could not be started). Evidence (`evidence.json`, created once,
its sha256 in the store and in the notification) holds argv, timings, exit
status, log sizes and hashes, signals, and git `HEAD` before/after plus the
commits `git rev-list` reports between them: a model claiming a commit in its
output changes nothing.

## Commit notifications

After the model and its whole process group are gone, the runner asks git (read
only) which commits appeared. A *meaningful* commit changes files: its tree
differs from its first parent (an `--allow-empty` commit or an empty merge is
not meaningful). Each meaningful commit is

- stored once by full hash in `commits` with task, run, repo, branch and source
  (`run`, `recovery` or `verification`);
- queued once as a `commit` notification (`dedup_key = commit:<sha>`), after the
  run's own `run_finished` row, in the same transaction;
- only looked at after the model has exited, so a commit made mid-run can never
  start anything while that model still works.

Seeing the same commit again (another run, a later verification) only bumps the
notification's `duplicates` count. `run_finished` and `commit` payloads carry
the task, run, model, repo/branch, exit code, evidence paths and the commit
hash; the message is a one-line `COMMIT <sha> on <branch>`.

## Coordinator workflow

```sh
export AGENTCAP_OPS_DIR=~/.local/state/agentcap/ops        # optional
python -m agentcap.ops task add --id T10 --title "harden X" --worktree /path/to/worktree
python -m agentcap.ops model set --worktree /path/to/worktree --model claude-opus-5-5
python -m agentcap.ops hooks install --worktree /path/to/worktree   # optional, Claude Code only
python -m agentcap.ops run --task T10 --model claude-opus-5-5 --timeout 7200 \
    --stdin-file prompt.txt -- claude -p --output-format json
python -m agentcap.ops task verify T10 -- python -m pytest tests/test_x.py
python -m agentcap.ops deliver --config /run/user/$UID/agentcap-ops-endpoint.json
python -m agentcap.ops status
```

`run` refuses (exit 2) a missing task, a task that is running (a duplicate
dispatch, even while its runner holds the worktree), a task that is unverified
or already succeeded (unless `--rerun`), or a model that is not the worktree's
active one; it refuses with exit 75 when another task's runner holds the
worktree, when processes of an earlier run are still alive there, or when the
model process itself tries to start a second tracked job.

`task verify` refuses inside a tracked model run and while a model owns the
worktree. It runs the test command in the worktree, with its own process group
and no `AGENTCAP_OPS_*` variables, and records `verification.json` plus hashed
logs. A pass needs exit 0, no failed/error count in a pytest summary line, at
least one passed test when such a line exists, and (unless the task was added
with `--no-commit`) at least one meaningful commit on the current branch that
is new since the task's first run started, with tracked files clean. Commits can
be named explicitly with `--commit SHA`; by default the meaningful commits
already recorded for the task are checked.

## Notification sender (runtime configuration)

`deliver` is the only command that reads the sender config, and only at runtime:
`--config PATH`, `--config-fd N` (e.g. `--config-fd 3 3<endpoint.json`; the fd is
closed after reading) or `$AGENTCAP_OPS_CONFIG`. A config file must be a regular
file (not a symlink), owned by the user, mode 0600, and outside every git work
tree. `run` never reads it and strips every `AGENTCAP_OPS_*` variable from the
model's environment, so keep the config out of the model's reach (for example
`/run/user/$UID/`) and run `deliver` from the coordinator, not from inside a
model session.

AgentCap never takes an endpoint URL, header, chat id or token. The coordinator's
existing deterministic sender keeps those in its own private settings; AgentCap
only names the sender program and *reference names* it resolves itself:

```json
{"command": ["/home/me/bin/notify", "--endpoint", "{endpoint}",
             "--bearer", "{bearer}", "--text", "{message}"],
 "endpoint": "ops-alerts", "bearer": "ops-alerts-token",
 "format": "text", "timeout_s": 10, "max_attempts": 8}
```

`command[0]` must be an absolute path to an executable regular file owned by the
user (or root) and not writable by group or others; there is no shell and no
`PATH` lookup. `url`, `headers`, `chat_id`, token-like keys and the former
`telegram` format are refused, as are command arguments carrying a URL, a
`Bearer` value or `token=`/`password=`-style credentials. `format` is `text` (the
one-line message on stdin, followed by a newline) or `json` (the full payload,
the default). Arguments that are exactly `{message}`, `{kind}`, `{event}`,
`{event_id}`, `{endpoint}` or `{bearer}` are replaced by that value (whole
arguments only; the last two are the configured reference names, never
secrets). The sender also gets `AGENTCAP_OPS_EVENT`, the stable
`AGENTCAP_OPS_EVENT_ID` (for the sender's own deduplication) and, when
configured, `AGENTCAP_OPS_ENDPOINT_REF` / `AGENTCAP_OPS_BEARER_REF`; every other
`AGENTCAP_OPS_*` variable is removed.

Exit 0 means delivered. Any other exit, a kill, a timeout (its process group is
SIGKILLed after `timeout_s`) or a program that cannot start is a failed attempt
with exponential backoff (30 s, 60 s, ... up to 1 h); after `max_attempts` it
becomes `failed`: still stored, shown by `status`, requeued with `notify retry`.
Its stdout/stderr are discarded and attempts record only `exit N` (or the error
class) and a fingerprint of the sender call, so nothing the sender prints
reaches the store or a model. Run the sender by hand to debug it.

To verify a newly wired sender without touching any task:
`python -m agentcap.ops notify probe` and then `deliver --config ...`; the probe is a
normal outbox row, so a failure stays queued and shows up in `status`.

Delivery is at-least-once with a stable `event_id`. One deliverer runs at a time
(a second one exits 75). A notification that cannot even be encoded or sent is
recorded as an `internal_error` attempt and retried the same way, so it never
blocks the rest of the queue. `deliver` exits 0 only when nothing is left
pending or failed, 1 otherwise, 2 on a config error.

## Restart recovery

Locks are `flock` locks, released by the kernel when their holder dies, so no
stale lock survives a crash. Before every run in a worktree, before every
verification and before every delivery pass (and on `recover`):

- runs left `running` by a dead runner become `interrupted` (one final
  notification per run, even if recovery runs again);
- if that run's model process group is still alive (checked through `/proc`
  with the recorded start time), the worktree stays blocked for new runs until
  `recover --kill-orphans` stops it; its commits are scanned only once the
  processes are gone;
- notifications a dead deliverer left `sending` go back to `pending` (on
  `deliver` and `recover`, only while no live deliverer holds the delivery lock;
  each one gets an `interrupted` attempt record).

Two database unique indexes (one running run per task and per worktree) plus the
per-worktree `flock` make an accidental second job fail closed rather than start
beside the first.

## Claude Code permission hook (worktree-scoped)

`hooks install --worktree W` adds one `Notification` hook to
`W/.claude/settings.local.json`; it records a deduplicated `permission` signal and
queues a `permission_needed` notification when Claude Code shows a permission
prompt. It only touches that file: never `~/.claude`, another worktree, git's
shared `.git/hooks` or git config. It refuses a path that is not a worktree root,
symlinks, unparsable files and a settings file tracked by git; every other key and
hook is preserved and installing twice changes nothing. `hooks uninstall`
restores the original bytes (or removes what it created) unless the file was edited
in the meantime, in which case only the AgentCap entry is removed. `hooks status`
also reports whether the file is git-ignored.

The hook always exits 0, so a broken store can never break the Claude session.

## Limits

- Linux/POSIX; orphan detection uses `/proc` (elsewhere: `killpg(pgid, 0)`).
- The store is local to one host; do not share the state directory over NFS.
- Evidence proves what the runner observed. A model running as the same OS user
  can still edit files in the state directory; the evidence hash in the delivered
  notification lets the coordinator detect later changes.
- Interactive TUI sessions are not wrapped by `run` (stdio is captured); track them
  with the hook and `ops signal`.
- A quota/permission signal from a hook or `ops signal` explains a non-zero run;
  it does not turn a clean exit into a failure. Only Claude Code's explicit
  `permission_denials` result marks an exit-0 run as `permission`.
