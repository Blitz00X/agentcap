"""Run one model command for a tracked task and record what really happened.

The runner holds the worktree's lock for the whole run (one model per
worktree), starts the model in its own session with stdout/stderr captured to
private log files, and records the exit status the kernel reports -- never
one the model printed.  Afterwards it hashes the logs, asks git (read-only)
which commits really appeared, writes ``evidence.json`` exactly once and
finalises the run, which queues one ``run_finished`` notification and one
``commit`` notification per new meaningful commit (deduplicated by hash).
Commits are only looked at after the model has exited, so a commit made
mid-run can never start anything while that model still works.

States describe the *process*, never the task's success:

* a mapped non-zero exit code (AgentCap's own wrapper exits 3 when its
  cap/quota stopped the agent), a quota/permission signal recorded during a
  non-zero run (hooks, wrappers, ``ops signal``), the ``permission_denials``
  Claude Code reports in its ``-p`` JSON result even when it exits 0, or --
  opt-in -- an output pattern on a non-zero run explains the state.  An
  answered permission prompt or a signal seen by a run that then exits 0 does
  not turn a clean run into a failure.
* otherwise exit 0 is ``completed``: the task becomes ``unverified`` and only
  ``task verify`` (independent tests and commit checks, :mod:`.verify`) can
  make it ``success``.
* anything else is ``failure``.

A runner that dies (SIGKILL, OOM, reboot) leaves its run ``running``.
:func:`recover_stale_runs` -- also run before every new run in the same
worktree and before every delivery pass -- marks it ``interrupted`` without
inventing an exit code, records its commits once its model processes are
gone, and refuses new runs there while the old model's process group is still
alive.  A model cannot start a second tracked job from inside its own run.
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import signal
import socket
import subprocess
import threading
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .private import ensure_private_dir, open_private
from . import gitutil
from .locks import FileLock, LockBusy
from .store import Conflict, NotFound, OpsError, OpsStore, iso

EXIT_REFUSED = 2
EXIT_BUSY = 75  # EX_TEMPFAIL: someone else holds the worktree / lock
EXIT_NOT_FOUND = 127
EXIT_CANNOT_EXEC = 126
AGENTCAP_LIMIT_EXIT = 3
#: ``ops run`` exit codes for a model that exited 0 although its run was denied.
EXIT_DENIED_QUOTA = AGENTCAP_LIMIT_EXIT
EXIT_DENIED_PERMISSION = 77  # EX_NOPERM
TAIL_BYTES = 64 * 1024
RESULT_SCAN_BYTES = 8 * 1024 * 1024
REAP_TIMEOUT_S = 2.0
ENV_PREFIX = "AGENTCAP_OPS_"
#: Set in every tracked model's environment; its presence means "inside a model run".
RUN_MARKER = "AGENTCAP_OPS_RUN_ID"
CLAUDE_RESULT_SOURCE = "output:claude-result"
_FORWARDED = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)
_TOOL_NAME = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")


class RunRefused(OpsError):
    """The run was not started; ``exit_code`` is what the CLI should return."""

    def __init__(self, message: str, exit_code: int = EXIT_REFUSED) -> None:
        super().__init__(message)
        self.exit_code = exit_code


def inside_model_run(environ: Mapping[str, str] | None = None) -> str | None:
    """The run id when this process runs inside a tracked model run (``AGENTCAP_OPS_RUN_ID``)."""

    env = os.environ if environ is None else environ
    return env.get(RUN_MARKER) or None


@dataclass(frozen=True)
class Classifier:
    """How a run is attributed to quota or permission."""

    quota_exit_codes: frozenset[int] = frozenset()
    permission_exit_codes: frozenset[int] = frozenset()
    quota_patterns: tuple[re.Pattern[str], ...] = ()
    permission_patterns: tuple[re.Pattern[str], ...] = ()

    @classmethod
    def build(
        cls,
        *,
        quota_exit_codes: Iterable[int] = (),
        permission_exit_codes: Iterable[int] = (),
        quota_patterns: Iterable[str] = (),
        permission_patterns: Iterable[str] = (),
    ) -> "Classifier":
        """Build from plain values; invalid regexes raise :class:`re.error`."""

        return cls(
            frozenset(int(code) for code in quota_exit_codes),
            frozenset(int(code) for code in permission_exit_codes),
            tuple(re.compile(p) for p in quota_patterns),
            tuple(re.compile(p) for p in permission_patterns),
        )

    def for_command(self, argv: Sequence[str]) -> "Classifier":
        """AgentCap's wrapper (``agentcap run|quota``) exits 3 when its limit stopped the agent."""

        if (
            argv
            and os.path.basename(str(argv[0])) == "agentcap"
            and AGENTCAP_LIMIT_EXIT not in self.permission_exit_codes
        ):
            return Classifier(
                self.quota_exit_codes | {AGENTCAP_LIMIT_EXIT},
                self.permission_exit_codes,
                self.quota_patterns,
                self.permission_patterns,
            )
        return self

    def describe(self) -> dict[str, Any]:
        return {
            "quota_exit_codes": sorted(self.quota_exit_codes),
            "permission_exit_codes": sorted(self.permission_exit_codes),
            "quota_patterns": [p.pattern for p in self.quota_patterns],
            "permission_patterns": [p.pattern for p in self.permission_patterns],
        }


