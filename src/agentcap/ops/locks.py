"""Exclusive file locks for the ops tracker.

``flock`` locks belong to an open file description, so the kernel drops them
the moment the holder exits -- normally, by SIGKILL, OOM or power loss.  A
crashed writer therefore never leaves a stale lock behind, which is what makes
restart recovery safe: whoever can take a lock knows its previous holder is
gone.  The holder writes its pid/host/label into the file so a refused caller
can say who has it.
"""

from __future__ import annotations

import fcntl
import json
import os
import socket
import time
from pathlib import Path
from typing import Any

from .private import ensure_private_dir, open_private

_POLL_S = 0.02


class LockBusy(Exception):
    """Another live open file description holds the lock."""

    def __init__(self, path: Path, holder: dict[str, Any] | None) -> None:
        self.path = Path(path)
        self.holder = holder or {}
        pid = self.holder.get("pid", "unknown")
        label = self.holder.get("label")
        detail = f" ({label})" if label else ""
        super().__init__(f"{self.path.name} is held by pid {pid}{detail}")


def _read_holder(fd: int) -> dict[str, Any] | None:
    try:
        data = os.pread(fd, 4096, 0)
        holder = json.loads(data.decode("utf-8")) if data else None
    except (OSError, ValueError):
        return None
    return holder if isinstance(holder, dict) else None


class FileLock:
    """One exclusive ``flock`` on ``path``; reusable after :meth:`release`."""

    def __init__(self, path: str | os.PathLike[str], *, label: str = "") -> None:
        self.path = Path(path)
        self.label = label
        self._fd: int | None = None

    @property
    def held(self) -> bool:
        return self._fd is not None

    def acquire(self, timeout: float | None = 0.0) -> "FileLock":
        """Take the lock: ``0`` = try once, ``None`` = wait forever, else wait that long.

        Raises :class:`LockBusy` (with the holder's pid/label) when it stays taken.
        """

        if self._fd is not None:
            raise RuntimeError(f"{self.path} is already held through this object")
        ensure_private_dir(self.path.parent)
        # No O_TRUNC: the file may describe the current holder.  O_NOFOLLOW
        # (added by open_private) refuses a symlinked lock file.
        fd = open_private(self.path, os.O_RDWR | os.O_CREAT)
        deadline = None if timeout is None else time.monotonic() + max(0.0, timeout)
        try:
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if deadline is not None and time.monotonic() >= deadline:
                        raise LockBusy(self.path, _read_holder(fd)) from None
                    time.sleep(_POLL_S)
            info = {
                "pid": os.getpid(),
                "host": socket.gethostname(),
                "label": self.label,
                "since": time.time(),
            }
            os.ftruncate(fd, 0)
            os.pwrite(fd, json.dumps(info).encode("utf-8"), 0)
        except BaseException:
            os.close(fd)
            raise
        self._fd = fd
        return self

    def release(self) -> None:
        fd, self._fd = self._fd, None
        if fd is None:
            return
        try:
            os.ftruncate(fd, 0)
        except OSError:
            pass
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(fd)

    def __enter__(self) -> "FileLock":
        return self.acquire(timeout=0.0)

    def __exit__(self, *exc: object) -> None:
        self.release()


__all__ = ["FileLock", "LockBusy"]
