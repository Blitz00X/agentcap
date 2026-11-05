"""Notification payloads for the ops outbox.

Every payload carries the same envelope, with ``null`` where a field does not
apply: ``event_id`` (stable per logical event, added when it is queued),
``kind``, ``project``, ``repo``, ``branch``, ``task``, ``run``,
``commit_sha``, ``exit_code``, ``tests`` and ``evidence_paths``.  ``event``,
``task_id`` and ``run_id`` are kept as aliases of ``kind``, ``task`` and
``run`` for senders written against payload schema 1.

Payloads are built from a fixed set of fields.  They never carry model
output, prompts, argv, environment, config or full paths: ``repo`` and
``worktree`` are directory names, ``evidence_paths`` are relative to the ops
state directory, and free text goes through :func:`agentcap.alerts.redact`
and is truncated.
"""

from __future__ import annotations

import hashlib
import os
from typing import Any, Iterable, Mapping

from ..alerts import redact

SOURCE = "agentcap-ops"
SCHEMA = 2
KINDS = ("run_finished", "commit", "task_verified", "permission_needed", "quota_reached", "probe")
#: Envelope keys present in every payload (``event_id`` is added by the store).
ENVELOPE_KEYS = (
    "event_id", "kind", "project", "repo", "branch", "task", "run", "commit_sha", "exit_code", "tests",
    "evidence_paths",
)
MAX_LISTED_COMMITS = 20


def event_id(dedup_key: str) -> str:
    """Stable id for one logical event (sent as ``Idempotency-Key``)."""

    return hashlib.sha256(dedup_key.encode("utf-8")).hexdigest()[:32]


def worktree_name(path: str | None) -> str | None:
    if not path:
        return None
    return os.path.basename(os.path.normpath(path)) or path


def _short(text: Any, limit: int) -> str | None:
    if text is None:
        return None
    clean = " ".join(redact(str(text)).split())
    return clean if len(clean) <= limit else clean[: limit - 3] + "..."


def _name(value: Any, limit: int = 120) -> str | None:
    return _short(value, limit) if value else None


def _get(row: Mapping[str, Any] | None, key: str) -> Any:
    return row.get(key) if row else None


def envelope(
    kind: str,
    *,
    project: str | None = None,
    repo: str | None = None,
    branch: str | None = None,
    task: str | None = None,
    run: str | None = None,
    commit_sha: str | None = None,
    exit_code: int | None = None,
    tests: Mapping[str, Any] | None = None,
    evidence_paths: Iterable[str] = (),
) -> dict[str, Any]:
    """The fields every notification carries (``event_id`` is added when it is queued)."""

    return {
        "source": SOURCE,
        "schema": SCHEMA,
        "kind": kind,
        "event": kind,
        "project": _name(project),
        "repo": _name(repo),
        "branch": _name(branch, 200),
        "task": task,
        "task_id": task,
        "run": run,
        "run_id": run,
        "commit_sha": commit_sha,
        "exit_code": exit_code,
        "tests": dict(tests) if tests is not None else None,
        "evidence_paths": [str(path) for path in evidence_paths],
    }


def _project(task: Mapping[str, Any] | None, repo: str | None) -> str | None:
    return _get(task, "project") or repo