@dataclass(frozen=True)
class RunOutcome:
    run_id: str
    task_id: str
    state: str
    reason: str
    exit_code: int | None
    exit_signal: str | None
    run_dir: Path
    evidence_sha256: str
    cli_exit_code: int
    task_state: str | None = None
    commits: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["run_dir"] = str(self.run_dir)
        data["commits"] = list(self.commits)
        return data


# ----------------------------------------------------------- classification
def _signal_name(number: int) -> str:
    try:
        return signal.Signals(number).name
    except ValueError:
        return f"signal {number}"


def _describe_exit(exit_code: int | None, timed_out: float | None) -> str:
    if exit_code is None:
        text = "exit status unknown"
    elif exit_code < 0:
        text = f"killed by {_signal_name(-exit_code)}"
    else:
        text = f"exit code {exit_code}"
    if timed_out is not None:
        text = f"timed out after {timed_out:g}s, {text}"
    return text


def _first_source(signals: Sequence[Mapping[str, Any]], kind: str) -> str:
    for item in signals:
        if item.get("kind") == kind:
            return str(item.get("source") or "unknown source")
    return "unknown source"


def classify(
    *,
    exit_code: int | None,
    timed_out: float | None,
    signals: Sequence[Mapping[str, Any]],
    tails: Sequence[str],
    classifier: Classifier,
    runner_signal: str | None = None,
) -> tuple[str, str]:
    """Return ``(state, reason)`` for a run whose process was started.

    A mapped non-zero exit code wins first.  Quota/permission signals and
    output patterns explain a non-zero exit.  A run that exits 0 is
    ``completed`` -- an answered permission prompt is not a failure -- unless
    Claude Code itself reported refused tool calls (``permission_denials`` in
    its result), which no one could answer.  ``completed`` is never
    ``success``: that needs independent verification.
    """

    base = _describe_exit(exit_code, timed_out)
    if exit_code in classifier.quota_exit_codes:
        return "quota", f"{base} (mapped to quota)"
    if exit_code in classifier.permission_exit_codes:
        return "permission", f"{base} (mapped to permission)"
    denied_exit = exit_code != 0 or timed_out is not None
    if denied_exit:
        kinds = {s["kind"] for s in signals}
        if "quota" in kinds:
            return "quota", f"{base} after a quota signal ({_first_source(signals, 'quota')})"
        if "permission" in kinds:
            return "permission", f"{base} after a permission signal ({_first_source(signals, 'permission')})"
        text = "\n".join(tails)
        if any(p.search(text) for p in classifier.quota_patterns):
            return "quota", f"{base}; output matched a quota pattern"
        if any(p.search(text) for p in classifier.permission_patterns):
            return "permission", f"{base}; output matched a permission pattern"
    if exit_code == 0 and timed_out is None:
        if any(
            s.get("kind") == "permission" and s.get("source") == CLAUDE_RESULT_SOURCE for s in signals
        ):
            return "permission", f"{base}; Claude Code refused tool calls it could not ask about"
        if runner_signal is not None:
            return "failure", f"{base}, but the run was stopped from outside"
        return "completed", f"{base}; not verified yet"
    return "failure", base


