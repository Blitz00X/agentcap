"""Process-group stop/reap helpers.

Escalates a signal ladder over a whole process group (for example a shell
that spawned helpers in its own session) so no stray grandchild outlives the
run.  The caller's own process group is never signalled: without a usable
group id these helpers fall back to the leader process alone.
"""

from __future__ import annotations

import ctypes
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import Mapping

POLL_INTERVAL = 0.05

# Linux prctl(2) options.  The numeric values are stable ABI.
_PR_SET_CHILD_SUBREAPER = 36
_PR_GET_CHILD_SUBREAPER = 37


def _prctl(option: int, argument: int) -> int:
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        return int(libc.prctl(option, argument, 0, 0, 0))
    except (OSError, AttributeError):
        return -1


def subreaper_supported() -> bool:
    """True when the host can make AgentCap adopt orphaned descendants."""

    return sys.platform.startswith("linux")


def _get_subreaper() -> bool | None:
    if not subreaper_supported():
        return None
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        value = ctypes.c_int(0)
        if libc.prctl(_PR_GET_CHILD_SUBREAPER, ctypes.byref(value), 0, 0, 0) != 0:
            return None
        return bool(value.value)
    except (OSError, AttributeError):
        return None


@dataclass(frozen=True)
class _ProcInfo:
    ppid: int
    start: int
    zombie: bool


@dataclass(frozen=True)
class _ProcessId:
    """A PID plus the start time that makes it unique across reuse."""

    pid: int
    start: int


def _read_proc(pid: int) -> _ProcInfo | None:
    """Read ppid/starttime/state for one pid from a single ``stat`` read.

    One read keeps the fields consistent: separate ``status`` and ``stat``
    reads could pair one process's parent with a reused PID's start time.
    """

    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8", errors="replace") as handle:
            # comm may contain spaces and ')'; the last ')' ends the comm field.
            fields = handle.read().rsplit(")", 1)[1].split()
        # fields[0] is state (field 3), [1] ppid (field 4), [19] starttime (field 22).
        return _ProcInfo(ppid=int(fields[1]), start=int(fields[19]), zombie=fields[0] == "Z")
    except (OSError, ValueError, IndexError):
        return None


def _snapshot() -> dict[int, _ProcInfo]:
    procs: dict[int, _ProcInfo] = {}
    try:
        names = os.listdir("/proc")
    except OSError:
        return procs
    for name in names:
        if not name.isdigit():
            continue
        info = _read_proc(int(name))
        if info is not None:
            procs[int(name)] = info
    return procs


def _descendants(root_pid: int, procs: dict[int, _ProcInfo]) -> set[int]:
    children: dict[int, list[int]] = {}
    for pid, info in procs.items():
        children.setdefault(info.ppid, []).append(pid)
    found: set[int] = set()
    stack = list(children.get(root_pid, []))
    while stack:
        pid = stack.pop()
        if pid in found or pid == root_pid:
            continue
        found.add(pid)
        stack.extend(children.get(pid, []))
    return found


def _depth(pid: int, procs: dict[int, _ProcInfo], root_pid: int) -> int:
    depth = 0
    seen: set[int] = set()
    current = pid
    while current != root_pid and current not in seen:
        seen.add(current)
        info = procs.get(current)
        if info is None:
            break
        current = info.ppid
        depth += 1
    return depth


def _signal_if_same_process(pid: int, expected: _ProcInfo, sig: int) -> bool:
    """Signal ``pid`` only if it is still the process ``expected`` describes.

    Returns False when it is gone, a zombie or a reused PID.  PermissionError
    (another user's process, a setuid program) is *raised*: the caller must
    stop treating that identity as something it can clean up.
    """
    current = _read_proc(pid)
    if current is None or current.zombie or current.start != expected.start:
        return False
    try:
        os.kill(pid, sig)
        return True
    except PermissionError:
        raise
    except (ProcessLookupError, OSError):
        return False


def denied_warning(pids: tuple[int, ...] | list[int]) -> str | None:
    """User-facing warning for descendants that could not be signalled."""

    if not pids:
        return None
    listed = ", ".join(str(pid) for pid in pids)
    return (
        f"agentcap: could not stop {len(pids)} process(es) (permission denied): pid {listed}; "
        "they run as another user or a setuid program and are still running."
    )


