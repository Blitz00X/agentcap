"""Independent verification of a completed run (``ops task verify``).

A run only says what the model *process* did.  A task becomes ``success``
only here, from evidence the coordinator produces itself -- never from what
the model printed or claimed:

* **tests**: the coordinator's own test command, run by the tracker in the
  task's worktree (own process group, stdin ``/dev/null``, no
  ``AGENTCAP_OPS_*`` in its environment, stdout/stderr hashed into the
  verification's evidence directory).  It must exit 0 and, when it prints a
  pytest summary line, report no failed test, no error and at least one
  passed test.
* **commits** (unless the task was added with ``requires_commit`` false):
  each commit -- given with ``--commit`` or, by default, the meaningful
  commits recorded for the task's runs -- must exist, be on the worktree's
  current branch (an ancestor of ``HEAD``), be new since the task's first run
  started and change files; and tracked files must not differ from ``HEAD``,
  so the tests ran against the committed code.

Verification refuses (records nothing) inside a tracked model run, while a
model holds the worktree or left processes running there, and for a task
whose last run is not ``completed``.  Its result, evidence hash and one
``task_verified`` notification are recorded in the same transaction as the
task's new state; the verified commits are queued once each, by hash.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import socket
import subprocess
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from .private import ensure_private_dir, open_private
from . import gitutil
from .locks import FileLock, LockBusy
from .runner import (
    ENV_PREFIX,
    EXIT_BUSY,
    RUN_MARKER,
    RunRefused,
    _describe_exit,
    _digest,
    _recover_worktree_locked,
    _signal_name,
    _SignalForwarder,
    _stop_group,
    _tail,
    _wait,
    inside_model_run,
    live_group_members,
)
from .store import OpsStore, iso

DEFAULT_VERIFIED_BY = "coordinator"
MAX_REASON = 1500
_HEX_REF = re.compile(r"^[0-9a-fA-F]{7,64}$")
_SUMMARY_LINE = re.compile(r"\b\d+ (?:passed|failed|errors?|skipped)\b.* in [0-9.]+s")
_SUMMARY_ITEM = re.compile(r"(\d+) (passed|failed|skipped|errors?|xfailed|xpassed|deselected|warnings?)\b")


def parse_test_counts(text: str) -> dict[str, int] | None:
    """Counts from the last pytest summary line in ``text`` (``3 passed, 1 skipped in 0.12s``), or None."""

    for line in reversed(text.splitlines()):
        if not _SUMMARY_LINE.search(line):
            continue
        counts = {"passed": 0, "failed": 0, "skipped": 0, "errors": 0}
        for number, word in _SUMMARY_ITEM.findall(line):
            key = "errors" if word.startswith("error") else word
            if key in counts:
                counts[key] += int(number)
        return counts
    return None


@dataclass(frozen=True)
class CommitCheck:
    sha: str
    requested: bool  # given with --commit (otherwise recorded for one of the task's runs)
    on_branch: bool
    new: bool
    meaningful: bool

    @property
    def ok(self) -> bool:
        return self.on_branch and self.new and self.meaningful

    @property
    def problem(self) -> str | None:
        if not self.on_branch:
            return f"commit {self.sha[:12]} is not on the worktree's current branch"
        if not self.new:
            return f"commit {self.sha[:12]} already existed before the task's first run"
        if not self.meaningful:
            return f"commit {self.sha[:12]} changes no files"
        return None


@dataclass(frozen=True)
class VerifyOutcome:
    verification_id: int
    task_id: str
    run_id: str
    state: str  # passed | failed
    reason: str
    task_state: str
    tests: dict[str, Any]
    commits: tuple[str, ...]
    evidence_dir: Path
    evidence_sha256: str

    @property
    def cli_exit_code(self) -> int:
        return 0 if self.state == "passed" else 1

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["evidence_dir"] = str(self.evidence_dir)
        data["commits"] = list(self.commits)
        data["cli_exit_code"] = self.cli_exit_code
        return data


def _require_verifiable(store: OpsStore, task: Mapping[str, Any]) -> dict[str, Any]:
    """The completed last run of an unverified task, or a refusal saying why there is none."""

    task_id, state = task["id"], task["state"]
    if state == "unverified":
        run = store.get_run(task["last_run_id"]) if task["last_run_id"] else None
        if run is not None and run["state"] == "completed":
            return run
        raise RunRefused(f"task {task_id} has no completed run to verify")
    if state == "success":
        raise RunRefused(f"task {task_id} is already verified (verification #{task['verification_id']})")
    if state == "running":
        raise RunRefused(f"task {task_id} is running; verify it after its run has completed")
    if state == "queued":
        raise RunRefused(f"task {task_id} has not run yet")
    if state == "failure" and task["verification_id"] is not None:
        raise RunRefused(
            f"task {task_id} failed verification #{task['verification_id']}; rerun it before verifying again"
        )
    raise RunRefused(
        f"task {task_id}'s last run ended {state}; only a completed run (exit 0 without quota or "
        "permission denials) can be verified -- rerun it first"
    )


def _check_commits(
    worktree: str, head: str | None, base: str | None, requested: Sequence[str], recorded: Sequence[str]
) -> tuple[list[CommitCheck], list[str]]:
    """Checks of the requested commits (or of the recorded ones still on the branch)."""

    checks: list[CommitCheck] = []
    off_branch: list[str] = []
    if requested:
        candidates = [(sha, True) for sha in requested]
    else:
        candidates = []
        for sha in recorded:
            if head and gitutil.is_ancestor(worktree, sha, head) is True:
                candidates.append((sha, False))
            else:
                off_branch.append(sha)  # rewritten or reset since; not part of the work any more
    for sha, was_requested in candidates:
        on_branch = bool(head) and gitutil.is_ancestor(worktree, sha, head) is True
        new = True
        if base:
            new = sha != base and gitutil.is_ancestor(worktree, sha, base) is False
        checks.append(CommitCheck(sha, was_requested, on_branch, new, gitutil.is_meaningful(worktree, sha)))
    return checks, off_branch


def _run_tests(
    command: Sequence[str],
    *,
    worktree: str,
    evidence_dir: Path,
    env: Mapping[str, str] | None,
    timeout_s: float | None,
    term_grace_s: float,
) -> dict[str, Any]:
    stdout_path = evidence_dir / "stdout.log"
    stderr_path = evidence_dir / "stderr.log"
    source = os.environ if env is None else env
    child_env = {key: value for key, value in source.items() if not key.startswith(ENV_PREFIX)}
    out_fd = open_private(stdout_path)
    err_fd = open_private(stderr_path)
    proc: subprocess.Popen | None = None
    spawn_error: str | None = None
    started = time.monotonic()
    try:
        proc = subprocess.Popen(
            list(command),
            cwd=worktree,
            env=child_env,
            stdin=subprocess.DEVNULL,
            stdout=out_fd,
            stderr=err_fd,
            start_new_session=True,
            close_fds=True,
        )
    except Exception as exc:  # recorded as a failed verification, not raised
        reason = exc.strerror if isinstance(exc, OSError) and exc.strerror else type(exc).__name__
        spawn_error = f"{command[0]}: {reason}"
    finally:
        os.close(out_fd)
        os.close(err_fd)
    exit_code: int | None = None
    timed_out: float | None = None
    if proc is not None:
        forwarder = _SignalForwarder(proc.pid)
        try:
            exit_code, timed_out = _wait(proc, timeout_s, term_grace_s)
        finally:
            forwarder.restore()
        _stop_group({"child_pgid": proc.pid, "child_start": None})
    return {
        "argv": list(command),
        "exit_code": exit_code,
        "exit_signal": _signal_name(-exit_code) if exit_code is not None and exit_code < 0 else None,
        "timed_out_after_s": timed_out,
        "spawn_error": spawn_error,
        "duration_s": round(time.monotonic() - started, 3),
        "counts": parse_test_counts(_tail(stdout_path) + "\n" + _tail(stderr_path)),
        "stdout": _digest(stdout_path),
        "stderr": _digest(stderr_path),
    }


def _test_problems(tests: Mapping[str, Any]) -> list[str]:
    if tests["spawn_error"] is not None:
        return [f"the test command could not start ({tests['spawn_error']})"]
    problems = []
    if tests["timed_out_after_s"] is not None or tests["exit_code"] != 0:
        problems.append(f"the tests ended with {_describe_exit(tests['exit_code'], tests['timed_out_after_s'])}")
    counts = tests["counts"]
    if counts is not None:
        if counts["failed"] or counts["errors"]:
            problems.append(f"the tests reported {counts['failed']} failed and {counts['errors']} errors")
        elif counts["passed"] == 0:
            problems.append("the tests reported no passed test")
    return problems


def _counts_text(tests: Mapping[str, Any]) -> str:
    counts = tests["counts"]
    if counts is None:
        return f"{_describe_exit(tests['exit_code'], None)}, no pytest summary"
    return f"{counts['passed']} passed, {counts['failed']} failed, {counts['skipped']} skipped, {counts['errors']} errors"


def verify_task(
    store: OpsStore,
    *,
    task_id: str,
    argv: Sequence[str],
    commits: Sequence[str] = (),
    timeout_s: float | None = None,
    verified_by: str = DEFAULT_VERIFIED_BY,
    env: Mapping[str, str] | None = None,
    term_grace_s: float = 10.0,
) -> VerifyOutcome:
    """Run the coordinator's tests for ``task_id``'s completed run, check its commits and record the result."""

    inside = inside_model_run(env)
    if inside:
        raise RunRefused(
            f"verification is the coordinator's job: this process runs inside tracked model run {inside} "
            f"({RUN_MARKER} is set) and a model may not verify work"
        )
    command = [os.fspath(part) for part in argv]
    if not command:
        raise RunRefused("no test command given (put the independent test command after --)")
    for ref in commits:
        if not isinstance(ref, str) or not _HEX_REF.match(ref):
            raise RunRefused("--commit takes a commit hash (7-64 hex digits)")
    task = store.get_task(task_id)
    if task is None:
        raise RunRefused(f"task {task_id} does not exist")
    _require_verifiable(store, task)
    worktree = task["worktree"]
    if not os.path.isdir(worktree):
        raise RunRefused(f"worktree {worktree} does not exist")

    lock = FileLock(store.worktree_lock_path(worktree), label=f"ops verify {task_id}")
    try:
        lock.acquire(timeout=0.0)
    except LockBusy as exc:
        raise RunRefused(
            f"{worktree} is busy ({exc}); a task is verified only while no model works in its worktree", EXIT_BUSY
        ) from None
    try:
        _recover_worktree_locked(store, worktree)
        for previous in store.orphan_candidates(worktree):
            if live_group_members(previous):
                raise RunRefused(
                    f"run {previous['id']} left model processes running in {worktree}; stop them with "
                    "`recover --kill-orphans` before verifying",
                    EXIT_BUSY,
                )
        task = store.get_task(task_id)
        run = _require_verifiable(store, task)
        return _verify_locked(
            store, task=task, run=run, command=command, commits=commits, timeout_s=timeout_s,
            verified_by=verified_by, env=env, term_grace_s=term_grace_s,
        )
    finally:
        lock.release()


def _verify_locked(
    store: OpsStore,
    *,
    task: Mapping[str, Any],
    run: Mapping[str, Any],
    command: list[str],
    commits: Sequence[str],
    timeout_s: float | None,
    verified_by: str,
    env: Mapping[str, str] | None,
    term_grace_s: float,
) -> VerifyOutcome:
    worktree = task["worktree"]
    head = gitutil.head(worktree)
    requested: list[str] = []
    for ref in commits:
        sha = gitutil.resolve_commit(worktree, ref)
        if sha is None:
            raise RunRefused(f"commit {ref} does not exist in {worktree}")
        if sha not in requested:
            requested.append(sha)
    runs = store.list_runs(task_id=task["id"])
    base = runs[0]["head_before"] if runs else None
    recorded = [row["sha"] for row in store.list_commits(task_id=task["id"])]
    checks, off_branch = _check_commits(worktree, head, base, requested, recorded)
    requires_commit = bool(task["requires_commit"])
    dirty = gitutil.dirty(worktree) if head else None

    ensure_private_dir(store.verifications_dir)
    evidence_dir = store.verifications_dir / uuid.uuid4().hex
    os.mkdir(evidence_dir, 0o700)  # exclusive: never reused
    tests = _run_tests(
        command, worktree=worktree, evidence_dir=evidence_dir, env=env, timeout_s=timeout_s,
        term_grace_s=term_grace_s,
    )

    problems = _test_problems(tests)
    problems += [check.problem for check in checks if not check.ok]
    verified = [check.sha for check in checks if check.ok]
    if requires_commit:
        if not head:
            problems.append("the worktree has no commit; this task requires one")
        elif not checks:
            problems.append(
                "no meaningful commit of this task is on the current branch; pass --commit SHA for the "
                "commit that holds the work"
            )
        if dirty:
            problems.append(
                "tracked files differ from HEAD, so the tests did not run against the verified commit; "
                "commit or discard those changes first"
            )
        elif dirty is None and head:
            problems.append("git could not tell whether tracked files differ from HEAD")
    passed = not problems
    if passed:
        reason = f"independent tests passed ({_counts_text(tests)})"
        reason += f"; commits {', '.join(sha[:12] for sha in verified)}" if verified else "; no commit required"
    else:
        reason = "; ".join(problems)
    if len(reason) > MAX_REASON:
        reason = reason[: MAX_REASON - 3] + "..."

    evidence = {
        "schema": "agentcap-ops-verification/1",
        "task_id": task["id"],
        "run_id": run["id"],
        "run_evidence_sha256": run["evidence_sha256"],
        "worktree": worktree,
        "verified_by": verified_by,
        "host": socket.gethostname(),
        "verifier_pid": os.getpid(),
        "recorded_at": iso(store.clock()),
        "result": "passed" if passed else "failed",
        "reason": reason,
        "tests": tests,
        "git": {
            "head": head,
            "base": base,
            "repo": gitutil.repo_name(worktree),
            "branch": gitutil.branch(worktree),
            "tracked_changes": dirty,
            "requires_commit": requires_commit,
            "commits": [{**asdict(check), "ok": check.ok, "problem": check.problem} for check in checks],
            "recorded_but_not_on_branch": off_branch,
        },
    }
    blob = (json.dumps(evidence, indent=2, sort_keys=True) + "\n").encode("utf-8")
    fd = open_private(evidence_dir / "verification.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL)
    with os.fdopen(fd, "wb") as handle:
        handle.write(blob)
        handle.flush()
        os.fsync(handle.fileno())
    digest = hashlib.sha256(blob).hexdigest()

    verification, final_task = store.record_verification(
        task_id=task["id"], run_id=run["id"], passed=passed, reason=reason, verified_by=verified_by,
        test_argv=command, test_exit_code=tests["exit_code"], test_exit_signal=tests["exit_signal"],
        counts=tests["counts"], duration_s=tests["duration_s"], head=head,
        repo=evidence["git"]["repo"], branch=evidence["git"]["branch"], commits=verified,
        evidence_dir=str(evidence_dir), evidence_sha256=digest,
    )
    return VerifyOutcome(
        verification_id=verification["id"],
        task_id=task["id"],
        run_id=run["id"],
        state=verification["state"],
        reason=verification["reason"],
        task_state=final_task["state"],
        tests={key: tests[key] for key in ("exit_code", "exit_signal", "timed_out_after_s", "counts", "duration_s")},
        commits=tuple(verified),
        evidence_dir=evidence_dir,
        evidence_sha256=digest,
    )


__all__ = ["CommitCheck", "VerifyOutcome", "parse_test_counts", "verify_task"]
