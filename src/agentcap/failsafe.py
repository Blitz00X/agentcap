"""Fail-closed guards for AgentCap quota runs.

Two independent safety nets live here: :class:`JobLock` stops two quota jobs
from fighting over the same account-wide percentage, and :class:`SchemaWatch`
stops a run whose output the adapter can no longer parse.  Both refuse to
continue silently -- the caller is told why instead of being left guessing.
"""

from __future__ import annotations

import fcntl
import os
from pathlib import Path

from .private import ensure_private_dir, open_private

EXIT_GUARD_FAILED = 5

_PARALLEL_ENV = "AGENTCAP_ALLOW_PARALLEL"


class GuardFailure(Exception):
    """A guard refused to let the run continue."""


class JobLock:
    """Exclusive cross-process lock for a single quota job."""

    def __init__(self, path: Path) -> None:
        """Remember the lock file location; nothing is touched yet."""
        self.path = Path(path)
        self._fh = None

    def acquire(self) -> None:
        """Take the lock, or raise :class:`GuardFailure` if another job holds it."""
        if os.environ.get(_PARALLEL_ENV) == "1":
            return
        ensure_private_dir(self.path.parent)
        fd = open_private(self.path, os.O_RDWR | os.O_CREAT)
        fh = os.fdopen(fd, "a+")
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            pid = self._read_pid(fh)
            fh.close()
            raise GuardFailure(
                f"another AgentCap quota job is already running (pid {pid}); "
                "quota % is account-wide, so two jobs cannot be guarded "
                "separately. Wait for it to finish or set "
                f"{_PARALLEL_ENV}=1 to run without this lock."
            ) from None
        except OSError:
            fh.close()
            raise
        self._fh = fh
        fh.seek(0)
        fh.truncate()
        fh.write(str(os.getpid()))
        fh.flush()

    @staticmethod
    def _read_pid(fh) -> str:
        """Return the pid recorded by the holder, or ``'unknown'``."""
        try:
            fh.seek(0)
            text = fh.read().strip()
        except OSError:
            return "unknown"
        return text or "unknown"

    def release(self) -> None:
        """Unlock and close the lock file, ignoring any failure."""
        fh, self._fh = self._fh, None
        if fh is None:
            return
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        try:
            fh.close()
        except OSError:
            pass

    def __enter__(self) -> "JobLock":
        """Acquire the lock and return it."""
        self.acquire()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        """Release the lock on the way out, whatever happened."""
        self.release()


class SchemaWatch:
    """Trip when a harness never emits a log line the adapter understands."""

    def __init__(self, harness: str, limit: int = 25) -> None:
        """Set the harness name and how many silent lines are tolerated."""
        self.harness = harness
        self.limit = limit
        self.tripped: str | None = None
        self._unrecognised = 0
        self._ever_recognised = False

    def feed(self, line: str, recognised: bool) -> str | None:
        """Account for one stdout line and return a stop message if tripping."""
        if recognised:
            self._ever_recognised = True
            self._unrecognised = 0
            return None
        if self._ever_recognised:
            return None
        self._unrecognised += 1
        if self._unrecognised >= self.limit and self.tripped is None:
            self.tripped = (
                f"{self.harness} output did not match any known log schema "
                f"after {self.limit} lines (harness updated?); the guard cannot "
                "see this run, stopping it (fail-closed)."
            )
            return self.tripped
        return None
