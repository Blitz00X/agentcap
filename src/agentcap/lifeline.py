"""Parent-death lifeline for processes AgentCap starts.

Without this, a SIGKILL to AgentCap (or an OOM kill, or a closed terminal on
some setups) leaves the guarded agent running with no cap.  On Linux the child
is started through :mod:`agentcap._lifeline_exec`, which arms
``PR_SET_PDEATHSIG`` and then ``exec``s the real command; the kernel then kills
the agent when AgentCap dies.  The parent-death signal reaches only that
process, so :class:`GroupWatchdog` (:mod:`agentcap._lifeline_watch`) also
SIGKILLs the rest of the agent's process group.  Other platforms return the
command unchanged.
"""

from __future__ import annotations

import errno
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Sequence

_SHIM = Path(__file__).with_name("_lifeline_exec.py")
_WATCH = Path(__file__).with_name("_lifeline_watch.py")


def supported() -> bool:
    """True when this platform can arm a parent-death signal."""

    return sys.platform.startswith("linux")


def guarded_argv(argv: Sequence[str], *, errfd: int | None = None) -> list[str]:
    """Return the argv to spawn so the child dies with AgentCap (Linux)."""

    command = [str(part) for part in argv]
    if not supported() or not command:
        return command
    options = ["--parent", str(os.getpid())]
    if errfd is not None:
        options += ["--errfd", str(errfd)]
    # -I: the shim sees only the standard library -- not its own package
    # directory, the user's PYTHONPATH or user site-packages.  The agent still
    # gets the unchanged environment through execvp.
    return [sys.executable, "-I", str(_SHIM), *options, "--", *command]


def _read_all(fd: int) -> bytes:
    chunks: list[bytes] = []
    while True:
        chunk = os.read(fd, 64)
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)


def _discard(child: subprocess.Popen, *, kill: bool) -> None:
    if kill:
        try:
            child.kill()
        except OSError:
            pass
    try:
        child.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass
    for stream in (child.stdin, child.stdout, child.stderr):
        if stream is not None:
            try:
                stream.close()
            except OSError:
                pass


def spawn(argv: Sequence[str], **popen_kwargs: Any) -> subprocess.Popen:
    """``subprocess.Popen(argv, ...)`` behind the lifeline, same start-failure contract.

    Popen now starts the shim, which always exists, so the shim sends an
    ``execvp`` failure (missing command, no exec permission, ...) back over a
    close-on-exec pipe.  It is raised here as the ``OSError`` subclass Popen
    raises for a direct spawn, so a typo is still reported as "could not start
    the command" and never as an agent that ran and made no requests.
    """

    command = [str(part) for part in argv]
    if not supported() or not command:
        return subprocess.Popen(command, **popen_kwargs)
    read_fd, write_fd = os.pipe()
    try:
        pass_fds = (*popen_kwargs.pop("pass_fds", ()), write_fd)
        child = subprocess.Popen(
            guarded_argv(command, errfd=write_fd), pass_fds=pass_fds, **popen_kwargs
        )
    except BaseException:
        os.close(read_fd)
        raise
    finally:
        os.close(write_fd)
    try:
        report = _read_all(read_fd)
    except BaseException:
        _discard(child, kill=True)  # never leave a command running untracked
        raise
    finally:
        os.close(read_fd)
    if not report:
        return child  # the shim exec'd the command
    _discard(child, kill=False)
    try:
        code = int(report)
    except ValueError:
        code = 0
    if code <= 0:
        code = errno.ENOEXEC
    raise OSError(code, os.strerror(code), command[0])


class GroupWatchdog:
    """SIGKILL the agent's whole process group if AgentCap dies (Linux).

    The parent-death signal only kills the process AgentCap started; this
    covers the rest of its group (a launcher's real agent, tool processes).
    :meth:`start` it before :class:`~agentcap.procstop.ProcessTreeGuard` so
    the descendant sweep's baseline excludes it, :meth:`watch` the group once
    the agent runs, and :meth:`disarm` it after cleanup.  A process that left
    the group (``setsid``) is outside its reach.  Best effort: if the watchdog
    cannot start, the parent-death signal still covers the direct child.
    """

    def __init__(self) -> None:
        self._proc: subprocess.Popen | None = None

    def start(self) -> bool:
        if not supported() or self._proc is not None:
            return False
        try:
            self._proc = subprocess.Popen(
                [sys.executable, "-I", str(_WATCH)],
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except OSError:
            self._proc = None
            return False
        return True

    def watch(self, pgid: int | None) -> None:
        if pgid is None or pgid <= 1 or pgid == os.getpgrp():
            return  # never arm against AgentCap's own group
        self._send(f"pgid {pgid}\n".encode("ascii"))

    def disarm(self) -> None:
        proc, self._proc = self._proc, None
        if proc is None:
            return
        self._send(b"done\n", proc)
        try:
            if proc.stdin is not None:
                proc.stdin.close()
        except OSError:
            pass
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()

    def _send(self, data: bytes, proc: subprocess.Popen | None = None) -> None:
        proc = proc or self._proc
        if proc is None or proc.stdin is None:
            return
        try:
            proc.stdin.write(data)
            proc.stdin.flush()
        except (OSError, ValueError):
            pass


__all__ = ["GroupWatchdog", "guarded_argv", "spawn", "supported"]
