"""Persistent single-writer store for AgentCap operations tracking.

One SQLite database under the ops state directory (``$AGENTCAP_OPS_DIR`` or
``~/.local/state/agentcap/ops``, 0700, files 0600):

* ``tasks`` -- what the coordinator dispatched, deduplicated by id, with the
  tasks each one depends on and whether its verification needs a commit.
* ``runs`` -- one row per model run with the kernel-reported exit code and the
  hash of its evidence file.  Partial unique indexes allow at most one running
  run per task and per worktree.
* ``verifications`` -- the coordinator's independent check of a completed run:
  its own test command (exit code, counts, hashed logs) and the commits it
  confirmed on the branch.
* ``commits`` -- every meaningful commit git showed after a run, during
  recovery or in a verification, keyed by its hash: each one is queued once.
* ``signals`` -- quota/permission events reported while a run is active
  (Claude Code hooks, wrappers, ``ops signal``, Claude Code's own JSON
  result), deduplicated by key.
* ``models`` / ``model_history`` -- the explicit active model per worktree.
* ``notifications`` / ``delivery_attempts`` -- the outbox.  Rows are never
  deleted: a failed delivery stays queued with its error and attempt count.

Run and task states are split on purpose.  A run records what the *process*
did: ``completed`` (exit 0 without any quota/permission denial), ``failure``,
``quota``, ``permission`` or ``interrupted``.  The task of a completed run is
``unverified``; only :meth:`OpsStore.record_verification` with passing
independent tests (and verified commits, unless the task needs none) makes
it ``success``.  A ``CHECK`` constraint and two triggers refuse a ``success``
task without a passed verification of its last run, whoever writes the row.

Every write goes through :meth:`OpsStore._write`: an exclusive ``flock`` on
``ops.lock`` (one writer across all processes, dropped by the kernel if the
holder dies) around ``BEGIN IMMEDIATE`` ... ``COMMIT``.  A run's final state,
its notification and the notifications of its commits are committed in the
same transaction, so a crash can never leave a finished run without them.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import socket
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Mapping, Sequence

from .private import ensure_private_dir, open_private
from . import events
from .locks import FileLock, LockBusy

SCHEMA_VERSION = 2
STATE_DIR_ENV = "AGENTCAP_OPS_DIR"

RUN_FINAL_STATES = ("completed", "failure", "quota", "permission", "interrupted")
RUN_STATES = ("running",) + RUN_FINAL_STATES
TASK_STATES = ("queued", "running", "unverified", "success", "failure", "quota", "permission", "interrupted")
NOTIFY_STATES = ("pending", "sending", "delivered", "failed")
SIGNAL_KINDS = ("quota", "permission", "info")
VERIFICATION_STATES = ("passed", "failed")
COMMIT_SOURCES = ("run", "recovery", "verification")
MODEL_STATES = ("ready", "running", "quota", "permission", "failure", "interrupted")
#: Task state after its run ends in each final run state.  ``success`` is not
#: here: only a passed verification sets it.
TASK_STATE_AFTER = {
    "completed": "unverified",
    "failure": "failure",
    "quota": "quota",
    "permission": "permission",
    "interrupted": "interrupted",
}
#: Active-model state after a run ends in each final state.
MODEL_STATE_AFTER = {
    "completed": "ready",
    "failure": "failure",
    "quota": "quota",
    "permission": "permission",
    "interrupted": "interrupted",
}
RUN_EVIDENCE_FILES = ("evidence.json", "stdout.log", "stderr.log")
VERIFICATION_EVIDENCE_FILES = ("verification.json", "stdout.log", "stderr.log")

MAX_PAYLOAD_BYTES = 16 * 1024
MAX_DEPENDENCIES = 32
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,127}$")
_SHA_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")

_TASKS_DDL = """CREATE TABLE IF NOT EXISTS {name} (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    worktree TEXT NOT NULL,
    project TEXT,
    depends_on_json TEXT NOT NULL DEFAULT '[]',
    requires_commit INTEGER NOT NULL DEFAULT 1 CHECK (requires_commit IN (0, 1)),
    state TEXT NOT NULL CHECK (state IN
        ('queued', 'running', 'unverified', 'success', 'failure', 'quota', 'permission', 'interrupted')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_run_id TEXT,
    run_count INTEGER NOT NULL DEFAULT 0,
    verification_id INTEGER,
    verified_at TEXT,
    CHECK (state != 'success' OR (verification_id IS NOT NULL AND verified_at IS NOT NULL))
)"""

_RUNS_DDL = """CREATE TABLE IF NOT EXISTS {name} (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(id),
    worktree TEXT NOT NULL,
    model TEXT NOT NULL,
    argv_json TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN
        ('running', 'completed', 'failure', 'quota', 'permission', 'interrupted')),
    reason TEXT,
    exit_code INTEGER,
    exit_signal TEXT,
    host TEXT NOT NULL,
    runner_pid INTEGER NOT NULL,
    child_pid INTEGER,
    child_pgid INTEGER,
    child_start INTEGER,
    started_at TEXT NOT NULL,
    started_epoch REAL NOT NULL,
    ended_at TEXT,
    duration_s REAL,
    run_dir TEXT NOT NULL,
    evidence_sha256 TEXT,
    head_before TEXT,
    head_after TEXT,
    new_commits INTEGER NOT NULL DEFAULT 0,
    repo TEXT,
    branch TEXT,
    commits_recorded INTEGER NOT NULL DEFAULT 0
)"""

#: Every statement of schema 2, in creation order (tables, indexes, triggers).
_SCHEMA_STATEMENTS = (
    _TASKS_DDL.format(name="tasks"),
    _RUNS_DDL.format(name="runs"),
    """CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    dedup_key TEXT NOT NULL UNIQUE,
    run_id TEXT REFERENCES runs(id),
    worktree TEXT,
    kind TEXT NOT NULL CHECK (kind IN ('quota', 'permission', 'info')),
    source TEXT NOT NULL,
    detail TEXT,
    created_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    seen_count INTEGER NOT NULL DEFAULT 1
)""",
    """CREATE TABLE IF NOT EXISTS models (
    worktree TEXT PRIMARY KEY,
    model TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN
        ('ready', 'running', 'quota', 'permission', 'failure', 'interrupted')),
    run_id TEXT,
    set_by TEXT NOT NULL,
    updated_at TEXT NOT NULL
)""",
    """CREATE TABLE IF NOT EXISTS model_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    worktree TEXT NOT NULL,
    model TEXT NOT NULL,
    state TEXT NOT NULL,
    run_id TEXT,
    note TEXT,
    at TEXT NOT NULL
)""",
    """CREATE TABLE IF NOT EXISTS notifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    dedup_key TEXT NOT NULL UNIQUE,
    event TEXT NOT NULL,
    task_id TEXT,
    run_id TEXT,
    payload_json TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('pending', 'sending', 'delivered', 'failed')),
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at REAL NOT NULL,
    last_error TEXT,
    last_http_status INTEGER,
    duplicates INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    delivered_at TEXT
)""",
    """CREATE TABLE IF NOT EXISTS delivery_attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    notification_id INTEGER NOT NULL REFERENCES notifications(id),
    attempted_at TEXT NOT NULL,
    outcome TEXT NOT NULL,
    http_status INTEGER,
    error TEXT,
    endpoint TEXT NOT NULL
)""",
    """CREATE TABLE IF NOT EXISTS verifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL REFERENCES tasks(id),
    run_id TEXT NOT NULL REFERENCES runs(id),
    state TEXT NOT NULL CHECK (state IN ('passed', 'failed')),
    reason TEXT NOT NULL,
    verified_by TEXT NOT NULL,
    host TEXT NOT NULL,
    test_argv_json TEXT NOT NULL,
    test_exit_code INTEGER,
    test_exit_signal TEXT,
    tests_passed INTEGER,
    tests_failed INTEGER,
    tests_skipped INTEGER,
    tests_errors INTEGER,
    duration_s REAL,
    head TEXT,
    repo TEXT,
    branch TEXT,
    commits_json TEXT NOT NULL DEFAULT '[]',
    evidence_dir TEXT NOT NULL,
    evidence_sha256 TEXT NOT NULL,
    created_at TEXT NOT NULL
)""",
    """CREATE TABLE IF NOT EXISTS commits (
    sha TEXT PRIMARY KEY,
    task_id TEXT REFERENCES tasks(id),
    run_id TEXT REFERENCES runs(id),
    worktree TEXT NOT NULL,
    repo TEXT,
    branch TEXT,
    source TEXT NOT NULL CHECK (source IN ('run', 'recovery', 'verification')),
    first_seen_at TEXT NOT NULL,
    verification_id INTEGER REFERENCES verifications(id),
    verified_at TEXT
)""",
    "CREATE UNIQUE INDEX IF NOT EXISTS runs_one_running_per_task ON runs(task_id) WHERE state = 'running'",
    "CREATE UNIQUE INDEX IF NOT EXISTS runs_one_running_per_worktree ON runs(worktree) WHERE state = 'running'",
    "CREATE INDEX IF NOT EXISTS runs_by_worktree ON runs(worktree, started_epoch)",
    "CREATE INDEX IF NOT EXISTS signals_by_run ON signals(run_id)",
    "CREATE INDEX IF NOT EXISTS notifications_due ON notifications(state, next_attempt_at)",
    "CREATE INDEX IF NOT EXISTS verifications_by_task ON verifications(task_id, id)",
    "CREATE INDEX IF NOT EXISTS commits_by_task ON commits(task_id)",
    """CREATE TRIGGER IF NOT EXISTS tasks_insert_never_success