def _reap_and_wait(expected: set[_ProcessId], timeout: float) -> set[_ProcessId]:
    """Wait for the exact process identities to exit; never follow a reused PID.

    A PID is only meaningful together with its start time.  If the original
    process exits and the kernel reuses its PID for something else, that new
    process is *not* the one we signalled: it must be dropped, not waited for
    (``waitpid`` could otherwise reap an unrelated child) and never returned
    for the SIGKILL step.
    """

    remaining = set(expected)
    deadline = time.monotonic() + max(0.0, timeout)
    while True:
        for ident in list(remaining):
            info = _read_proc(ident.pid)
            if info is None or info.start != ident.start:
                remaining.discard(ident)  # exited, or PID reused by a stranger
                continue
            if info.zombie:
                try:
                    os.waitpid(ident.pid, os.WNOHANG)
                except (ChildProcessError, OSError):
                    pass
                remaining.discard(ident)
        if not remaining or time.monotonic() >= deadline:
            return remaining
        time.sleep(POLL_INTERVAL)


def _still_running(ident: _ProcessId) -> bool:
    """True while ``ident`` is alive; reap it if it has become our zombie."""

    info = _read_proc(ident.pid)
    if info is None or info.start != ident.start:
        return False
    if info.zombie:
        try:
            os.waitpid(ident.pid, os.WNOHANG)
        except (ChildProcessError, OSError):
            pass
        return False
    return True