def tests_summary(verification: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """The ``tests`` field: what the coordinator's independent test run reported."""

    if not verification:
        return None
    return {
        "status": verification["state"],
        "exit_code": verification["test_exit_code"],
        "passed": verification["tests_passed"],
        "failed": verification["tests_failed"],
        "skipped": verification["tests_skipped"],
        "errors": verification["tests_errors"],
        "duration_s": verification["duration_s"],
        "verification_id": verification["id"],
    }


def run_finished(
    run: Mapping[str, Any],
    task: Mapping[str, Any] | None,
    signal_counts: Mapping[str, int],
    *,
    evidence_paths: Iterable[str] = (),
    commits: Iterable[str] = (),
) -> dict[str, Any]:
    commits = list(commits)
    repo = _get(run, "repo")
    payload = envelope(
        "run_finished",
        project=_project(task, repo),
        repo=repo,
        branch=_get(run, "branch"),
        task=run["task_id"],
        run=run["id"],
        commit_sha=commits[-1] if commits else None,
        exit_code=run["exit_code"],
        tests=None,  # a run is never tested by itself: see task_verified
        evidence_paths=evidence_paths,
    )
    payload.update(
        {
            "state": run["state"],
            "task_state": _get(task, "state"),
            "task_title": _short(task["title"], 120) if task else None,
            "model": run["model"],
            "worktree": worktree_name(run["worktree"]),
            "exit_signal": run["exit_signal"],
            "reason": _short(run["reason"], 300),
            "started_at": run["started_at"],
            "ended_at": run["ended_at"],
            "duration_s": run["duration_s"],
            "evidence_sha256": run["evidence_sha256"],
            "head_before": run["head_before"],
            "head_after": run["head_after"],
            "new_commits": run["new_commits"],
            "commits": commits[-MAX_LISTED_COMMITS:],
            "commits_total": len(commits),
            "signals": {
                "permission": int(signal_counts.get("permission", 0)),
                "quota": int(signal_counts.get("quota", 0)),
            },
        }
    )
    payload["message"] = message(payload)
    return payload


def commit_recorded(
    commit: Mapping[str, Any],
    run: Mapping[str, Any] | None,
    task: Mapping[str, Any] | None,
    *,
    tests: Mapping[str, Any] | None = None,
    evidence_paths: Iterable[str] = (),
) -> dict[str, Any]:
    """One meaningful commit git showed (queued once per commit hash)."""

    repo = commit.get("repo")
    payload = envelope(
        "commit",
        project=_project(task, repo),
        repo=repo,
        branch=commit.get("branch"),
        task=commit.get("task_id"),
        run=commit.get("run_id"),
        commit_sha=commit["sha"],
        exit_code=_get(run, "exit_code"),
        tests=tests,
        evidence_paths=evidence_paths,
    )
    payload.update(
        {
            "state": "verified" if commit.get("verified_at") else "recorded",
            "commit_source": commit["source"],
            "first_seen_at": commit["first_seen_at"],
            "task_state": _get(task, "state"),
            "model": _get(run, "model"),
            "worktree": worktree_name(commit.get("worktree")),
        }
    )
    payload["message"] = message(payload)
    return payload


def task_verified(
    verification: Mapping[str, Any],
    task: Mapping[str, Any] | None,
    run: Mapping[str, Any] | None,
    commits: Iterable[str],
    *,
    evidence_paths: Iterable[str] = (),
) -> dict[str, Any]:
    """The coordinator's independent verification of a completed run."""

    commits = list(commits)
    passed = verification["state"] == "passed"
    repo = verification.get("repo") or _get(run, "repo")
    payload = envelope(
        "task_verified",
        project=_project(task, repo),
        repo=repo,
        branch=verification.get("branch") or _get(run, "branch"),
        task=verification["task_id"],
        run=verification["run_id"],
        commit_sha=commits[-1] if commits else None,
        exit_code=_get(run, "exit_code"),
        tests=tests_summary(verification),
        evidence_paths=evidence_paths,
    )
    payload.update(
        {
            "state": "success" if passed else "failure",
            "verified": passed,
            "verification_id": verification["id"],
            "reason": _short(verification["reason"], 300),
            "commits": commits[-MAX_LISTED_COMMITS:],
            "commits_total": len(commits),
            "task_title": _short(task["title"], 120) if task else None,
            "model": _get(run, "model"),
            "worktree": worktree_name(_get(task, "worktree")),
            "verified_by": _short(verification["verified_by"], 60),
            "verified_at": verification["created_at"],
        }
    )
    payload["message"] = message(payload)
    return payload


def signal_raised(
    signal: Mapping[str, Any],
    run: Mapping[str, Any] | None,
    model: str | None,
    task: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    kind = signal["kind"]
    repo = _get(run, "repo")
    payload = envelope(
        "permission_needed" if kind == "permission" else "quota_reached",
        project=_project(task, repo),
        repo=repo,
        branch=_get(run, "branch"),
        task=_get(run, "task_id"),
        run=_get(run, "id"),
    )
    payload.update(
        {
            "state": kind,
            "model": model,
            "worktree": worktree_name(signal["worktree"]),
            "detail": _short(signal["detail"], 200),
            "signal_source": signal["source"],
            "raised_at": signal["created_at"],
        }
    )
    payload["message"] = message(payload)
    return payload


def probe(at: str) -> dict[str, Any]:
    """Harmless test event the coordinator queues to check its sender."""

    payload = envelope("probe")
    payload.update({"state": "probe", "raised_at": at})
    payload["message"] = f"agentcap-ops: endpoint probe {at}"
    return payload


def message(payload: Mapping[str, Any]) -> str:
    """One human-readable line (used by the text format)."""

    kind = payload.get("kind") or payload.get("event")
    where = payload.get("worktree") or "?"
    model = payload.get("model") or "model unknown"
    task = payload.get("task") or payload.get("task_id") or "-"
    run = str(payload.get("run") or payload.get("run_id") or "")[:12]
    if kind == "run_finished":
        code = payload.get("exit_code")
        exit_part = f"exit {code}" if code is not None else "exit unknown"
        if payload.get("exit_signal"):
            exit_part += f" ({payload['exit_signal']})"
        text = (
            f"agentcap-ops: {task} {str(payload.get('state', '?')).upper()} [{model}] "
            f"{exit_part}, run {run} in {where}"
        )
        if payload.get("state") == "completed":
            text += "; not verified yet (task verify)"
        elif payload.get("reason"):
            text += f": {payload['reason']}"
        return text
    if kind == "commit":
        sha = str(payload.get("commit_sha") or "")[:12]
        on = payload.get("branch") or "a detached HEAD"
        repo = payload.get("repo") or where
        return f"agentcap-ops: COMMIT {sha} on {on} in {repo} (task {task}, run {run or '-'})"
    if kind == "task_verified":
        tests = payload.get("tests") or {}
        counts = ", ".join(
            f"{tests[key]} {key}" for key in ("passed", "failed", "skipped") if tests.get(key) is not None
        )
        if payload.get("verified"):
            text = f"agentcap-ops: {task} VERIFIED [{model}]"
            if counts:
                text += f": tests {counts}"
            sha = str(payload.get("commit_sha") or "")[:12]
            if sha:
                text += f"; commit {sha}"
            return text
        text = f"agentcap-ops: {task} VERIFICATION FAILED [{model}]"
        if payload.get("reason"):
            text += f": {payload['reason']}"
        return text
    if kind in ("permission_needed", "quota_reached"):
        label = "PERMISSION NEEDED" if kind == "permission_needed" else "QUOTA REACHED"
        text = f"agentcap-ops: {task} {label} [{model}] in {where}"
        if payload.get("detail"):
            text += f": {payload['detail']}"
        return text
    return f"agentcap-ops: {kind}"


__all__ = [
    "ENVELOPE_KEYS",
    "KINDS",
    "commit_recorded",
    "envelope",
    "event_id",
    "message",
    "probe",
    "run_finished",
    "signal_raised",
    "task_verified",
    "tests_summary",
    "worktree_name",
]
