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

from agentcap.procstop import (
    ProcessTreeGuard,
    group_alive,
    reap_group,
    stop_tree,
    subreaper_supported,
)
from agentcap import procstop as procstop_module

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


# --------------------------------------------------------------- subreaper guard
ESCAPE_LEADER = """
import subprocess, sys
child = subprocess.Popen(
    ["setsid", sys.executable, "-c", "import time; time.sleep(60)"],
    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
)
print(child.pid, flush=True)
"""


def _spawn_escape_leader(linger: bool) -> tuple[subprocess.Popen, int, int]:
    code = ESCAPE_LEADER + ("\nimport time; time.sleep(60)\n" if linger else "")
    proc = subprocess.Popen(
        [sys.executable, "-c", code],
        start_new_session=True,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        escaped = _read_pid(proc)
    except BaseException:
        _kill_group(proc, proc.pid)
        raise
    return proc, proc.pid, escaped


@pytest.mark.skipif(not subreaper_supported(), reason="Linux subreaper is required")
def test_setsid_grandchild_is_swept_after_leader_exit():
    guard = ProcessTreeGuard()
    assert guard.start() is True
    proc, pgid, escaped = _spawn_escape_leader(linger=False)
    try:
        proc.wait(timeout=SPAWN_TIMEOUT)
        assert pid_alive(escaped), "setsid grandchild should have escaped the group"
        assert guard.sweep(term_timeout=0.5, kill_timeout=0.5) >= 1
        assert _wait_pid_dead(escaped), "setsid grandchild survived the sweep"
    finally:
        _kill_group(proc, pgid)
        try:
            os.kill(escaped, signal.SIGKILL)
        except OSError:
            pass
        guard.close()


@pytest.mark.skipif(not subreaper_supported(), reason="Linux subreaper is required")
def test_setsid_grandchild_is_swept_while_leader_is_alive():
    guard = ProcessTreeGuard()
    assert guard.start() is True
    proc, pgid, escaped = _spawn_escape_leader(linger=True)
    try:
        assert pid_alive(escaped)
        proc.kill()
        proc.wait(timeout=SPAWN_TIMEOUT)
        assert guard.sweep(term_timeout=0.5, kill_timeout=0.5) >= 1
        assert _wait_pid_dead(escaped), "setsid grandchild survived the sweep"
    finally:
        _kill_group(proc, pgid)
        try:
            os.kill(escaped, signal.SIGKILL)
        except OSError:
            pass
        guard.close()


@pytest.mark.skipif(not subreaper_supported(), reason="Linux subreaper is required")
def test_sweep_does_not_kill_a_baseline_child():
    unrelated = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        start_new_session=True,
    )
    guard = ProcessTreeGuard()
    assert guard.start() is True
    proc, pgid, escaped = _spawn_escape_leader(linger=False)
    try:
        proc.wait(timeout=SPAWN_TIMEOUT)
        assert guard.sweep(term_timeout=0.5, kill_timeout=0.5) >= 1
        assert unrelated.poll() is None, "baseline child was killed"
        assert _wait_pid_dead(escaped)
    finally:
        _kill_group(proc, pgid)
        unrelated.kill()
        unrelated.wait(timeout=SPAWN_TIMEOUT)
        guard.close()


