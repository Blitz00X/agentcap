"""Process-group stop/reap helpers.

Escalates a signal ladder over a whole process group (for example a shell
that spawned helpers in its own session) so no stray grandchild outlives the
run.  The caller's own process group is never signalled: without a usable
group id these helpers fall back to the leader process alone.
"""

from __future__ import annotations

import os
import signal
import subprocess
import time

POLL_INTERVAL = 0.05


def _signal_name(sig: int) -> str:
    """Return the conventional name for ``sig`` (e.g. ``'SIGTERM'``)."""
    try:
        return signal.Signals(sig).name
    except ValueError:
        return str(sig)


def group_alive(pgid: int) -> bool:
    """Return True while any process is still a member of process group ``pgid``.

    Signal 0 performs the permission and existence checks without delivering
    anything.  A missing group is reported as dead; a group we may not signal
    is assumed to be alive, since it clearly exists.
    """
    if pgid is None or pgid <= 0:
        # pgid 0 would mean "the caller's own group"; never probe it.
        return False
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _wait_gone(
    child: subprocess.Popen, pgid: int | None, use_group: bool, timeout: float
) -> bool:
    """Wait until the leader exited and the group is empty, or ``timeout`` passes."""
    deadline = time.monotonic() + max(0.0, timeout)
    while True:
        leader_done = child.poll() is not None
        members_done = not (use_group and group_alive(pgid))
        if leader_done and members_done:
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(POLL_INTERVAL)


def _wait_group_gone(pgid: int, timeout: float) -> bool:
    deadline = time.monotonic() + max(0.0, timeout)
    while True:
        if not group_alive(pgid):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(POLL_INTERVAL)


def stop_tree(
    child: subprocess.Popen,
    pgid: int | None,
    *,
    int_timeout: float = 5.0,
    term_timeout: float = 3.0,
    kill_timeout: float = 2.0,
    first_signal: int = signal.SIGINT,
) -> str:
    """Escalate ``first_signal`` -> SIGTERM -> SIGKILL over a process group.

    Each step waits up to its own timeout for both the leader to exit and the
    group to empty, then moves on: a dead leader with live grandchildren keeps
    escalating.  The caller's own group is never signalled -- when ``pgid`` is
    missing or matches it, signals go to ``child`` alone.

    Returns the name of the last signal sent, or ``'none'`` when nothing was
    alive to begin with.
    """
    own_pgid = os.getpgrp()
    use_group = pgid is not None and pgid > 0 and pgid != own_pgid

    def send(sig: int) -> None:
        if use_group:
            try:
                os.killpg(pgid, sig)
            except (ProcessLookupError, OSError):
                pass
        elif child.poll() is None:
            try:
                child.send_signal(sig)
            except (ProcessLookupError, OSError):
                pass

    def finish() -> None:
        try:
            child.wait(timeout=1)
        except subprocess.TimeoutExpired:
            pass

    nothing_alive = child.poll() is not None and not (
        use_group and group_alive(pgid)
    )
    if nothing_alive:
        finish()
        return "none"

    ladder = (
        (first_signal, int_timeout),
        (signal.SIGTERM, term_timeout),
        (signal.SIGKILL, kill_timeout),
    )
    last = "none"
    sent: list[int] = []
    for sig, timeout in ladder:
        if sig in sent:
            continue
        sent.append(sig)
        send(sig)
        last = _signal_name(sig)
        if _wait_gone(child, pgid, use_group, timeout):
            break

    finish()
    return last


def reap_group(pgid: int | None, term_timeout: float = 2.0) -> bool:
    """Clean up group members left behind after the leader exited normally.

    SIGTERMs the group, waits ``term_timeout``, then SIGKILLs whatever remains.
    Returns True if the group had survivors and had to be signalled, False if
    it was already empty (or no usable group id was given).  The caller's own
    group is never touched.
    """
    if pgid is None or pgid <= 0 or pgid == os.getpgrp():
        return False
    if not group_alive(pgid):
        return False

    try:
        os.killpg(pgid, signal.SIGTERM)
    except (ProcessLookupError, OSError):
        pass
    if _wait_group_gone(pgid, term_timeout):
        return True

    try:
        os.killpg(pgid, signal.SIGKILL)
    except (ProcessLookupError, OSError):
        pass
    _wait_group_gone(pgid, term_timeout)
    return True