class ProcessTreeGuard:
    """Adopt and clean up descendants that leave the child's process group.

    Linux ``PR_SET_CHILD_SUBREAPER`` makes orphaned grandchildren (including
    processes that call ``setsid`` and daemonize) reparent to AgentCap instead
    of init.  A snapshot taken before the run distinguishes them from unrelated
    children of the same process (important for tests and embedders), and every
    signal is checked against the process start time so a recycled pid is never
    killed.  The previous subreaper setting is restored by :meth:`close`.

    This is a best-effort cleanup, not a security sandbox: a process that
    changes its uid, enters another pid namespace, or is owned by another user
    cannot be found or signalled.  If AgentCap itself is SIGKILLed, the
    parent-death lifeline kills the direct child and its watchdog kills the
    agent's process group, but descendants that had already left the group
    (``setsid``) may still escape; no userspace helper can clean up after its
    own SIGKILL.
    """

    def __init__(self, root_pid: int | None = None) -> None:
        self.root_pid = root_pid if root_pid is not None else os.getpid()
        self.baseline: dict[int, int] = {}
        # PIDs the last sweep could not signal (PermissionError) and that were
        # still running when it finished.  See :func:`denied_warning`.
        self.denied_pids: tuple[int, ...] = ()
        self._previous: bool | None = None
        self._enabled = False

    def start(self) -> bool:
        """Enable the subreaper and capture the pre-run child baseline."""

        if not subreaper_supported():
            return False
        self._previous = _get_subreaper()
        if _prctl(_PR_SET_CHILD_SUBREAPER, 1) != 0:
            self._previous = None
            return False
        self._enabled = True
        procs = _snapshot()
        self.baseline = {
            pid: procs[pid].start for pid in _descendants(self.root_pid, procs)
        }
        return True

    def sweep(self, term_timeout: float = 2.0, kill_timeout: float = 2.0) -> int:
        """Terminate descendants created after :meth:`start`; return the count.

        Every target is tracked as ``(pid, start_time)`` from the snapshot that
        selected it.  The same identity is used for TERM, the wait and KILL.  If
        the PID disappears after TERM and the kernel reuses it, the newcomer is
        not signalled; later sweep rounds also skip a PID whose start time
        differs from the identity already attempted.  The trade-off is
        conservative: a new descendant that happens to reuse a targeted PID in
        the few seconds of cleanup is skipped rather than risking a kill of an
        unrelated process.

        An identity whose signal fails with PermissionError (another user's
        process, e.g. ``sudo -b``, or a setuid program) is tried once and then
        neither waited for nor signalled again; if it is still running at the
        end it is listed in :attr:`denied_pids` and not counted as killed.  The
        denial belongs to that ``(pid, start_time)`` only: a later descendant
        that reuses the PID is handled like any other.
        """

        if not self._enabled:
            return 0
        signalled: set[_ProcessId] = set()
        denied: set[_ProcessId] = set()
        attempted: dict[int, int] = {}
        for _ in range(4):
            procs = _snapshot()
            protected = self._baseline_subtrees(procs)
            targets: dict[int, int] = {}
            for pid in _descendants(self.root_pid, procs):
                info = procs[pid]
                if pid in protected or _ProcessId(pid, info.start) in denied:
                    continue
                previous = attempted.get(pid)
                if previous is not None and previous != info.start:
                    # The original process is gone and this PID now belongs to
                    # a different process.  Never transfer the kill to it.
                    continue
                targets[pid] = info.start
                attempted[pid] = info.start
            if not targets:
                break
            ordered = sorted(targets, key=lambda pid: _depth(pid, procs, self.root_pid), reverse=True)
            for pid in ordered:
                info = procs.get(pid)
                if info is not None and self._signal(pid, info, signal.SIGTERM, denied, attempted):
                    signalled.add(_ProcessId(pid, info.start))
            remaining = _reap_and_wait(
                {_ProcessId(pid, start) for pid, start in targets.items()} - denied,
                term_timeout,
            )
            if remaining:
                latest = _snapshot()
                for ident in sorted(
                    remaining,
                    key=lambda item: _depth(item.pid, latest, self.root_pid),
                    reverse=True,
                ):
                    info = latest.get(ident.pid)
                    if info is None or info.start != ident.start:
                        continue  # PID reused while we waited: not our process
                    if self._signal(ident.pid, info, signal.SIGKILL, denied, attempted):
                        signalled.add(ident)
                _reap_and_wait(remaining - denied, kill_timeout)
        left = {ident for ident in denied if _still_running(ident)}
        self.denied_pids = tuple(sorted(ident.pid for ident in left))
        # A process that got SIGTERM but refused SIGKILL and is still running
        # was not stopped: never count it as killed.
        return len(signalled - left)

    @staticmethod
    def _signal(
        pid: int,
        info: _ProcInfo,
        sig: int,
        denied: set[_ProcessId],
        attempted: dict[int, int],
    ) -> bool:
        try:
            return _signal_if_same_process(pid, info, sig)
        except PermissionError:
            denied.add(_ProcessId(pid, info.start))
            # The denial is tied to this identity, not to the PID: a later
            # descendant that reuses the PID must not be skipped because of it.
            attempted.pop(pid, None)
            return False

    def _baseline_subtrees(self, procs: dict[int, _ProcInfo]) -> set[int]:
        """Pre-run children that are still the same process, and their subtrees.

        The agent's processes can never be below them (a baseline child is a
        sibling of the agent, not an ancestor), so a worker a baseline child
        starts during the run is just as unrelated as the child itself.
        """

        keep: set[int] = set()
        for pid, start in self.baseline.items():
            info = procs.get(pid)
            if info is not None and info.start == start:
                keep.add(pid)
                keep |= _descendants(pid, procs)
        return keep

    def close(self) -> None:
        """Restore the subreaper setting that was active before :meth:`start`."""

        if not self._enabled:
            return
        if self._previous is not None:
            _prctl(_PR_SET_CHILD_SUBREAPER, 1 if self._previous else 0)
        self._enabled = False

    def __enter__(self) -> "ProcessTreeGuard":
        self.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        try:
            self.sweep()
        finally:
            self.close()


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


#: Opt-in (default OFF): reap adopted zombies during the run, not only at its end.
ZOMBIE_COLLECTOR_ENV = "AGENTCAP_COLLECT_ZOMBIES"


def _zombie_identity(pid: int) -> tuple[int, int, int, bool] | None:
    """``(ppid, sid, start, zombie)`` from one ``/proc/<pid>/stat`` read."""

    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8", errors="replace") as handle:
            fields = handle.read().rsplit(")", 1)[1].split()
        # state (3), ppid (4), session (6), starttime (22).
        return int(fields[1]), int(fields[3]), int(fields[19]), fields[0] == "Z"
    except (OSError, ValueError, IndexError):
        return None