def test_sweep_never_sigkills_a_reused_pid(monkeypatch):
    """A target that exits after TERM must not lend its PID to the SIGKILL.

    The snapshot is mocked and signals are recorded, not delivered: this test
    never touches a real process.  It reproduces the race where PID 4321 exits
    after SIGTERM and is immediately reused by a different process (start time
    222).  The original identity was (4321, 111); only that identity may be
    signalled.
    """

    root = 100
    pid = 4321
    before = procstop_module._ProcInfo(ppid=root, start=111, zombie=False)
    reused = procstop_module._ProcInfo(ppid=root, start=222, zombie=False)
    state = {
        "snapshot": {
            root: procstop_module._ProcInfo(ppid=0, start=1, zombie=False),
            pid: before,
        }
    }
    signals: list[tuple[int, int, int]] = []

    def fake_snapshot():
        return dict(state["snapshot"])

    def fake_reap(items, timeout):
        # Simulate the original process exiting and its PID being reused while
        # the caller waits for the TERM ladder to finish.
        state["snapshot"][pid] = reused
        return set(items)

    def fake_signal(pid, expected, sig):
        signals.append((pid, expected.start, sig))
        return True

    monkeypatch.setattr(procstop_module, "_snapshot", fake_snapshot)
    monkeypatch.setattr(procstop_module, "_reap_and_wait", fake_reap)
    monkeypatch.setattr(procstop_module, "_signal_if_same_process", fake_signal)

    guard = procstop_module.ProcessTreeGuard(root_pid=root)
    guard._enabled = True
    guard.baseline = {}
    guard.sweep(term_timeout=0, kill_timeout=0)

    assert signals == [(pid, 111, signal.SIGTERM)]
    assert all(sig != signal.SIGKILL for _, _, sig in signals), (
        "a reused PID received SIGKILL: " + repr(signals)
    )


def test_sweep_keeps_the_same_identity_for_kill(monkeypatch):
    """TERM and KILL must use the *same* (pid, start), not a later snapshot."""

    root = 100
    pid = 4321
    identity = procstop_module._ProcInfo(ppid=root, start=111, zombie=False)
    snapshot = {
        root: procstop_module._ProcInfo(ppid=0, start=1, zombie=False),
        pid: identity,
    }
    signals: list[tuple[int, int, int]] = []

    def fake_reap(items, timeout):
        return set(items)  # the target ignored TERM and is still alive

    def fake_signal(pid, expected, sig):
        signals.append((pid, expected.start, sig))
        return True

    monkeypatch.setattr(procstop_module, "_snapshot", lambda: dict(snapshot))
    monkeypatch.setattr(procstop_module, "_reap_and_wait", fake_reap)
    monkeypatch.setattr(procstop_module, "_signal_if_same_process", fake_signal)

    guard = procstop_module.ProcessTreeGuard(root_pid=root)
    guard._enabled = True
    guard.baseline = {}
    guard.sweep(term_timeout=0, kill_timeout=0)

    assert (pid, 111, signal.SIGTERM) in signals
    assert (pid, 111, signal.SIGKILL) in signals
    assert {start for _, start, _ in signals} == {111}


def test_reap_and_wait_drops_a_reused_pid_without_reaping_it(monkeypatch):
    """_reap_and_wait must compare start time, not just the PID."""

    pid = 4321
    expected = procstop_module._ProcessId(pid, 111)
    reused = procstop_module._ProcInfo(ppid=100, start=222, zombie=False)
    waitpid_calls: list[tuple[int, int]] = []

    monkeypatch.setattr(procstop_module, "_read_proc", lambda _pid: reused)
    monkeypatch.setattr(
        procstop_module.os,
        "waitpid",
        lambda _pid, _flags: waitpid_calls.append((_pid, _flags)) or (0, 0),
    )

    remaining = procstop_module._reap_and_wait({expected}, timeout=0)

    assert remaining == set()
    assert waitpid_calls == [], "waitpid must not touch a reused PID"


# ---------------------------------------------- adopted zombies in the agent group
# With the subreaper on, a background job whose shell exits is re-parented to
# AgentCap but stays in the agent's process group.  Once it exits it is a zombie
# that only AgentCap can reap -- and an unreaped zombie still answers
# killpg(pgid, 0), so the group looks alive until somebody reaps it.
BACKGROUND_JOB = "sleep {seconds} >/dev/null 2>&1 & echo $!"