def claude_permission_denials(path: str | os.PathLike[str]) -> tuple[int, list[str]] | None:
    """``(count, tool names)`` from the ``permission_denials`` of Claude Code's ``-p`` JSON result.

    ``claude -p --output-format json`` prints one result object and
    ``--output-format stream-json`` ends with one; when Claude Code refused
    tool calls (no one could answer the permission prompt) they are listed
    there -- and the process still exits 0.  Only tool names are returned,
    never the tool input (commands, paths, possibly secrets).  None when the
    output holds no such result.
    """

    try:
        with open(path, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - RESULT_SCAN_BYTES))
            data = handle.read().decode("utf-8", "replace")
    except OSError:
        return None
    candidates = [line.strip() for line in data.splitlines()[-50:]]
    candidates.reverse()
    candidates.append(data.strip())  # a pretty-printed single object
    for text in candidates:
        if not text.startswith("{"):
            continue
        try:
            obj = json.loads(text)
        except ValueError:
            continue
        if not isinstance(obj, dict) or obj.get("type") != "result":
            continue
        denials = obj.get("permission_denials")
        if not isinstance(denials, list) or not denials:
            return 0, []
        names: list[str] = []
        for item in denials:
            name = item.get("tool_name") if isinstance(item, dict) else None
            label = name if isinstance(name, str) and _TOOL_NAME.match(name) else "unknown tool"
            if label not in names:
                names.append(label)
        return len(denials), names
    return None


# ------------------------------------------------------------ processes
def _proc_stat(pid: int) -> tuple[str, int, int] | None:
    """``(state, pgrp, starttime)`` from ``/proc/<pid>/stat``, or None."""

    try:
        with open(f"/proc/{pid}/stat", "rb") as handle:
            data = handle.read().decode("ascii", "replace")
    except OSError:
        return None
    _, sep, rest = data.rpartition(")")
    if not sep:
        return None
    fields = rest.split()
    try:
        return fields[0], int(fields[2]), int(fields[19])
    except (IndexError, ValueError):
        return None


def process_start(pid: int) -> int | None:
    """Kernel start time of ``pid`` (clock ticks since boot), to detect pid reuse."""

    stat = _proc_stat(pid)
    return stat[2] if stat else None


def live_group_members(run: Mapping[str, Any] | None) -> list[int]:
    """Live (non-zombie) processes still in the run's model process group."""

    if not run:
        return []
    pgid = run.get("child_pgid")
    if not pgid or pgid <= 1:
        return []
    if not os.path.isdir("/proc"):
        try:
            os.killpg(pgid, 0)
        except PermissionError:
            return [pgid]
        except OSError:
            return []
        return [pgid]
    try:
        entries = os.listdir("/proc")
    except OSError:
        return []
    members: list[int] = []
    for name in entries:
        if not name.isdigit():
            continue
        stat = _proc_stat(int(name))
        if stat is None or stat[1] != pgid or stat[0] in ("Z", "X"):
            continue
        if int(name) == pgid and run.get("child_start") is not None and stat[2] != run["child_start"]:
            return []  # the pgid was reused by an unrelated process group
        members.append(int(name))
    return members


def _signal_group(pgid: int, sig: int) -> None:
    if pgid <= 1:
        return
    try:
        os.killpg(pgid, sig)
    except (ProcessLookupError, PermissionError):
        pass


def _wait_group_gone(run: Mapping[str, Any], timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while True:
        if not live_group_members(run):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.05)


def _stop_group(run: Mapping[str, Any], timeout: float = REAP_TIMEOUT_S) -> bool:
    """SIGTERM, then SIGKILL, whatever is left of the run's process group."""

    if not live_group_members(run):
        return False
    _signal_group(run["child_pgid"], signal.SIGTERM)
    if not _wait_group_gone(run, timeout):
        _signal_group(run["child_pgid"], signal.SIGKILL)
        _wait_group_gone(run, timeout)
    return True


class _SignalForwarder:
    """While the child runs, SIGINT/SIGTERM/SIGHUP to this process go to the child's group."""

    def __init__(self, pgid: int) -> None:
        self.pgid = pgid
        self.received: str | None = None
        self._previous: dict[int, Any] = {}
        if threading.current_thread() is not threading.main_thread():
            return
        for sig in _FORWARDED:
            try:
                self._previous[sig] = signal.signal(sig, self._forward)
            except (OSError, ValueError):
                pass

    def _forward(self, signum: int, _frame: Any) -> None:
        if self.received is None:
            self.received = _signal_name(signum)
        _signal_group(self.pgid, signum)

    def restore(self) -> None:
        for sig, handler in self._previous.items():
            try:
                signal.signal(sig, handler if handler is not None else signal.SIG_DFL)
            except (OSError, ValueError, TypeError):
                pass
        self._previous.clear()