def collect_adopted_zombies() -> int:
    """Reap exited helpers AgentCap adopted as subreaper; return how many.

    Only a zombie that is our child *and* whose session id is neither
    AgentCap's own nor its own pid is reaped: an in-group background job or a
    double-forked daemon of the agent.  The agent (started in its own
    session), a ``setsid`` helper (a session leader) and any child AgentCap
    started itself are never waited for here, so their exit statuses stay with
    their ``Popen``/final sweep.  Each candidate's ``(pid, start)`` identity is
    re-read just before ``waitpid`` so a reused PID is never reaped.
    """

    if not subreaper_supported():
        return 0
    me = os.getpid()
    try:
        my_sid = os.getsid(0)
        names = os.listdir("/proc")
    except OSError:
        return 0
    collected = 0
    for name in names:
        if not name.isdigit():
            continue
        pid = int(name)
        first = _zombie_identity(pid)
        if first is None:
            continue
        ppid, sid, _start, zombie = first
        if ppid != me or not zombie or sid in (my_sid, pid):
            continue
        if _zombie_identity(pid) != first:
            continue  # changed under us: never wait on a different process
        try:
            got, _status = os.waitpid(pid, os.WNOHANG)
        except (ChildProcessError, OSError):
            continue
        if got == pid:
            collected += 1
    return collected


class ZombieCollector:
    """Throttled :func:`collect_adopted_zombies`, enabled by an env flag.

    Off unless ``AGENTCAP_COLLECT_ZOMBIES=1``; when off :meth:`tick` does
    nothing at all, so the run behaves exactly as before.
    """

    def __init__(self, *, interval: float = 1.0, env: Mapping[str, str] | None = None) -> None:
        source = os.environ if env is None else env
        self.enabled = source.get(ZOMBIE_COLLECTOR_ENV, "").strip() == "1"
        self.interval = float(interval)
        self.collected = 0
        self._next = 0.0

    def tick(self) -> int:
        if not self.enabled:
            return 0
        now = time.monotonic()
        if now < self._next:
            return 0
        self._next = now + self.interval
        try:
            # Looked up at call time so tests can observe it.
            count = sys.modules[__name__].collect_adopted_zombies()
        except Exception:
            return 0
        self.collected += count
        return count


def _reap_group_zombies(pgid: int | None) -> None:
    """Reap our own exited children that are still members of group ``pgid``.

    As child subreaper (:class:`ProcessTreeGuard`) AgentCap adopts the agent's
    orphaned helpers, and they stay in the agent's process group.  Once they
    exit they are zombies only AgentCap can reap, and a zombie still answers
    ``killpg(pgid, 0)``: without this the group looks alive and every wait runs
    to its timeout.  Call it only after the group leader has been reaped
    (``Popen.poll``/``wait``) so its exit status stays with the ``Popen``.
    """
    if pgid is None or pgid <= 1:
        return  # waitpid(-1) would mean "any child"
    while True:
        try:
            pid, _status = os.waitpid(-pgid, os.WNOHANG)
        except ChildProcessError:
            return  # no child of ours is left in that group
        except OSError:
            return
        if pid == 0:
            return  # members exist, none has exited


def _wait_gone(
    child: subprocess.Popen, pgid: int | None, use_group: bool, timeout: float
) -> bool:
    """Wait until the leader exited and the group is empty, or ``timeout`` passes."""
    deadline = time.monotonic() + max(0.0, timeout)
    while True:
        leader_done = child.poll() is not None
        if leader_done and use_group:
            _reap_group_zombies(pgid)
        members_done = not (use_group and group_alive(pgid))
        if leader_done and members_done:
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(POLL_INTERVAL)


def _wait_group_gone(pgid: int, timeout: float) -> bool:
    deadline = time.monotonic() + max(0.0, timeout)
    while True:
        _reap_group_zombies(pgid)
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

    leader_done = child.poll() is not None
    if leader_done and use_group:
        _reap_group_zombies(pgid)
    nothing_alive = leader_done and not (use_group and group_alive(pgid))
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
    group is never touched.  If no member may be signalled at all (``killpg``
    fails with EPERM, e.g. only a root-owned ``sudo -b`` child is left) it
    returns False at once: waiting, or retrying with SIGKILL, cannot help, and
    nothing was killed.  On Linux :meth:`ProcessTreeGuard.sweep` then reports
    such a process via ``denied_pids``.
    """
    if pgid is None or pgid <= 0 or pgid == os.getpgrp():
        return False
    _reap_group_zombies(pgid)  # adopted helpers that already exited
    if not group_alive(pgid):
        return False

    try:
        os.killpg(pgid, signal.SIGTERM)
    except PermissionError:
        return False
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