BEFORE INSERT ON tasks WHEN NEW.state = 'success'
BEGIN
    SELECT RAISE(ABORT, 'a task becomes success only through a passed verification');
END""",
    """CREATE TRIGGER IF NOT EXISTS tasks_success_needs_a_passed_verification
BEFORE UPDATE ON tasks
WHEN NEW.state = 'success' AND NOT EXISTS (
    SELECT 1 FROM verifications AS v
    WHERE v.id = NEW.verification_id AND v.task_id = NEW.id
      AND v.run_id = NEW.last_run_id AND v.state = 'passed')
BEGIN
    SELECT RAISE(ABORT, 'a task becomes success only through a passed verification of its last run');
END""",
)

_V1_TASK_COLUMNS = "id, title, worktree, state, created_at, updated_at, last_run_id, run_count"
_V1_RUN_COLUMNS = (
    "id, task_id, worktree, model, argv_json, state, reason, exit_code, exit_signal, host, runner_pid, "
    "child_pid, child_pgid, child_start, started_at, started_epoch, ended_at, duration_s, run_dir, "
    "evidence_sha256, head_before, head_after, new_commits"
)


class OpsError(Exception):
    """Base class for refused or failed ops operations."""


class StoreBusy(OpsError):
    """The single-writer lock stayed taken for longer than the lock timeout."""


class NotFound(OpsError):
    """A task, run or notification id does not exist."""


class Conflict(OpsError):
    """The request contradicts recorded state (duplicate dispatch, wrong model, ...)."""


def default_state_dir() -> Path:
    env = os.environ.get(STATE_DIR_ENV)
    if env:
        return Path(env).expanduser()
    return Path.home() / ".local" / "state" / "agentcap" / "ops"


def iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat(timespec="milliseconds")


def _check_id(value: Any, what: str) -> str:
    if not isinstance(value, str) or not _ID_RE.match(value):
        raise ValueError(f"{what} must be 1-128 letters, digits, '.', '_', ':' or '-'")
    return value


def _check_model(value: Any) -> str:
    if not isinstance(value, str) or not _MODEL_RE.match(value):
        raise ValueError("model must be 1-128 letters, digits or . _ : / @ + -")
    return value


def _check_worktree(value: Any) -> str:
    if not isinstance(value, str) or not os.path.isabs(value):
        raise ValueError("worktree must be an absolute path")
    return os.path.normpath(value)


def _check_text(value: Any, what: str, limit: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{what} must not be empty")
    if len(value) > limit:
        raise ValueError(f"{what} is longer than {limit} characters")
    return value


def _check_shas(values: Iterable[Any] | None) -> list[str]:
    """Full lowercase commit ids, deduplicated in order; anything else is dropped."""

    shas: list[str] = []
    for value in values or ():
        sha = str(value).strip().lower()
        if _SHA_RE.match(sha) and sha not in shas:
            shas.append(sha)
    return shas


def _task_spec(
    task_id: Any,
    title: Any,
    worktree: Any,
    depends_on: Iterable[Any] | None = (),
    project: Any = None,
    requires_commit: Any = True,
) -> dict[str, Any]:
    task_id = _check_id(task_id, "task id")
    deps: list[str] = []
    if isinstance(depends_on, str):
        raise ValueError("depends_on must be a list of task ids")
    for dep in depends_on or ():
        dep = _check_id(dep, "dependency id")
        if dep == task_id:
            raise ValueError(f"task {task_id} cannot depend on itself")
        if dep not in deps:
            deps.append(dep)
    if len(deps) > MAX_DEPENDENCIES:
        raise ValueError(f"a task may depend on at most {MAX_DEPENDENCIES} tasks")
    if not isinstance(requires_commit, bool):
        raise ValueError("requires_commit must be true or false")
    return {
        "id": task_id,
        "title": _check_text(title, "title", 500),
        "worktree": _check_worktree(worktree),
        "depends_on": deps,
        "project": None if project is None else _check_id(project, "project"),
        "requires_commit": requires_commit,
    }


def _dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)).fetchone() is not None


@contextmanager
def _transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    conn.execute("COMMIT")


class OpsStore:
    """Tasks, runs, verifications, commits, signals, active models and the notification outbox."""

    def __init__(
        self,
        state_dir: str | os.PathLike[str] | None = None,
        *,
        clock: Callable[[], float] | None = None,
        lock_timeout: float = 30.0,
    ) -> None:
        base = Path(state_dir).expanduser() if state_dir is not None else default_state_dir()
        self.state_dir = Path(os.path.abspath(base))
        self.clock = clock or time.time
        self.lock_timeout = float(lock_timeout)
        self.db_path = self.state_dir / "ops.sqlite3"
        self.runs_dir = self.state_dir / "runs"
        self.verifications_dir = self.state_dir / "verifications"
        self.locks_dir = self.state_dir / "locks"
        self.writer_lock_path = self.state_dir / "ops.lock"
        self.delivery_lock_path = self.state_dir / "deliver.lock"
        self._writer = FileLock(self.writer_lock_path, label="ops store writer")
        self._thread_lock = threading.Lock()
        ensure_private_dir(self.state_dir)
        self._initialise()

    # ------------------------------------------------------------ plumbing
    def worktree_lock_path(self, worktree: str) -> Path:
        digest = hashlib.sha256(_check_worktree(worktree).encode("utf-8")).hexdigest()[:20]
        return self.locks_dir / f"worktree-{digest}.lock"

    def _acquire_writer(self) -> None:
        try:
            self._writer.acquire(timeout=self.lock_timeout)
        except LockBusy as exc:
            raise StoreBusy(f"ops store is busy: {exc}") from None

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=self.lock_timeout, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute(f"PRAGMA busy_timeout={int(self.lock_timeout * 1000)}")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA synchronous=FULL")
        return conn

    def _initialise(self) -> None:
        with self._thread_lock:
            self._acquire_writer()
            try:
                # Create the file 0600 (never truncating an existing store) before
                # SQLite opens it; WAL/SHM files inherit the database's mode.
                os.close(open_private(self.db_path, os.O_RDWR | os.O_CREAT))
                try:
                    os.chmod(self.db_path, 0o600)
                except OSError:
                    pass
                conn = self._connect()
                try:
                    conn.execute("PRAGMA journal_mode=WAL")
                    version = conn.execute("PRAGMA user_version").fetchone()[0]
                    if version > SCHEMA_VERSION:
                        raise OpsError(
                            f"{self.db_path} uses ops schema {version}; this AgentCap only "
                            f"understands schema {SCHEMA_VERSION} (upgrade AgentCap)"
                        )
                    if version == 0 and _has_table(conn, "tasks"):
                        version = 1  # written by schema-1 code that stopped before stamping it
                    if version == 1:
                        self._migrate_from_1(conn)
                    else:
                        with _transaction(conn):
                            for statement in _SCHEMA_STATEMENTS:
                                conn.execute(statement)
                            if version < SCHEMA_VERSION:
                                conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
                finally:
                    conn.close()
            finally:
                self._writer.release()

    def _older_runner_active(self, conn: sqlite3.Connection) -> str | None:
        """Worktree of a run whose (schema-1) runner still holds its lock, if any."""

        if not _has_table(conn, "runs"):
            return None
        for row in conn.execute("SELECT DISTINCT worktree FROM runs WHERE state = 'running'").fetchall():
            try:
                probe = FileLock(self.worktree_lock_path(row["worktree"]), label="ops schema upgrade")
            except ValueError:
                continue
            try:
                probe.acquire(timeout=0.0)
            except LockBusy:
                return row["worktree"]
            probe.release()
        return None

    def _migrate_from_1(self, conn: sqlite3.Connection) -> None:
        """Schema 1 -> 2: split run and task success; add dependencies, verifications and commits.

        A schema-1 ``success`` only meant "the process exited 0": those runs
        become ``completed`` and their tasks ``unverified`` (they need ``task
        verify`` now).  Everything else is kept as it was.  Refused (store
        busy, nothing changed) while a schema-1 runner is still running: it
        would try to finalise its run with the old states.
        """

        busy = self._older_runner_active(conn)
        if busy is not None:
            raise StoreBusy(
                f"a runner of an older AgentCap still works in {busy}; the ops store is upgraded "
                f"to schema {SCHEMA_VERSION} once it has finished"
            )
        conn.execute("PRAGMA foreign_keys=OFF")  # tables are rebuilt; checked below before COMMIT
        try:
            with _transaction(conn):
                conn.execute(_TASKS_DDL.format(name="tasks_v2"))
                conn.execute(
                    f"INSERT INTO tasks_v2 ({_V1_TASK_COLUMNS}) SELECT id, title, worktree, "
                    "CASE state WHEN 'success' THEN 'unverified' ELSE state END, "
                    "created_at, updated_at, last_run_id, run_count FROM tasks ORDER BY rowid"
                )
                conn.execute("DROP TABLE tasks")
                conn.execute("ALTER TABLE tasks_v2 RENAME TO tasks")
                conn.execute(_RUNS_DDL.format(name="runs_v2"))
                conn.execute(
                    f"INSERT INTO runs_v2 ({_V1_RUN_COLUMNS}, commits_recorded) SELECT "
                    "id, task_id, worktree, model, argv_json, "
                    "CASE state WHEN 'success' THEN 'completed' ELSE state END, "
                    "reason, exit_code, exit_signal, host, runner_pid, child_pid, child_pgid, child_start, "
                    "started_at, started_epoch, ended_at, duration_s, run_dir, evidence_sha256, "
                    "head_before, head_after, new_commits, "
                    "CASE state WHEN 'running' THEN 0 ELSE 1 END FROM runs ORDER BY rowid"
                )
                conn.execute("DROP TABLE runs")
                conn.execute("ALTER TABLE runs_v2 RENAME TO runs")
                for statement in _SCHEMA_STATEMENTS:
                    conn.execute(statement)
                if conn.execute("PRAGMA foreign_key_check").fetchall():
                    raise OpsError(
                        f"{self.db_path}: the foreign key check failed while upgrading ops schema 1 "
                        f"to {SCHEMA_VERSION}; nothing was changed"
                    )
                conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        finally:
            conn.execute("PRAGMA foreign_keys=ON")

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        """One exclusive cross-process write transaction (not re-entrant)."""

        with self._thread_lock:
            self._acquire_writer()
            try:
                conn = self._connect()
                try:
                    with _transaction(conn):
                        yield conn
                finally:
                    conn.close()
            finally:
                self._writer.release()

    @contextmanager
    def _read(self) -> Iterator[sqlite3.Connection]:
        conn = self._connect()
        try:
            yield conn
        finally:
            conn.close()

    def _stamp(self) -> tuple[float, str]:
        now = self.clock()
        return now, iso(now)

    def _evidence_paths(self, directory: str | None, names: Sequence[str]) -> list[str]:
        """Existing evidence files of a run/verification, relative to the state directory."""

        if not directory:
            return []
        paths = []
        for name in names:
            path = os.path.join(directory, name)
            if os.path.isfile(path):
                paths.append(os.path.relpath(path, self.state_dir))
        return paths

    # --------------------------------------------------------------- tasks
    def add_task(
        self,
        task_id: str,
        title: str,
        worktree: str,
        *,
        depends_on: Sequence[str] = (),
        project: str | None = None,
        requires_commit: bool = True,
    ) -> tuple[dict[str, Any], bool]:
        """Record a dispatched task; an identical re-dispatch returns ``(task, False)``.

        ``depends_on`` names tasks that must be ``success`` (verified) before
        this one may run; they must already exist, so dependencies can never
        form a cycle.
        """

        spec = _task_spec(task_id, title, worktree, depends_on, project, requires_commit)
        _, now = self._stamp()
        with self._write() as conn:
            return self._put_task(conn, spec, now)

    def add_tasks(self, specs: Sequence[Mapping[str, Any]]) -> list[tuple[dict[str, Any], bool]]:
        """Record several tasks in order, in one transaction: all of them or none.

        Each spec has ``id``, ``title``, ``worktree`` and optionally
        ``depends_on``, ``project`` and ``requires_commit``; a task may depend
        on tasks recorded before it, in the store or earlier in ``specs``.
        """

        checked = [
            _task_spec(
                spec.get("id"), spec.get("title"), spec.get("worktree"), spec.get("depends_on", ()),
                spec.get("project"), spec.get("requires_commit", True),
            )
            for spec in specs
        ]
        _, now = self._stamp()
        with self._write() as conn:
            return [self._put_task(conn, spec, now) for spec in checked]

    @staticmethod
    def _put_task(conn: sqlite3.Connection, spec: Mapping[str, Any], now: str) -> tuple[dict[str, Any], bool]:
        row = conn.execute("SELECT * FROM tasks WHERE id = ?", (spec["id"],)).fetchone()
        if row is not None:
            same = (
                row["title"] == spec["title"]
                and row["worktree"] == spec["worktree"]
                and json.loads(row["depends_on_json"] or "[]") == list(spec["depends_on"])
                and row["project"] == spec["project"]
                and bool(row["requires_commit"]) == spec["requires_commit"]
            )
            if not same:
                raise Conflict(
                    f"task {spec['id']} already exists with a different title, worktree ({row['worktree']}), "
                    "dependencies, project or commit requirement; use a new task id"
                )
            return dict(row), False
        for dep in spec["depends_on"]:
            if conn.execute("SELECT 1 FROM tasks WHERE id = ?", (dep,)).fetchone() is None:
                raise NotFound(f"task {spec['id']} depends on {dep}, which does not exist; add {dep} first")
        conn.execute(
            "INSERT INTO tasks (id, title, worktree, project, depends_on_json, requires_commit, state, "
            "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, 'queued', ?, ?)",
            (spec["id"], spec["title"], spec["worktree"], spec["project"], json.dumps(list(spec["depends_on"])),
             int(spec["requires_commit"]), now, now),
        )
        return dict(conn.execute("SELECT * FROM tasks WHERE id = ?", (spec["id"],)).fetchone()), True

    def get_task(self, task_id: str) -> dict[str, Any] | None:
        with self._read() as conn:
            return _dict(conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone())

    def list_tasks(self) -> list[dict[str, Any]]:
        """All tasks in the order they were recorded (dependencies before dependants)."""

        with self._read() as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM tasks ORDER BY created_at, rowid")]

    @staticmethod
    def _unmet_dependencies(conn: sqlite3.Connection, task: Mapping[str, Any]) -> list[dict[str, Any]]:
        waiting = []
        for dep in json.loads(task["depends_on_json"] or "[]"):
            row = conn.execute("SELECT state FROM tasks WHERE id = ?", (dep,)).fetchone()
            state = row["state"] if row is not None else "missing"
            if state != "success":
                waiting.append({"id": dep, "state": state})
        return waiting

    def waiting_for(self, task_id: str) -> list[dict[str, Any]]:
        """Dependencies of ``task_id`` that are not verified yet (``[{"id", "state"}]``)."""

        with self._read() as conn:
            task = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
            if task is None:
                raise NotFound(f"task {task_id} does not exist")
            return self._unmet_dependencies(conn, task)

    # -------------------------------------------------------------- models
    @staticmethod
    def _put_model(conn, worktree, model, state, run_id, set_by, now, note) -> None:
        conn.execute(
            "INSERT OR REPLACE INTO models (worktree, model, state, run_id, set_by, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (worktree, model, state, run_id, set_by, now),
        )
        conn.execute(
            "INSERT INTO model_history (worktree, model, state, run_id, note, at) VALUES (?, ?, ?, ?, ?, ?)",
            (worktree, model, state, run_id, note, now),
        )

    def set_model(self, worktree: str, model: str, *, note: str | None = None) -> dict[str, Any]:
        """Explicitly make ``model`` the active model of ``worktree`` (state ``ready``)."""

        worktree = _check_worktree(worktree)
        model = _check_model(model)
        _, now = self._stamp()
        with self._write() as conn:
            busy = conn.execute(
                "SELECT id FROM runs WHERE worktree = ? AND state = 'running'", (worktree,)
            ).fetchone()
            if busy is not None:
                raise Conflict(f"run {busy['id']} is still running in {worktree}; the active model cannot change mid-run")
            self._put_model(conn, worktree, model, "ready", None, "coordinator", now, note or "set explicitly")
            return dict(conn.execute("SELECT * FROM models WHERE worktree = ?", (worktree,)).fetchone())

    def get_model(self, worktree: str) -> dict[str, Any] | None:
        with self._read() as conn:
            return _dict(conn.execute("SELECT * FROM models WHERE worktree = ?", (_check_worktree(worktree),)).fetchone())

    def list_models(self) -> list[dict[str, Any]]:
        with self._read() as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM models ORDER BY worktree")]

    def model_history(self, worktree: str) -> list[dict[str, Any]]:
        with self._read() as conn:
            return [
                dict(r)
                for r in conn.execute(
                    "SELECT * FROM model_history WHERE worktree = ? ORDER BY id", (_check_worktree(worktree),)
                )
            ]

    # ---------------------------------------------------------------- runs
    def start_run(
        self,
        *,
        task_id: str,
        model: str,
        argv: Sequence[str],
        run_id: str,
        run_dir: str,
        head_before: str | None = None,
        rerun: bool = False,
        repo: str | None = None,
        branch: str | None = None,
    ) -> dict[str, Any]:
        """Record a new running run; refuses duplicates, unmet dependencies and implicit model switches."""

        run_id = _check_id(run_id, "run id")
        model = _check_model(model)
        argv_json = json.dumps([str(part) for part in argv])
        now_epoch, now = self._stamp()
        with self._write() as conn:
            task = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
            if task is None:
                raise NotFound(f"task {task_id} does not exist; add it first")
            worktree = task["worktree"]
            if task["state"] == "running":
                raise Conflict(f"task {task_id} is already running (run {task['last_run_id']})")
            if task["state"] == "success" and not rerun:
                raise Conflict(
                    f"task {task_id} was verified after run {task['last_run_id']}; pass --rerun to run it again"
                )
            if task["state"] == "unverified" and not rerun:
                raise Conflict(
                    f"task {task_id} completed run {task['last_run_id']} and waits for its independent "
                    "verification (`task verify`); pass --rerun to run it again instead"
                )
            waiting = self._unmet_dependencies(conn, task)
            if waiting:
                raise Conflict(
                    f"task {task_id} waits for "
                    + ", ".join(f"{dep['id']} ({dep['state']})" for dep in waiting)
                    + ": a task starts only after every task it depends on is verified (success)"
                )
            busy = conn.execute(
                "SELECT id, task_id FROM runs WHERE worktree = ? AND state = 'running'", (worktree,)
            ).fetchone()
            if busy is not None:
                raise Conflict(f"{worktree} already has a running run {busy['id']} (task {busy['task_id']})")
            active = conn.execute("SELECT * FROM models WHERE worktree = ?", (worktree,)).fetchone()
            if active is not None and active["model"] != model:
                raise Conflict(
                    f"the active model for {worktree} is {active['model']} (state {active['state']}); "
                    f"switch explicitly with `model set` before running {model}"
                )
            try:
                conn.execute(
                    "INSERT INTO runs (id, task_id, worktree, model, argv_json, state, host, runner_pid, "
                    "started_at, started_epoch, run_dir, head_before, repo, branch) "
                    "VALUES (?, ?, ?, ?, ?, 'running', ?, ?, ?, ?, ?, ?, ?, ?)",
                    (run_id, task_id, worktree, model, argv_json, socket.gethostname(), os.getpid(),
                     now, now_epoch, run_dir, head_before, repo, branch),
                )
            except sqlite3.IntegrityError as exc:
                raise Conflict(f"run {run_id} could not be recorded: {exc}") from None
            # A new run makes any earlier verification history, not the current state.
            conn.execute(
                "UPDATE tasks SET state = 'running', last_run_id = ?, run_count = run_count + 1, "
                "verification_id = NULL, verified_at = NULL, updated_at = ? WHERE id = ?",
                (run_id, now, task_id),
            )
            set_by = active["set_by"] if active is not None else "first run"
            self._put_model(conn, worktree, model, "running", run_id, set_by, now, f"run {run_id} started")
            return dict(conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone())

    def attach_child(self, run_id: str, *, pid: int, pgid: int, start: int | None) -> None:
        """Remember the model process group so recovery can tell if it outlived the runner."""

        with self._write() as conn:
            conn.execute(
                "UPDATE runs SET child_pid = ?, child_pgid = ?, child_start = ? WHERE id = ? AND state = 'running'",
                (pid, pgid, start, run_id),
            )

    def record_exit(self, run_id: str, exit_code: int | None, exit_signal: str | None) -> None:
        """Persist the kernel-reported exit status as soon as it is known."""

        with self._write() as conn:
            conn.execute(
                "UPDATE runs SET exit_code = ?, exit_signal = ? WHERE id = ? AND state = 'running'",
                (exit_code, exit_signal, run_id),
            )

    def _signal_counts(self, conn: sqlite3.Connection, run_id: str) -> dict[str, int]:
        counts = {"permission": 0, "quota": 0}
        for row in conn.execute("SELECT kind, COUNT(*) AS n FROM signals WHERE run_id = ? GROUP BY kind", (run_id,)):
            counts[row["kind"]] = row["n"]
        return counts

    def _finalise(
        self,
        conn: sqlite3.Connection,
        run_id: str,
        state: str,
        now: str,
        now_epoch: float,
        commits: Sequence[str] = (),
    ) -> dict[str, Any]:
        """Task/model state and the single final notification, inside the caller's transaction."""

        run = dict(conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone())
        conn.execute(
            "UPDATE tasks SET state = ?, updated_at = ? WHERE id = ? AND last_run_id = ?",
            (TASK_STATE_AFTER[state], now, run["task_id"], run_id),
        )
        active = conn.execute("SELECT * FROM models WHERE worktree = ?", (run["worktree"],)).fetchone()
        if active is not None and active["run_id"] == run_id:
            self._put_model(
                conn, run["worktree"], active["model"], MODEL_STATE_AFTER[state], run_id,
                active["set_by"], now, f"run {run_id} {state}",
            )
        task = _dict(conn.execute("SELECT * FROM tasks WHERE id = ?", (run["task_id"],)).fetchone())
        payload = events.run_finished(
            run, task, self._signal_counts(conn, run_id),
            evidence_paths=self._evidence_paths(run["run_dir"], RUN_EVIDENCE_FILES), commits=commits,
        )
        self._enqueue(
            conn, dedup_key=f"run:{run_id}:final", event="run_finished", payload=payload,
            task_id=run["task_id"], run_id=run_id, now_epoch=now_epoch,
        )
        return run

    def _record_commits(
        self,
        conn: sqlite3.Connection,
        run: Mapping[str, Any],
        shas: Sequence[str],
        *,
        source: str,
        now: str,
        now_epoch: float,
        verification: Mapping[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Remember each meaningful commit once (by hash) and queue one ``commit`` notification for it.

        Called only once the model of ``run`` has exited (run finished,
        recovered with its model gone, or verified), never while it works, so
        a commit can never trigger anything while its model is still running.
        A commit seen again (another run, a verification) only bumps the
        ``duplicates`` count of its existing notification.
        """

        if not shas:
            return []
        task = _dict(conn.execute("SELECT * FROM tasks WHERE id = ?", (run["task_id"],)).fetchone())
        if verification is not None:
            repo, branch = verification.get("repo"), verification.get("branch")
            tests = events.tests_summary(verification)
            paths = self._evidence_paths(verification["evidence_dir"], VERIFICATION_EVIDENCE_FILES)
        else:
            repo, branch = run.get("repo"), run.get("branch")
            tests = None
            paths = self._evidence_paths(run["run_dir"], RUN_EVIDENCE_FILES)
        recorded = []
        for sha in shas:
            conn.execute(
                "INSERT OR IGNORE INTO commits (sha, task_id, run_id, worktree, repo, branch, source, first_seen_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (sha, run["task_id"], run["id"], run["worktree"], repo, branch, source, now),
            )
            if verification is not None and verification["state"] == "passed":
                conn.execute(
                    "UPDATE commits SET verification_id = ?, verified_at = ? WHERE sha = ? AND verification_id IS NULL",
                    (verification["id"], now, sha),
                )
            row = dict(conn.execute("SELECT * FROM commits WHERE sha = ?", (sha,)).fetchone())
            payload = events.commit_recorded(row, run, task, tests=tests, evidence_paths=paths)
            self._enqueue(
                conn, dedup_key=f"commit:{sha}", event="commit", payload=payload,
                task_id=row["task_id"], run_id=row["run_id"], now_epoch=now_epoch,
            )
            recorded.append(row)
        return recorded

    def finish_run(
        self,
        run_id: str,
        *,
        state: str,
        reason: str,
        exit_code: int | None,
        exit_signal: str | None = None,
        duration_s: float | None = None,
        evidence_sha256: str | None = None,
        head_after: str | None = None,
        new_commits: int = 0,
        commits: Sequence[str] = (),
        repo: str | None = None,
        branch: str | None = None,
    ) -> tuple[dict[str, Any], bool]:
        """Finalise a running run once; returns ``(run, False)`` if it was already final.

        ``commits`` are the meaningful commits git showed for the run; each is
        recorded and queued once, after the run's own ``run_finished``.
        """

        if state not in RUN_FINAL_STATES:
            raise ValueError(f"final run state must be one of {', '.join(RUN_FINAL_STATES)}")
        shas = _check_shas(commits)
        now_epoch, now = self._stamp()
        with self._write() as conn:
            run = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
            if run is None:
                raise NotFound(f"run {run_id} does not exist")
            if run["state"] != "running":
                return dict(run), False
            if duration_s is None:
                duration_s = round(max(0.0, now_epoch - run["started_epoch"]), 3)
            conn.execute(
                "UPDATE runs SET state = ?, reason = ?, exit_code = ?, exit_signal = ?, ended_at = ?, "
                "duration_s = ?, evidence_sha256 = ?, head_after = ?, new_commits = ?, "
                "repo = COALESCE(?, repo), branch = COALESCE(?, branch), commits_recorded = 1 WHERE id = ?",
                (state, reason, exit_code, exit_signal, now, duration_s, evidence_sha256, head_after,
                 int(new_commits), repo, branch, run_id),
            )
            final = self._finalise(conn, run_id, state, now, now_epoch, commits=shas)
            self._record_commits(conn, final, shas, source="run", now=now, now_epoch=now_epoch)
            return final, True

    def interrupt_run(
        self, run_id: str, *, reason: str, scan: Mapping[str, Any] | None = None
    ) -> tuple[dict[str, Any], bool]:
        """Mark a run whose runner is gone; keeps (never invents) a recorded exit code.

        ``scan`` (``head_after``, ``new_commits``, ``commits``, ``repo``,
        ``branch``) is what git shows now; give it only when the run's model
        processes are gone.  Without it the run's commits are recorded later
        (:meth:`record_run_commits`).
        """

        now_epoch, now = self._stamp()
        shas = _check_shas(scan.get("commits")) if scan is not None else []
        with self._write() as conn:
            run = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
            if run is None:
                raise NotFound(f"run {run_id} does not exist")
            if run["state"] != "running":
                return dict(run), False
            conn.execute(
                "UPDATE runs SET state = 'interrupted', reason = ?, ended_at = ?, duration_s = ? WHERE id = ?",
                (reason, now, round(max(0.0, now_epoch - run["started_epoch"]), 3), run_id),
            )
            if scan is not None:
                self._apply_scan(conn, run_id, scan)
            final = self._finalise(conn, run_id, "interrupted", now, now_epoch, commits=shas)
            self._record_commits(conn, final, shas, source="recovery", now=now, now_epoch=now_epoch)
            return final, True

    @staticmethod
    def _apply_scan(conn: sqlite3.Connection, run_id: str, scan: Mapping[str, Any]) -> None:
        conn.execute(
            "UPDATE runs SET head_after = COALESCE(head_after, ?), new_commits = ?, repo = COALESCE(?, repo), "
            "branch = COALESCE(?, branch), commits_recorded = 1 WHERE id = ?",
            (scan.get("head_after"), int(scan.get("new_commits") or 0), scan.get("repo"), scan.get("branch"),
             run_id),
        )

    def record_run_commits(self, run_id: str, *, scan: Mapping[str, Any]) -> tuple[list[str], bool]:
        """Record the commits of an interrupted run once its model processes are gone (once per run)."""

        shas = _check_shas(scan.get("commits"))
        now_epoch, now = self._stamp()
        with self._write() as conn:
            run = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
            if run is None:
                raise NotFound(f"run {run_id} does not exist")
            if run["state"] != "interrupted" or run["commits_recorded"]:
                return [], False
            self._apply_scan(conn, run_id, scan)
            final = dict(conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone())
            self._record_commits(conn, final, shas, source="recovery", now=now, now_epoch=now_epoch)
            return shas, True

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        with self._read() as conn:
            return _dict(conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone())

    def list_runs(
        self,
        *,
        task_id: str | None = None,
        worktree: str | None = None,
        state: str | None = None,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        clauses, params = [], []
        if task_id is not None:
            clauses.append("task_id = ?")
            params.append(task_id)
        if worktree is not None:
            clauses.append("worktree = ?")
            params.append(_check_worktree(worktree))
        if state is not None:
            clauses.append("state = ?")
            params.append(state)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._read() as conn:
            return [
                dict(r)
                for r in conn.execute(
                    f"SELECT * FROM runs {where} ORDER BY started_epoch, id LIMIT ?", (*params, int(limit))
                )
            ]

    def running_runs(self, worktree: str | None = None) -> list[dict[str, Any]]:
        return self.list_runs(worktree=worktree, state="running")

    def runs_awaiting_commit_scan(self, worktree: str | None = None) -> list[dict[str, Any]]:
        """Interrupted runs whose commits were not recorded yet (their model was still alive)."""

        params: list[Any] = []
        extra = ""
        if worktree is not None:
            extra = "AND worktree = ?"
            params.append(_check_worktree(worktree))
        with self._read() as conn:
            return [
                dict(r)
                for r in conn.execute(
                    f"SELECT * FROM runs WHERE state = 'interrupted' AND commits_recorded = 0 {extra} "
                    "ORDER BY started_epoch, id",
                    params,
                )
            ]

    def orphan_candidates(self, worktree: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
        """Recent running/interrupted runs whose model process group might still exist."""

        params: list[Any] = []
        extra = ""
        if worktree is not None:
            extra = "AND worktree = ?"
            params.append(_check_worktree(worktree))
        with self._read() as conn:
            return [
                dict(r)
                for r in conn.execute(
                    "SELECT * FROM runs WHERE state IN ('running', 'interrupted') AND child_pgid IS NOT NULL "
                    f"{extra} ORDER BY started_epoch DESC LIMIT ?",
                    (*params, int(limit)),
                )
            ]

    # ------------------------------------------------------- verification
    def record_verification(
        self,
        *,
        task_id: str,
        run_id: str,
        passed: bool,
        reason: str,
        verified_by: str,
        test_argv: Sequence[str],
        test_exit_code: int | None,
        evidence_dir: str,
        evidence_sha256: str,
        test_exit_signal: str | None = None,
        counts: Mapping[str, int] | None = None,
        duration_s: float | None = None,
        head: str | None = None,
        repo: str | None = None,
        branch: str | None = None,
        commits: Sequence[str] = (),
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Record the coordinator's independent check of a completed run.

        Passed: the task becomes ``success``.  Failed: ``failure`` (a rerun is
        needed).  Either way the verification, its evidence and its
        ``task_verified`` notification are kept, and ``commits`` (those that
        passed the commit checks) are recorded and queued once by hash.
        """

        shas = _check_shas(commits)
        reason = _check_text(reason, "verification reason", 2000)
        verified_by = _check_text(verified_by, "verified by", 100)
        counts = dict(counts or {})
        state = "passed" if passed else "failed"
        now_epoch, now = self._stamp()
        with self._write() as conn:
            task = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
            if task is None:
                raise NotFound(f"task {task_id} does not exist")
            run = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
            if run is None or run["task_id"] != task_id:
                raise NotFound(f"run {run_id} of task {task_id} does not exist")
            if task["state"] != "unverified" or task["last_run_id"] != run_id or run["state"] != "completed":
                raise Conflict(
                    f"task {task_id} is {task['state']} (last run {task['last_run_id']}); only the completed "
                    "last run of an unverified task can be verified"
                )
            if passed and task["requires_commit"] and not shas:
                raise ValueError(f"task {task_id} requires a commit: a passed verification needs a verified commit")
            cursor = conn.execute(
                "INSERT INTO verifications (task_id, run_id, state, reason, verified_by, host, test_argv_json, "
                "test_exit_code, test_exit_signal, tests_passed, tests_failed, tests_skipped, tests_errors, "
                "duration_s, head, repo, branch, commits_json, evidence_dir, evidence_sha256, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (task_id, run_id, state, reason, verified_by, socket.gethostname(),
                 json.dumps([str(part) for part in test_argv]), test_exit_code, test_exit_signal,
                 counts.get("passed"), counts.get("failed"), counts.get("skipped"), counts.get("errors"),
                 duration_s, head, repo, branch, json.dumps(shas), str(evidence_dir), evidence_sha256, now),
            )
            verification = dict(
                conn.execute("SELECT * FROM verifications WHERE id = ?", (cursor.lastrowid,)).fetchone()
            )
            if passed:
                conn.execute(
                    "UPDATE tasks SET state = 'success', verification_id = ?, verified_at = ?, updated_at = ? "
                    "WHERE id = ?",
                    (verification["id"], now, now, task_id),
                )
            else:
                conn.execute(
                    "UPDATE tasks SET state = 'failure', verification_id = ?, verified_at = NULL, updated_at = ? "
                    "WHERE id = ?",
                    (verification["id"], now, task_id),
                )
            final_task = dict(conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone())
            run_row = dict(run)
            paths = self._evidence_paths(str(evidence_dir), VERIFICATION_EVIDENCE_FILES)
            paths += self._evidence_paths(run_row["run_dir"], ("evidence.json",))
            payload = events.task_verified(verification, final_task, run_row, shas, evidence_paths=paths)
            self._enqueue(
                conn, dedup_key=f"verification:{verification['id']}", event="task_verified", payload=payload,
                task_id=task_id, run_id=run_id, now_epoch=now_epoch,
            )
            self._record_commits(
                conn, run_row, shas, source="verification", now=now, now_epoch=now_epoch, verification=verification,
            )
            return verification, final_task

    def get_verification(self, verification_id: int) -> dict[str, Any] | None:
        with self._read() as conn:
            return _dict(conn.execute("SELECT * FROM verifications WHERE id = ?", (int(verification_id),)).fetchone())

    def list_verifications(self, task_id: str | None = None) -> list[dict[str, Any]]:
        with self._read() as conn:
            if task_id is None:
                rows = conn.execute("SELECT * FROM verifications ORDER BY id")
            else:
                rows = conn.execute("SELECT * FROM verifications WHERE task_id = ? ORDER BY id", (task_id,))
            return [dict(r) for r in rows]

    def get_commit(self, sha: str) -> dict[str, Any] | None:
        with self._read() as conn:
            return _dict(conn.execute("SELECT * FROM commits WHERE sha = ?", (str(sha).lower(),)).fetchone())

    def list_commits(self, task_id: str | None = None) -> list[dict[str, Any]]:
        with self._read() as conn:
            if task_id is None:
                rows = conn.execute("SELECT * FROM commits ORDER BY first_seen_at, rowid")
            else:
                rows = conn.execute(
                    "SELECT * FROM commits WHERE task_id = ? ORDER BY first_seen_at, rowid", (task_id,)
                )
            return [dict(r) for r in rows]

    # ------------------------------------------------------------- signals
    def record_signal(
        self,
        *,
        kind: str,
        source: str,
        detail: str | None = None,
        run_id: str | None = None,
        worktree: str | None = None,
        dedup_key: str | None = None,
    ) -> tuple[dict[str, Any], bool]:
        """Record a quota/permission/info signal once; quota/permission also queue a notification."""

        if kind not in SIGNAL_KINDS:
            raise ValueError(f"signal kind must be one of {', '.join(SIGNAL_KINDS)}")
        source = _check_text(source, "signal source", 100)
        detail = None if detail is None else str(detail)[:500]
        if worktree is not None:
            worktree = _check_worktree(worktree)
        now_epoch, now = self._stamp()
        with self._write() as conn:
            run = None
            if run_id is not None:
                run = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
                if run is None:
                    raise NotFound(f"run {run_id} does not exist")
                worktree = run["worktree"]
            elif worktree is not None:
                run = conn.execute(
                    "SELECT * FROM runs WHERE worktree = ? AND state = 'running'", (worktree,)
                ).fetchone()
            else:
                raise ValueError("a signal needs a run id or a worktree")
            run_ref = run["id"] if run is not None else None
            key = dedup_key or "auto:" + hashlib.sha256(
                "|".join([kind, source, detail or "", run_ref or "", worktree]).encode("utf-8")
            ).hexdigest()[:32]
            existing = conn.execute("SELECT * FROM signals WHERE dedup_key = ?", (key,)).fetchone()
            if existing is not None:
                conn.execute(
                    "UPDATE signals SET seen_count = seen_count + 1, last_seen_at = ? WHERE id = ?",
                    (now, existing["id"]),
                )
                return dict(conn.execute("SELECT * FROM signals WHERE id = ?", (existing["id"],)).fetchone()), False
            cursor = conn.execute(
                "INSERT INTO signals (dedup_key, run_id, worktree, kind, source, detail, created_at, last_seen_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (key, run_ref, worktree, kind, source, detail, now, now),
            )
            recorded = dict(conn.execute("SELECT * FROM signals WHERE id = ?", (cursor.lastrowid,)).fetchone())
            if kind in ("permission", "quota"):
                task = None
                if run is not None:
                    model = run["model"]
                    task = _dict(conn.execute("SELECT * FROM tasks WHERE id = ?", (run["task_id"],)).fetchone())
                else:
                    active = conn.execute("SELECT model FROM models WHERE worktree = ?", (worktree,)).fetchone()
                    model = active["model"] if active is not None else None
                payload = events.signal_raised(recorded, _dict(run), model, task)
                self._enqueue(
                    conn, dedup_key=f"signal:{key}", event=payload["kind"], payload=payload,
                    task_id=run["task_id"] if run is not None else None, run_id=run_ref, now_epoch=now_epoch,
                )
            return recorded, True

    def run_signals(self, run_id: str) -> list[dict[str, Any]]:
        with self._read() as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM signals WHERE run_id = ? ORDER BY id", (run_id,))]

    # -------------------------------------------------------- notifications
    def _enqueue(
        self,
        conn: sqlite3.Connection,
        *,
        dedup_key: str,
        event: str,
        payload: dict[str, Any],
        task_id: str | None,
        run_id: str | None,
        now_epoch: float,
    ) -> tuple[dict[str, Any], bool]:
        now = iso(now_epoch)
        existing = conn.execute("SELECT * FROM notifications WHERE dedup_key = ?", (dedup_key,)).fetchone()
        if existing is not None:
            conn.execute(
                "UPDATE notifications SET duplicates = duplicates + 1, updated_at = ? WHERE id = ?",
                (now, existing["id"]),
            )
            return dict(conn.execute("SELECT * FROM notifications WHERE id = ?", (existing["id"],)).fetchone()), False
        body = dict(payload)
        body["event_id"] = events.event_id(dedup_key)
        text = json.dumps(body, sort_keys=True, separators=(",", ":"), default=str)
        if len(text.encode("utf-8")) > MAX_PAYLOAD_BYTES:
            raise ValueError("notification payload is larger than 16 KiB")
        cursor = conn.execute(
            "INSERT INTO notifications (dedup_key, event, task_id, run_id, payload_json, state, attempts, "
            "next_attempt_at, created_at, updated_at) VALUES (?, ?, ?, ?, ?, 'pending', 0, ?, ?, ?)",
            (dedup_key, event, task_id, run_id, text, now_epoch, now, now),
        )
        return dict(conn.execute("SELECT * FROM notifications WHERE id = ?", (cursor.lastrowid,)).fetchone()), True

    def enqueue(
        self,
        *,
        dedup_key: str,
        event: str,
        payload: dict[str, Any],
        task_id: str | None = None,
        run_id: str | None = None,
    ) -> tuple[dict[str, Any], bool]:
        """Queue a notification once per ``dedup_key``; repeats only bump ``duplicates``."""

        _check_text(dedup_key, "dedup key", 300)
        _check_text(event, "event", 100)
        now_epoch, _ = self._stamp()
        with self._write() as conn:
            return self._enqueue(
                conn, dedup_key=dedup_key, event=event, payload=payload,
                task_id=task_id, run_id=run_id, now_epoch=now_epoch,
            )

    def claim_next(self) -> dict[str, Any] | None:
        """Move the oldest due pending notification to ``sending`` (attempts + 1)."""

        now_epoch, now = self._stamp()
        with self._write() as conn:
            row = conn.execute(
                "SELECT id FROM notifications WHERE state = 'pending' AND next_attempt_at <= ? "
                "ORDER BY next_attempt_at, id LIMIT 1",
                (now_epoch,),
            ).fetchone()
            if row is None:
                return None
            conn.execute(
                "UPDATE notifications SET state = 'sending', attempts = attempts + 1, updated_at = ? WHERE id = ?",
                (now, row["id"]),
            )
            return dict(conn.execute("SELECT * FROM notifications WHERE id = ?", (row["id"],)).fetchone())

    def complete_delivery(
        self,
        notification_id: int,
        *,
        outcome: str,
        endpoint: str,
        http_status: int | None = None,
        error: str | None = None,
        next_attempt_at: float | None = None,
        give_up: bool = False,
    ) -> dict[str, Any]:
        """Record one attempt: delivered, back to pending (retry) or failed (kept)."""

        now_epoch, now = self._stamp()
        with self._write() as conn:
            row = conn.execute("SELECT * FROM notifications WHERE id = ?", (notification_id,)).fetchone()
            if row is None:
                raise NotFound(f"notification {notification_id} does not exist")
            if row["state"] != "sending":
                raise Conflict(f"notification {notification_id} is {row['state']}, not sending")
            conn.execute(
                "INSERT INTO delivery_attempts (notification_id, attempted_at, outcome, http_status, error, endpoint) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (notification_id, now, outcome, http_status, error, endpoint),
            )
            if outcome == "delivered":
                conn.execute(
                    "UPDATE notifications SET state = 'delivered', delivered_at = ?, last_http_status = ?, "
                    "last_error = NULL, updated_at = ? WHERE id = ?",
                    (now, http_status, now, notification_id),
                )
            elif give_up:
                conn.execute(
                    "UPDATE notifications SET state = 'failed', last_error = ?, last_http_status = ?, "
                    "updated_at = ? WHERE id = ?",
                    (error, http_status, now, notification_id),
                )
            else:
                conn.execute(
                    "UPDATE notifications SET state = 'pending', next_attempt_at = ?, last_error = ?, "
                    "last_http_status = ?, updated_at = ? WHERE id = ?",
                    (next_attempt_at if next_attempt_at is not None else now_epoch, error, http_status, now,
                     notification_id),
                )
            return dict(conn.execute("SELECT * FROM notifications WHERE id = ?", (notification_id,)).fetchone())

    def reset_sending(self) -> int:
        """Requeue claims of a deliverer that died mid-send (caller holds the delivery lock)."""

        now_epoch, now = self._stamp()
        with self._write() as conn:
            rows = conn.execute("SELECT id FROM notifications WHERE state = 'sending'").fetchall()
            for row in rows:
                conn.execute(
                    "INSERT INTO delivery_attempts (notification_id, attempted_at, outcome, http_status, error, endpoint) "
                    "VALUES (?, ?, 'interrupted', NULL, 'deliverer stopped before recording the result', 'unknown')",
                    (row["id"], now),
                )
                conn.execute(
                    "UPDATE notifications SET state = 'pending', next_attempt_at = ?, updated_at = ? WHERE id = ?",
                    (now_epoch, now, row["id"]),
                )
            return len(rows)

    def retry_failed(self, ids: Sequence[int] | None = None) -> int:
        """Give failed (dead-lettered) notifications a fresh set of attempts."""

        now_epoch, now = self._stamp()
        with self._write() as conn:
            sql = "UPDATE notifications SET state = 'pending', attempts = 0, next_attempt_at = ?, updated_at = ? WHERE state = 'failed'"
            params: list[Any] = [now_epoch, now]
            if ids:
                sql += f" AND id IN ({','.join('?' * len(ids))})"
                params.extend(int(i) for i in ids)
            return conn.execute(sql, params).rowcount

    def get_notification(self, notification_id: int) -> dict[str, Any] | None:
        with self._read() as conn:
            return _dict(conn.execute("SELECT * FROM notifications WHERE id = ?", (notification_id,)).fetchone())

    def list_notifications(self, state: str | None = None, limit: int = 500) -> list[dict[str, Any]]:
        with self._read() as conn:
            if state is None:
                rows = conn.execute("SELECT * FROM notifications ORDER BY id LIMIT ?", (int(limit),))
            else:
                rows = conn.execute(
                    "SELECT * FROM notifications WHERE state = ? ORDER BY id LIMIT ?", (state, int(limit))
                )
            return [dict(r) for r in rows]

    def delivery_attempts(self, notification_id: int) -> list[dict[str, Any]]:
        with self._read() as conn:
            return [
                dict(r)
                for r in conn.execute(
                    "SELECT * FROM delivery_attempts WHERE notification_id = ? ORDER BY id", (notification_id,)
                )
            ]

    def notification_counts(self) -> dict[str, int]:
        counts = {state: 0 for state in NOTIFY_STATES}
        with self._read() as conn:
            for row in conn.execute("SELECT state, COUNT(*) AS n FROM notifications GROUP BY state"):
                counts[row["state"]] = row["n"]
        return counts

    # ------------------------------------------------------------- summary
    def open_queue(self) -> list[dict[str, Any]]:
        """Every task that is not verified yet, in recorded order, with what it waits for."""

        with self._read() as conn:
            rows = conn.execute("SELECT * FROM tasks WHERE state != 'success' ORDER BY created_at, rowid").fetchall()
            return [
                {
                    "id": row["id"],
                    "title": row["title"],
                    "state": row["state"],
                    "depends_on": json.loads(row["depends_on_json"] or "[]"),
                    "waiting_for": self._unmet_dependencies(conn, row),
                    "requires_commit": bool(row["requires_commit"]),
                }
                for row in rows
            ]

    def summary(self) -> dict[str, Any]:
        tasks = {state: 0 for state in TASK_STATES}
        with self._read() as conn:
            for row in conn.execute("SELECT state, COUNT(*) AS n FROM tasks GROUP BY state"):
                tasks[row["state"]] = row["n"]
            running = [dict(r) for r in conn.execute("SELECT * FROM runs WHERE state = 'running' ORDER BY started_epoch")]
            recent = [dict(r) for r in conn.execute("SELECT * FROM runs ORDER BY started_epoch DESC, id DESC LIMIT 10")]
            models = [dict(r) for r in conn.execute("SELECT * FROM models ORDER BY worktree")]
            failed = [
                dict(r)
                for r in conn.execute(
                    "SELECT id, event, task_id, run_id, attempts, last_error, last_http_status, updated_at "
                    "FROM notifications WHERE state = 'failed' ORDER BY id"
                )
            ]
            oldest = conn.execute(
                "SELECT MIN(created_at) AS at FROM notifications WHERE state IN ('pending', 'sending')"
            ).fetchone()["at"]
            commits = conn.execute("SELECT COUNT(*) AS n FROM commits").fetchone()["n"]
        return {
            "state_dir": str(self.state_dir),
            "tasks": tasks,
            "open_queue": self.open_queue(),
            "running_runs": running,
            "recent_runs": recent,
            "models": models,
            "commits_recorded": commits,
            "notifications": self.notification_counts(),
            "failed_notifications": failed,
            "oldest_undelivered_at": oldest,
        }


__all__ = [
    "COMMIT_SOURCES",
    "Conflict",
    "MODEL_STATES",
    "NOTIFY_STATES",
    "NotFound",
    "OpsError",
    "OpsStore",
    "RUN_FINAL_STATES",
    "SCHEMA_VERSION",
    "SIGNAL_KINDS",
    "STATE_DIR_ENV",
    "StoreBusy",
    "TASK_STATES",
    "TASK_STATE_AFTER",
    "VERIFICATION_STATES",
    "default_state_dir",
    "iso",
]