def _wait(proc: subprocess.Popen, timeout_s: float | None, grace_s: float) -> tuple[int, float | None]:
    try:
        return proc.wait(timeout=timeout_s), None
    except subprocess.TimeoutExpired:
        pass
    _signal_group(proc.pid, signal.SIGTERM)
    try:
        return proc.wait(timeout=grace_s), timeout_s
    except subprocess.TimeoutExpired:
        _signal_group(proc.pid, signal.SIGKILL)
        return proc.wait(), timeout_s


# ------------------------------------------------------------- evidence
def _child_env(base: Mapping[str, str] | None, *, run_id: str, task_id: str, model: str, state_dir: Path) -> dict[str, str]:
    """The model's environment: no ``AGENTCAP_OPS_*`` (config path, fds), plus run markers."""

    source = os.environ if base is None else base
    env = {key: value for key, value in source.items() if not key.startswith(ENV_PREFIX)}
    env.update(
        {
            RUN_MARKER: run_id,
            "AGENTCAP_OPS_TASK_ID": task_id,
            "AGENTCAP_OPS_MODEL": model,
            "AGENTCAP_OPS_DIR": str(state_dir),
        }
    )
    return env


def _digest(path: Path) -> dict[str, Any]:
    digest = hashlib.sha256()
    size = 0
    try:
        with open(path, "rb") as handle:
            for chunk in iter(lambda: handle.read(1 << 20), b""):
                digest.update(chunk)
                size += len(chunk)
    except OSError:
        return {"path": path.name, "bytes": None, "sha256": None}
    return {"path": path.name, "bytes": size, "sha256": digest.hexdigest()}