def _spawn_background_job(seconds: float) -> tuple[subprocess.Popen, int, int]:
    """bash leaves ``sleep`` in its group and exits: (leader, pgid, job pid)."""
    proc = subprocess.Popen(
        ["bash", "-c", BACKGROUND_JOB.format(seconds=seconds)],
        start_new_session=True,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        job = _read_pid(proc)
        proc.wait(timeout=SPAWN_TIMEOUT)
    except BaseException:
        _kill_group(proc, proc.pid)
        raise
    return proc, proc.pid, job


def _cleanup_adopted(pid: int) -> None:
    try:
        os.kill(pid, signal.SIGKILL)
    except OSError:
        pass
    try:
        os.waitpid(pid, 0)
    except ChildProcessError:
        pass


@pytest.mark.skipif(not subreaper_supported(), reason="Linux subreaper is required")
def test_reap_group_reaps_an_adopted_zombie_instead_of_waiting_on_it():
    guard = ProcessTreeGuard()
    assert guard.start() is True
    proc, pgid, job = _spawn_background_job(0.2)
    try:
        assert _wait_until(lambda: _proc_state(job) == "Z", SPAWN_TIMEOUT), (
            "the background job never became our zombie"
        )
        started = time.monotonic()
        result = reap_group(pgid, term_timeout=1.0)
        elapsed = time.monotonic() - started

        assert result is False, "a zombie is not a survivor; nothing had to be killed"
        assert elapsed < 0.5, f"reap_group waited {elapsed:.2f}s on a zombie"
        assert _proc_state(job) is None, "the adopted zombie was left unreaped"
    finally:
        _cleanup_adopted(job)
        _kill_group(proc, pgid)
        guard.close()


@pytest.mark.skipif(not subreaper_supported(), reason="Linux subreaper is required")
def test_reap_group_returns_as_soon_as_an_adopted_member_dies():
    guard = ProcessTreeGuard()
    assert guard.start() is True
    proc, pgid, job = _spawn_background_job(30)
    try:
        assert pid_alive(job), "the background job should outlive its shell"
        started = time.monotonic()
        result = reap_group(pgid, term_timeout=1.0)
        elapsed = time.monotonic() - started

        assert result is True
        assert _proc_state(job) is None, "the terminated member was left as a zombie"
        assert elapsed < 0.8, f"SIGTERM worked but reap_group sat {elapsed:.2f}s"
    finally:
        _cleanup_adopted(job)
        _kill_group(proc, pgid)
        guard.close()


STOP_LEADER = (
    "import os, signal, subprocess, time\n"
    "signal.signal(signal.SIGINT, lambda *_: os._exit(0))\n"
    f"out = subprocess.run(['bash', '-c', {BACKGROUND_JOB.format(seconds=0.2)!r}],\n"
    "                     capture_output=True, text=True).stdout.strip()\n"
    "print(out, flush=True)\n"
    "time.sleep(60)\n"
)


@pytest.mark.skipif(not subreaper_supported(), reason="Linux subreaper is required")
def test_stop_tree_does_not_escalate_because_of_an_adopted_zombie():
    guard = ProcessTreeGuard()
    assert guard.start() is True
    proc, pgid, job = _spawn(STOP_LEADER)
    try:
        assert _wait_until(lambda: _proc_state(job) == "Z", SPAWN_TIMEOUT)
        started = time.monotonic()
        result = stop_tree(proc, pgid, int_timeout=1.0, term_timeout=1.0, kill_timeout=1.0)
        elapsed = time.monotonic() - started

        assert result == "SIGINT", f"the leader obeyed SIGINT, yet the stop escalated to {result}"
        assert elapsed < 0.8, f"stop_tree took {elapsed:.2f}s"
        assert proc.poll() is not None
        assert _proc_state(job) is None, "the adopted zombie was left unreaped"
    finally:
        _cleanup_adopted(job)
        _kill_group(proc, pgid)
        guard.close()


def test_read_proc_takes_parent_and_start_time_from_one_read(monkeypatch):
    """ppid and start time must describe the same process.

    Reading ``status`` for the parent and ``stat`` for the start time are two
    reads; if the PID is reused in between, the old process's parent is paired
    with the newcomer's start time.  Here ``status`` still shows the old
    process (child of 100) while ``stat`` already shows the newcomer (child of
    999, started at 222).
    """

    import io

    after_comm = ["S", "999", "4321", "4321", "0", "-1", "0", "0", "0", "0", "0",
                  "0", "0", "0", "0", "20", "0", "1", "0", "222", "0", "0"]
    files = {
        "/proc/4321/status": "Name:\tsleep\nState:\tS (sleeping)\nPPid:\t100\n",
        "/proc/4321/stat": "4321 (sleep) " + " ".join(after_comm) + "\n",
    }

    def fake_open(path, *args, **kwargs):
        return io.StringIO(files[str(path)])

    monkeypatch.setattr(procstop_module, "open", fake_open, raising=False)

    info = procstop_module._read_proc(4321)

    assert (info.ppid, info.start, info.zombie) == (999, 222, False)


def test_sweep_spares_new_children_of_a_baseline_process(monkeypatch):
    """A process that existed before the run is unrelated, and so is its subtree.

    Mocked snapshot, recorded signals: no real process is touched.  Root 100
    had child 200 before the run; during the run 200 starts 300 (its own
    worker) and the agent leaves orphan 400 behind.  Only 400 is the agent's.
    """

    root = 100
    proc = procstop_module._ProcInfo
    snapshot = {
        root: proc(ppid=0, start=1, zombie=False),
        200: proc(ppid=root, start=5, zombie=False),
        300: proc(ppid=200, start=50, zombie=False),
        400: proc(ppid=root, start=60, zombie=False),
    }
    signals: list[tuple[int, int]] = []

    def fake_signal(pid, expected, sig):
        signals.append((pid, sig))
        snapshot.pop(pid, None)  # the target obeys and exits
        return True

    monkeypatch.setattr(procstop_module, "_snapshot", lambda: dict(snapshot))
    monkeypatch.setattr(procstop_module, "_reap_and_wait", lambda items, timeout: set())
    monkeypatch.setattr(procstop_module, "_signal_if_same_process", fake_signal)

    guard = procstop_module.ProcessTreeGuard(root_pid=root)
    guard._enabled = True
    guard.baseline = {200: 5}

    assert guard.sweep(term_timeout=0, kill_timeout=0) == 1
    assert signals == [(400, signal.SIGTERM)], signals


# ------------------------------------------- F5: processes we may not signal
# A descendant owned by another user (``sudo -b``) or running a setuid program
# makes os.kill raise PermissionError.  Everything below is mocked: the /proc
# view, os.kill and the clock.  No real process is ever signalled.


class _FakeClock:
    """Virtual monotonic clock: ``sleep`` advances it instead of blocking."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.slept = 0.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds
        self.slept += seconds


class _FakeTree:
    """A mocked /proc plus a recording os.kill for :class:`ProcessTreeGuard`."""

    ROOT = 100

    def __init__(self, monkeypatch, procs: dict[int, tuple[int, int]]) -> None:
        P = procstop_module._ProcInfo
        self.procs: dict[int, procstop_module._ProcInfo] = {
            self.ROOT: P(ppid=0, start=1, zombie=False)
        }
        for pid, (ppid, start) in procs.items():
            self.procs[pid] = P(ppid=ppid, start=start, zombie=False)
        self.kills: list[tuple[int, int, int]] = []  # (pid, start at kill time, sig)
        self.behaviour: dict[tuple[int, int], object] = {}
        self.waitpids: list[int] = []
        self.clock = _FakeClock()
        monkeypatch.setattr(procstop_module, "_snapshot", lambda: dict(self.procs))
        monkeypatch.setattr(procstop_module, "_read_proc", lambda pid: self.procs.get(pid))
        monkeypatch.setattr(procstop_module.os, "kill", self.kill)
        monkeypatch.setattr(procstop_module.os, "waitpid", self.waitpid)
        monkeypatch.setattr(procstop_module.time, "monotonic", self.clock.monotonic)
        monkeypatch.setattr(procstop_module.time, "sleep", self.clock.sleep)

    def kill(self, pid: int, sig: int) -> None:
        info = self.procs.get(pid)
        if info is None:
            raise ProcessLookupError(pid)
        self.kills.append((pid, info.start, sig))
        action = self.behaviour.get((pid, info.start), "obey")
        if callable(action):
            action(pid, sig)
        elif action == "obey":
            self.procs.pop(pid)
        elif action == "deny":
            raise PermissionError(1, "Operation not permitted")
        elif action == "deny_kill" and sig == signal.SIGKILL:
            raise PermissionError(1, "Operation not permitted")

    def waitpid(self, pid: int, flags: int):
        self.waitpids.append(pid)
        info = self.procs.get(pid)
        if info is not None and info.zombie:
            self.procs.pop(pid)
            return pid, 0
        return 0, 0

    def guard(self) -> ProcessTreeGuard:
        guard = ProcessTreeGuard(root_pid=self.ROOT)
        guard._enabled = True
        guard.baseline = {}
        return guard


def test_sweep_tries_an_unsignallable_process_once_and_never_waits_on_it(monkeypatch):
    tree = _FakeTree(monkeypatch, {4242: (100, 111), 4300: (100, 120)})
    tree.behaviour[(4242, 111)] = "deny"  # e.g. a root-owned `sudo -b` child
    guard = tree.guard()

    killed = guard.sweep(term_timeout=2.0, kill_timeout=2.0)

    attempts = [k for k in tree.kills if k[0] == 4242]
    assert attempts == [(4242, 111, signal.SIGTERM)], attempts
    assert tree.clock.slept < 0.5, f"sweep slept {tree.clock.slept:.1f}s on a process it cannot signal"
    assert killed == 1, "only the process that was actually stopped counts as killed"
    assert guard.denied_pids == (4242,)


def test_sweep_does_not_wait_after_sigkill_is_denied(monkeypatch):
    # TERM is delivered (and ignored); by the KILL step the process is no longer
    # ours to signal (it exec'd a setuid program).  One TERM wait, then stop.
    tree = _FakeTree(monkeypatch, {4242: (100, 111)})
    tree.behaviour[(4242, 111)] = "deny_kill"
    guard = tree.guard()

    killed = guard.sweep(term_timeout=2.0, kill_timeout=2.0)

    assert tree.kills == [(4242, 111, signal.SIGTERM), (4242, 111, signal.SIGKILL)]
    assert tree.clock.slept < 2.5, f"sweep slept {tree.clock.slept:.1f}s"
    assert killed == 0, "it got SIGTERM but is still running: not killed"
    assert guard.denied_pids == (4242,)


def test_sweep_still_stops_a_new_process_that_reuses_a_denied_pid(monkeypatch):
    # (4242, start 111) cannot be signalled and exits; the kernel hands PID 4242
    # to a new descendant (start 222).  The denial belongs to the old identity:
    # the newcomer is stopped normally, and the old identity is never retried.
    tree = _FakeTree(monkeypatch, {4242: (100, 111)})

    def deny_then_reuse(pid, sig):
        tree.procs[pid] = procstop_module._ProcInfo(ppid=100, start=222, zombie=False)
        raise PermissionError(1, "Operation not permitted")

    tree.behaviour[(4242, 111)] = deny_then_reuse
    guard = tree.guard()

    killed = guard.sweep(term_timeout=2.0, kill_timeout=2.0)

    assert tree.kills == [(4242, 111, signal.SIGTERM), (4242, 222, signal.SIGTERM)], tree.kills
    assert killed == 1
    assert guard.denied_pids == (), "the denied identity is gone; nothing was left behind"
    assert tree.clock.slept < 0.5


def test_denied_identity_is_never_retried_while_other_rounds_run(monkeypatch):
    # A second target keeps the sweep going for several rounds (it respawns a
    # child each time it is stopped).  The denied identity is still only tried
    # once, and a denied PID never borrows a signal meant for anyone else.
    tree = _FakeTree(monkeypatch, {4242: (100, 111), 5000: (100, 500)})
    tree.behaviour[(4242, 111)] = "deny"
    spawned = iter([5001, 5002])

    def respawn(pid, sig):
        tree.procs.pop(pid)
        nxt = next(spawned, None)
        if nxt is not None:
            tree.procs[nxt] = procstop_module._ProcInfo(ppid=100, start=nxt, zombie=False)
            tree.behaviour[(nxt, nxt)] = respawn

    tree.behaviour[(5000, 500)] = respawn
    guard = tree.guard()

    killed = guard.sweep(term_timeout=2.0, kill_timeout=2.0)

    assert [k for k in tree.kills if k[0] == 4242] == [(4242, 111, signal.SIGTERM)]
    assert {k[0] for k in tree.kills} == {4242, 5000, 5001, 5002}
    assert killed == 3
    assert guard.denied_pids == (4242,)
    assert tree.clock.slept < 0.5


def test_denied_process_that_exits_on_its_own_is_reaped_not_reported(monkeypatch):
    tree = _FakeTree(monkeypatch, {4242: (100, 111)})

    def deny_then_exit(pid, sig):
        tree.procs[pid] = procstop_module._ProcInfo(ppid=100, start=111, zombie=True)
        raise PermissionError(1, "Operation not permitted")

    tree.behaviour[(4242, 111)] = deny_then_exit
    guard = tree.guard()

    assert guard.sweep(term_timeout=2.0, kill_timeout=2.0) == 0
    assert guard.denied_pids == ()
    assert 4242 not in tree.procs, "our adopted zombie was left unreaped"


def test_sweep_already_gone_and_normal_paths_report_nothing_denied(monkeypatch):
    tree = _FakeTree(monkeypatch, {4242: (100, 111), 4300: (100, 120)})

    def vanish(pid, sig):  # exited between the snapshot and the signal
        tree.procs.pop(pid)
        raise ProcessLookupError(pid)

    tree.behaviour[(4242, 111)] = vanish
    guard = tree.guard()

    assert guard.sweep(term_timeout=2.0, kill_timeout=2.0) == 1  # only 4300 was stopped
    assert guard.denied_pids == ()
    assert tree.clock.slept < 0.5
    assert ProcessTreeGuard().denied_pids == ()


def test_denied_warning_names_the_processes():
    assert procstop_module.denied_warning(()) is None
    msg = procstop_module.denied_warning((4242, 4300))
    assert msg.startswith("agentcap: could not stop 2 process(es) (permission denied): pid 4242, 4300")


def test_reap_group_does_not_wait_when_no_member_may_be_signalled(monkeypatch):
    # killpg fails with EPERM only when *no* member could be signalled, so
    # waiting for the group to empty (and retrying with SIGKILL) is pointless.
    clock = _FakeClock()
    calls: list[tuple[int, int]] = []

    def fake_killpg(pgid, sig):
        calls.append((pgid, sig))
        if sig != 0:
            raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr(procstop_module.os, "killpg", fake_killpg)
    monkeypatch.setattr(procstop_module.os, "getpgrp", lambda: 1)
    monkeypatch.setattr(procstop_module, "_reap_group_zombies", lambda pgid: None)
    monkeypatch.setattr(procstop_module.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(procstop_module.time, "sleep", clock.sleep)

    assert reap_group(7777, term_timeout=2.0) is False, "nothing was killed"
    assert [sig for _, sig in calls if sig != 0] == [signal.SIGTERM]
    assert clock.slept == 0.0
