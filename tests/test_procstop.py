"""Tests for :mod:`agentcap.procstop`.

Every test drives a real process tree: a leader started with
``start_new_session=True`` (so it owns a fresh process group) which forks a
grandchild that just sleeps.  Cleanup always SIGKILLs the whole group.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time

import pytest

from agentcap.procstop import group_alive, reap_group, stop_tree

pytestmark = pytest.mark.skipif(
    os.name != "posix", reason="POSIX process groups and signals are required"
)

GRANDCHILD_SLEEP = 60
LEADER_SLEEP = 60
SPAWN_TIMEOUT = 5.0


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def _grandchild_code(ignore: tuple[str, ...]) -> str:
    """Source for the sleeping grandchild; optionally deaf to some signals.

    The ready pipe write happens *after* the dispositions are installed so the
    leader never publishes the pid to a grandchild that could still be killed
    by a signal it is supposed to ignore.
    """
    parts = ["import os", "import signal", "import sys", "import time"]
    for name in ignore:
        parts.append(f"signal.signal(signal.{name}, signal.SIG_IGN)")
    parts.append("os.write(int(sys.argv[1]), b'x')")
    parts.append(f"time.sleep({GRANDCHILD_SLEEP})")
    return "; ".join(parts)


def _leader_code(
    *,
    ignore: tuple[str, ...] = (),
    grandchild_ignore: tuple[str, ...] = (),
    exit_on_sigint: bool = False,
    linger: bool = True,
) -> str:
    """Source for the leader: prints the grandchild pid, then sleeps or exits."""
    lines = ["import os", "import signal", "import subprocess", "import sys", "import time"]
    for name in ignore:
        lines.append(f"signal.signal(signal.{name}, signal.SIG_IGN)")
    if exit_on_sigint:
        lines.append("signal.signal(signal.SIGINT, lambda *_: os._exit(0))")
    lines.append("_ready_r, _ready_w = os.pipe()")
    lines.append(
        "_gc = subprocess.Popen("
        f"[sys.executable, '-c', {_grandchild_code(grandchild_ignore)!r}, str(_ready_w)], "
        "stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, pass_fds=(_ready_w,))"
    )
    lines.append("os.close(_ready_w)")
    lines.append("os.read(_ready_r, 1)  # grandchild has installed its handlers")
    lines.append("os.close(_ready_r)")
    lines.append("print(_gc.pid, flush=True)")
    lines.append(f"time.sleep({LEADER_SLEEP})" if linger else "sys.exit(0)")
    return "\n".join(lines)


def _read_pid(proc: subprocess.Popen, timeout: float = SPAWN_TIMEOUT) -> int:
    """Read the first stdout line (the grandchild pid) without risking a hang."""
    box: dict[str, object] = {}

    def _reader() -> None:
        try:
            box["line"] = proc.stdout.readline()
        except Exception as exc:  # pragma: no cover - defensive
            box["error"] = exc

    thread = threading.Thread(target=_reader, daemon=True)
    thread.start()
    thread.join(timeout)
    if thread.is_alive():
        raise AssertionError("tree leader never printed a pid line")
    if "error" in box:
        raise AssertionError(f"reading pid line failed: {box['error']}")
    line = str(box.get("line", "")).strip()
    if not line:
        raise AssertionError("tree leader closed stdout without printing a pid")
    return int(line)


def _spawn(code: str) -> tuple[subprocess.Popen, int, int]:
    """Start ``code`` in a new session; return (leader, pgid, grandchild pid)."""
    proc = subprocess.Popen(
        [sys.executable, "-c", code],
        start_new_session=True,
        stdout=subprocess.PIPE,
        text=True,
    )
    pgid = proc.pid  # start_new_session makes the leader its own group leader
    try:
        grandchild = _read_pid(proc)
    except BaseException:
        _kill_group(proc, pgid)
        raise
    return proc, pgid, grandchild


def _kill_group(proc: subprocess.Popen, pgid: int) -> None:
    """SIGKILL the whole group, ignoring every failure, and reap the leader."""
    try:
        os.killpg(pgid, signal.SIGKILL)
    except OSError:
        pass
    try:
        proc.wait(timeout=2)
    except (subprocess.TimeoutExpired, OSError):
        pass
    try:
        if proc.stdout is not None:
            proc.stdout.close()
    except OSError:
        pass


def _proc_state(pid: int) -> str | None:
    """Process state letter from /proc, or None when /proc is unavailable."""
    try:
        with open(f"/proc/{pid}/stat", "r", encoding="utf-8") as handle:
            data = handle.read()
    except OSError:
        return None
    # The comm field may contain spaces and parentheses; split after the last ')'.
    close = data.rfind(")")
    fields = data[close + 2 :].split() if close >= 0 else []
    return fields[0] if fields else None


def pid_alive(pid: int) -> bool:
    """True only for a live, non-zombie process."""
    if pid is None or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, just not ours
    except OSError:
        return False
    return _proc_state(pid) != "Z"


def _wait_until(predicate, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while True:
        if predicate():
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.02)


def _wait_pid_dead(pid: int, timeout: float = 2.0) -> bool:
    return _wait_until(lambda: not pid_alive(pid), timeout)


def _wait_group_dead(pgid: int, timeout: float = 2.0) -> bool:
    return _wait_until(lambda: not group_alive(pgid), timeout)


# --------------------------------------------------------------------------- #
# tests
# --------------------------------------------------------------------------- #


def test_stop_tree_sigint_kills_grandchild():
    proc, pgid, grandchild = _spawn(
        _leader_code()  # default dispositions: SIGINT is fatal for both
    )
    try:
        result = stop_tree(proc, pgid)

        assert result == "SIGINT"
        assert proc.poll() is not None, "leader should have exited"
        assert _wait_pid_dead(grandchild), "grandchild survived SIGINT"
        assert _wait_group_dead(pgid), "process group still has members"
        assert group_alive(pgid) is False
    finally:
        _kill_group(proc, pgid)


def test_stop_tree_escalates_to_sigkill():
    proc, pgid, grandchild = _spawn(
        _leader_code(
            ignore=("SIGINT", "SIGTERM"),
            grandchild_ignore=("SIGINT", "SIGTERM"),
        )
    )
    try:
        result = stop_tree(proc, pgid, int_timeout=0.3, term_timeout=0.3)

        assert result == "SIGKILL"
        assert _wait_pid_dead(grandchild), "grandchild survived SIGKILL"
        assert _wait_group_dead(pgid), "process group still has members"
        assert proc.poll() is not None, "leader should have exited"
        assert pid_alive(grandchild) is False
    finally:
        _kill_group(proc, pgid)


def test_stop_tree_kills_orphans_after_leader_exits():
    proc, pgid, grandchild = _spawn(
        _leader_code(exit_on_sigint=True, grandchild_ignore=("SIGINT",))
    )
    try:
        result = stop_tree(proc, pgid, int_timeout=0.3, term_timeout=0.3)

        assert result in ("SIGTERM", "SIGKILL")
        assert proc.poll() is not None, "leader should have exited"
        assert _wait_pid_dead(grandchild), "orphaned grandchild survived"
        assert _wait_group_dead(pgid), "orphaned group member survived"
    finally:
        _kill_group(proc, pgid)


def test_reap_group_after_normal_exit():
    proc, pgid, grandchild = _spawn(_leader_code(linger=False))
    try:
        proc.wait(timeout=SPAWN_TIMEOUT)
        assert pid_alive(grandchild), "grandchild should outlive the leader"

        assert reap_group(pgid) is True
        assert _wait_pid_dead(grandchild), "grandchild survived reap_group"
        assert reap_group(pgid) is False
    finally:
        _kill_group(proc, pgid)


def test_never_signals_own_group():
    assert reap_group(os.getpgrp()) is False