def _tail(path: Path, limit: int = TAIL_BYTES) -> str:
    try:
        with open(path, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            handle.seek(max(0, handle.tell() - limit))
            return handle.read().decode("utf-8", "replace")
    except OSError:
        return ""


def _cli_exit(exit_code: int | None, spawn_errno: int | None, spawn_error: str | None, state: str) -> int:
    if spawn_error is not None:
        return EXIT_NOT_FOUND if spawn_errno == errno.ENOENT else EXIT_CANNOT_EXEC
    if exit_code is None:
        return 1
    if exit_code < 0:
        return 128 + (-exit_code)
    if exit_code == 0 and state != "completed":
        # Exit 0 must never read as "done" when the run was denied or stopped.
        if state == "quota":
            return EXIT_DENIED_QUOTA
        if state == "permission":
            return EXIT_DENIED_PERMISSION
        return 1
    return exit_code


def scan_commits(worktree: str, head_before: str | None) -> dict[str, Any]:
    """What git shows now: ``head_after``, the new commits and the meaningful ones (read-only)."""

    head_after = gitutil.head(worktree)
    every = gitutil.new_commits(worktree, head_before, head_after)
    meaningful = [sha for sha in every if gitutil.is_meaningful(worktree, sha)]
    return {
        "head_after": head_after,
        "new_commits": len(every),
        "all_commits": every,
        "commits": meaningful,
        "repo": gitutil.repo_name(worktree),
        "branch": gitutil.branch(worktree),
    }


# -------------------------------------------------------------- recovery
def _interrupt_reason(run: Mapping[str, Any], alive: Sequence[int]) -> str:
    if alive:
        return (
            "runner stopped before the run finished; model process group "
            f"{run['child_pgid']} is still running unsupervised"
        )
    code = run.get("exit_code")
    if code is not None:
        how = f"been killed by {_signal_name(-code)}" if code < 0 else f"exited with code {code}"
        return f"runner stopped before finalising the run; the model had {how}"
    return "runner stopped before the run finished; exit status unknown"


def _recover_worktree_locked(store: OpsStore, worktree: str) -> list[dict[str, Any]]:
    """Interrupt every running run in ``worktree`` and record the commits of interrupted runs whose model is gone.

    The caller holds the worktree lock, so the runners of those runs are gone.
    """

    actions = []
    for run in store.running_runs(worktree):
        alive = live_group_members(run)
        scan = None if alive else scan_commits(worktree, run["head_before"])
        _, changed = store.interrupt_run(run["id"], reason=_interrupt_reason(run, alive), scan=scan)
        if changed:
            actions.append(
                {"run_id": run["id"], "task_id": run["task_id"], "worktree": worktree,
                 "action": "interrupted", "child_alive": bool(alive)}
            )
    for run in store.runs_awaiting_commit_scan(worktree):
        if live_group_members(run):
            continue  # its model still works: its commits are looked at once it is gone
        recorded, changed = store.record_run_commits(run["id"], scan=scan_commits(worktree, run["head_before"]))
        if changed and recorded:
            actions.append(
                {"run_id": run["id"], "task_id": run["task_id"], "worktree": worktree,
                 "action": "commits_recorded", "child_alive": False, "commits": recorded}
            )
    return actions


def _recover_worktrees(store: OpsStore, only: Iterable[str] | None = None) -> list[dict[str, Any]]:
    worktrees = {run["worktree"] for run in store.running_runs()}
    worktrees |= {run["worktree"] for run in store.runs_awaiting_commit_scan()}
    if only is not None:
        worktrees &= set(only)
    actions: list[dict[str, Any]] = []
    for worktree in sorted(worktrees):
        lock = FileLock(store.worktree_lock_path(worktree), label="ops recover")
        try:
            lock.acquire(timeout=0.0)
        except LockBusy:
            continue  # a live runner owns this worktree
        try:
            actions.extend(_recover_worktree_locked(store, worktree))
        finally:
            lock.release()
    return actions


def recover_stale_runs(store: OpsStore, *, kill_orphans: bool = False) -> list[dict[str, Any]]:
    """Mark runs whose runner died as ``interrupted``; optionally stop orphaned model groups."""

    actions = _recover_worktrees(store)
    if kill_orphans:
        stopped: set[str] = set()
        for run in store.orphan_candidates():
            if run["state"] == "interrupted" and _stop_group(run):
                actions.append(
                    {"run_id": run["id"], "task_id": run["task_id"], "worktree": run["worktree"],
                     "action": "orphans_stopped", "child_alive": bool(live_group_members(run))}
                )
                stopped.add(run["worktree"])
        if stopped:
            actions.extend(_recover_worktrees(store, only=stopped))
    return actions


# ------------------------------------------------------------------- run
def run_task(
    store: OpsStore,
    *,
    task_id: str,
    model: str,
    argv: Sequence[str],
    classifier: Classifier | None = None,
    timeout_s: float | None = None,
    stdin_path: str | os.PathLike[str] | None = None,
    rerun: bool = False,
    env: Mapping[str, str] | None = None,
    term_grace_s: float = 10.0,
) -> RunOutcome:
    """Run ``argv`` as ``model`` for ``task_id`` in the task's worktree and record it."""

    inside = inside_model_run(env)
    if inside:
        raise RunRefused(
            f"this process runs inside tracked model run {inside} ({RUN_MARKER} is set): a model cannot "
            "start a second job; the coordinator dispatches tasks"
        )
    command = [os.fspath(part) for part in argv]
    if not command:
        raise RunRefused("no command given (put the model command after --)")
    task = store.get_task(task_id)
    if task is None:
        raise RunRefused(f"task {task_id} does not exist; add it with `task add` first")
    worktree = task["worktree"]
    if not os.path.isdir(worktree):
        raise RunRefused(f"worktree {worktree} does not exist")
    if stdin_path is not None and not os.path.isfile(stdin_path):
        raise RunRefused(f"stdin file {stdin_path} does not exist")
    rules = (classifier or Classifier()).for_command(command)

    lock = FileLock(store.worktree_lock_path(worktree), label=f"ops run {task_id}")
    try:
        lock.acquire(timeout=0.0)
    except LockBusy as exc:
        current = store.get_task(task_id)
        if current is not None and current["state"] == "running":
            # A live runner holds the worktree and this task is its run: a
            # duplicate dispatch (refused, exit 2), not a "busy, retry later".
            raise RunRefused(
                f"task {task_id} is already running (run {current['last_run_id']}); not dispatching it twice"
            ) from None
        raise RunRefused(f"{worktree} is busy: {exc}", EXIT_BUSY) from None
    try:
        _recover_worktree_locked(store, worktree)
        for previous in store.orphan_candidates(worktree):
            if live_group_members(previous):
                raise RunRefused(
                    f"run {previous['id']} left model processes running in {worktree} (process group "
                    f"{previous['child_pgid']}); stop them with `recover --kill-orphans` first",
                    EXIT_BUSY,
                )
        run_id = uuid.uuid4().hex
        ensure_private_dir(store.runs_dir)
        run_dir = store.runs_dir / run_id
        os.mkdir(run_dir, 0o700)  # exclusive: a run directory is never reused
        head_before = gitutil.head(worktree)
        try:
            store.start_run(
                task_id=task_id, model=model, argv=command, run_id=run_id,
                run_dir=str(run_dir), head_before=head_before, rerun=rerun,
                repo=gitutil.repo_name(worktree), branch=gitutil.branch(worktree),
            )
        except (Conflict, NotFound, ValueError) as exc:
            os.rmdir(run_dir)
            raise RunRefused(str(exc)) from None
        return _execute(
            store, run_id=run_id, task_id=task_id, model=model, command=command, worktree=worktree,
            run_dir=run_dir, head_before=head_before, rules=rules, timeout_s=timeout_s,
            stdin_path=stdin_path, env=env, term_grace_s=term_grace_s,
        )
    finally:
        lock.release()


def _execute(
    store: OpsStore,
    *,
    run_id: str,
    task_id: str,
    model: str,
    command: list[str],
    worktree: str,
    run_dir: Path,
    head_before: str | None,
    rules: Classifier,
    timeout_s: float | None,
    stdin_path: str | os.PathLike[str] | None,
    env: Mapping[str, str] | None,
    term_grace_s: float,
) -> RunOutcome:
    stdout_path = run_dir / "stdout.log"
    stderr_path = run_dir / "stderr.log"
    child_env = _child_env(env, run_id=run_id, task_id=task_id, model=model, state_dir=store.state_dir)
    started = time.monotonic()
    proc: subprocess.Popen | None = None
    spawn_error: str | None = None
    spawn_errno: int | None = None
    out_fd = open_private(stdout_path)
    err_fd = open_private(stderr_path)
    stdin_handle = None
    try:
        stdin_handle = open(stdin_path, "rb") if stdin_path is not None else None
        proc = subprocess.Popen(
            command,
            cwd=worktree,
            env=child_env,
            stdin=stdin_handle if stdin_handle is not None else subprocess.DEVNULL,
            stdout=out_fd,
            stderr=err_fd,
            start_new_session=True,  # own process group: stoppable as a whole
            close_fds=True,
        )
    except Exception as exc:  # any failure to start is recorded, not raised
        reason = exc.strerror if isinstance(exc, OSError) and exc.strerror else type(exc).__name__
        spawn_error = f"{command[0]}: {reason}"
        spawn_errno = exc.errno if isinstance(exc, OSError) else None
    finally:
        os.close(out_fd)
        os.close(err_fd)
        if stdin_handle is not None:
            stdin_handle.close()

    exit_code: int | None = None
    exit_signal: str | None = None
    timed_out: float | None = None
    runner_signal: str | None = None
    if proc is not None:
        try:
            store.attach_child(run_id, pid=proc.pid, pgid=proc.pid, start=process_start(proc.pid))
        except OpsError:
            pass  # only needed for crash recovery; never abandon a live model over it
        forwarder = _SignalForwarder(proc.pid)
        try:
            exit_code, timed_out = _wait(proc, timeout_s, term_grace_s)
        finally:
            forwarder.restore()
        runner_signal = forwarder.received
        exit_signal = _signal_name(-exit_code) if exit_code < 0 else None
        try:
            store.record_exit(run_id, exit_code, exit_signal)
        except OpsError:
            pass
        # Leftover members of the model's group are orphans now; stop them.
        _stop_group({"child_pgid": proc.pid, "child_start": None})
    duration = round(time.monotonic() - started, 3)

    # The model and its group are gone: only now are its commits looked at.
    scan = scan_commits(worktree, head_before)
    denied = claude_permission_denials(stdout_path) if proc is not None else None
    denial_detail = None
    if denied and denied[0]:
        count, names = denied
        denial_detail = f"Claude Code refused {count} tool call(s): {', '.join(names[:10])}"
        try:
            store.record_signal(
                kind="permission", source=CLAUDE_RESULT_SOURCE, detail=denial_detail, run_id=run_id,
                dedup_key=f"run:{run_id}:claude-permission-denials",
            )
        except (OpsError, ValueError):
            pass  # classified below from the output itself
    signals = list(store.run_signals(run_id))
    if denial_detail is not None and not any(s["kind"] == "permission" for s in signals):
        signals.append(
            {"kind": "permission", "source": CLAUDE_RESULT_SOURCE, "detail": denial_detail,
             "created_at": iso(store.clock()), "seen_count": 1}
        )
    if spawn_error is not None:
        state, reason = "failure", f"could not start the command: {spawn_error}"
    else:
        state, reason = classify(
            exit_code=exit_code, timed_out=timed_out, signals=signals,
            tails=[_tail(stdout_path), _tail(stderr_path)], classifier=rules, runner_signal=runner_signal,
        )
    if runner_signal:
        reason += f"; the runner received {runner_signal} and forwarded it"

    evidence = {
        "schema": "agentcap-ops-evidence/2",
        "run_id": run_id,
        "task_id": task_id,
        "model": model,
        "worktree": worktree,
        "argv": command,
        "host": socket.gethostname(),
        "runner_pid": os.getpid(),
        "child_pid": proc.pid if proc is not None else None,
        "recorded_at": iso(store.clock()),
        "duration_s": duration,
        "exit_code": exit_code,
        "exit_signal": exit_signal,
        "timed_out_after_s": timed_out,
        "runner_signal": runner_signal,
        "spawn_error": spawn_error,
        "state": state,
        "reason": reason,
        "verified": False,  # a run never verifies itself; see `task verify`
        "stdout": _digest(stdout_path),
        "stderr": _digest(stderr_path),
        "git": {
            "head_before": head_before,
            "head_after": scan["head_after"],
            "new_commits": scan["all_commits"],
            "meaningful_commits": scan["commits"],
            "repo": scan["repo"],
            "branch": scan["branch"],
        },
        "permission_denials": (
            {"count": denied[0], "tools": denied[1]} if denied is not None else None
        ),
        "signals": [
            {"kind": s["kind"], "source": s["source"], "detail": s["detail"],
             "first_seen": s["created_at"], "seen_count": s["seen_count"]}
            for s in signals
        ],
        "classifier": rules.describe(),
    }
    blob = (json.dumps(evidence, indent=2, sort_keys=True) + "\n").encode("utf-8")
    fd = open_private(run_dir / "evidence.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL)
    with os.fdopen(fd, "wb") as handle:
        handle.write(blob)
        handle.flush()
        os.fsync(handle.fileno())
    evidence_sha256 = hashlib.sha256(blob).hexdigest()

    final, _ = store.finish_run(
        run_id, state=state, reason=reason, exit_code=exit_code, exit_signal=exit_signal,
        duration_s=duration, evidence_sha256=evidence_sha256, head_after=scan["head_after"],
        new_commits=scan["new_commits"], commits=scan["commits"], repo=scan["repo"], branch=scan["branch"],
    )
    task = store.get_task(task_id)
    return RunOutcome(
        run_id=run_id,
        task_id=task_id,
        state=final["state"],
        reason=final["reason"],
        exit_code=final["exit_code"],
        exit_signal=final["exit_signal"],
        run_dir=run_dir,
        evidence_sha256=final["evidence_sha256"] or evidence_sha256,
        cli_exit_code=_cli_exit(exit_code, spawn_errno, spawn_error, final["state"]),
        task_state=task["state"] if task is not None else None,
        commits=tuple(scan["commits"]),
    )


__all__ = [
    "Classifier",
    "EXIT_BUSY",
    "EXIT_DENIED_PERMISSION",
    "EXIT_DENIED_QUOTA",
    "EXIT_REFUSED",
    "RUN_MARKER",
    "RunOutcome",
    "RunRefused",
    "claude_permission_denials",
    "classify",
    "inside_model_run",
    "live_group_members",
    "process_start",
    "recover_stale_runs",
    "run_task",
    "scan_commits",
]
