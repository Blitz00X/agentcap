"""Optional in-run collector for adopted zombies (default OFF).

It reaps only zombies that are AgentCap's children by adoption *and* whose
session id is neither AgentCap's nor their own pid: an in-group background job
or a double-forked daemon.  The agent itself (a session leader), a ``setsid``
helper and AgentCap's own children are never touched, so their exit statuses
are kept.  With the flag off nothing changes.
"""

from __future__ import annotations

import io
import os
import subprocess
import sys
import time

import httpx
import pytest

from agentcap import procstop
from agentcap.alerts import AlertDispatcher
from agentcap.ledger import Ledger
from agentcap.license import FREE_FEATURES
from agentcap.procstop import (
    ZOMBIE_COLLECTOR_ENV,
    ProcessTreeGuard,
    ZombieCollector,
    collect_adopted_zombies,
)
from agentcap.runner import Runner

pytestmark = pytest.mark.skipif(
    not procstop.subreaper_supported(), reason="child subreaper is Linux-only"
)

# The agent: prints the pids of three helpers that exit almost at once, then
# lives on so they are adopted (and become zombies) while it still runs.
AGENT = r"""
import os, subprocess, sys, time
in_group = subprocess.run(
    ["sh", "-c", "sleep 0.1 >/dev/null 2>&1 & echo $!"],
    capture_output=True, text=True).stdout.strip()
setsid_helper = subprocess.run(
    [sys.executable, "-c",
     "import os, subprocess, sys\n"
     "p = subprocess.Popen(['sleep', '0.1'], start_new_session=True)\n"
     "print(p.pid)"],
    capture_output=True, text=True).stdout.strip()
double_fork = subprocess.run(
    [sys.executable, "-c",
     "import os, sys\n"
     "r, w = os.pipe()\n"
     "if os.fork() == 0:\n"
     "    os.setsid()\n"
     "    pid = os.fork()\n"
     "    if pid == 0:\n"
     "        os.close(r); os.close(w); __import__('time').sleep(0.1); os._exit(11)\n"
     "    os.write(w, str(pid).encode()); os._exit(0)\n"
     "os.close(w)\n"
     "print(os.read(r, 32).decode())\n"
     "os.wait()"],
    capture_output=True, text=True).stdout.strip()
print(in_group, setsid_helper, double_fork, flush=True)
time.sleep(float(sys.argv[1]))
sys.exit(int(sys.argv[2]))
"""


def _stat(pid: int):
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8") as handle:
            fields = handle.read().rsplit(")", 1)[1].split()
    except OSError:
        return None
    return {"state": fields[0], "ppid": int(fields[1]), "sid": int(fields[3])}


def _wait_adopted_zombies(pids, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        stats = [_stat(pid) for pid in pids]
        if all(s and s["state"] == "Z" and s["ppid"] == os.getpid() for s in stats):
            return
        time.sleep(0.05)
    raise AssertionError(f"helpers were not adopted as zombies: {[_stat(p) for p in pids]}")


@pytest.fixture
def subreaper():
    guard = ProcessTreeGuard()
    assert guard.start()
    yield guard
    guard.sweep()
    guard.close()


def test_collects_in_group_and_double_fork_zombies_but_not_setsid_or_the_agent(subreaper):
    agent = subprocess.Popen(
        [sys.executable, "-c", AGENT, "3", "7"], stdout=subprocess.PIPE, text=True,
        start_new_session=True,
    )
    try:
        in_group, setsid_helper, double_fork = map(int, agent.stdout.readline().split())
        _wait_adopted_zombies([in_group, setsid_helper, double_fork])
        assert _stat(setsid_helper)["sid"] == setsid_helper  # a session leader

        collected = collect_adopted_zombies()

        assert collected == 2
        assert _stat(in_group) is None and _stat(double_fork) is None
        assert _stat(setsid_helper)["state"] == "Z", "a setsid helper must be left alone"
        assert agent.poll() is None
    finally:
        code = agent.wait(timeout=15)
    assert code == 7, "the agent's own exit status must stay with its Popen"


def test_never_touches_agentcaps_own_exited_children(subreaper):
    own = subprocess.Popen([sys.executable, "-c", "raise SystemExit(9)"])
    deadline = time.monotonic() + 10
    while (_stat(own.pid) or {}).get("state") != "Z" and time.monotonic() < deadline:
        time.sleep(0.02)
    assert collect_adopted_zombies() == 0
    assert own.wait(timeout=5) == 9


def test_collector_is_off_by_default_and_throttled(monkeypatch):
    monkeypatch.delenv(ZOMBIE_COLLECTOR_ENV, raising=False)
    calls = []
    monkeypatch.setattr(procstop, "collect_adopted_zombies", lambda: calls.append(1) or 0)
    off = ZombieCollector()
    assert off.enabled is False
    for _ in range(5):
        off.tick()
    assert calls == []

    monkeypatch.setenv(ZOMBIE_COLLECTOR_ENV, "1")
    on = ZombieCollector(interval=60.0)
    assert on.enabled is True
    for _ in range(5):
        on.tick()
    assert calls == [1]


def _runner(tmp_path, argv, stderr):
    return Runner(
        argv,
        cap="1",
        ledger=Ledger(tmp_path / f"ledger-{time.time_ns()}.db"),
        transport=httpx.MockTransport(lambda r: httpx.Response(500)),
        stderr=stderr,
        alerts=AlertDispatcher(features=FREE_FEATURES, stderr=stderr, background=False),
        features=FREE_FEATURES,
        poll_interval=0.02,
        term_timeout=2.0,
    )


def _summary_without_run_id(text: str) -> list[str]:
    return [line for line in text.splitlines() if "run id:" not in line]


@pytest.mark.parametrize("exit_code", [7, 9, 11])
def test_runner_keeps_the_agent_exit_code_with_the_flag_on_and_off(tmp_path, monkeypatch, exit_code):
    outputs = {}
    collected = {}
    real = procstop.collect_adopted_zombies

    def counting():
        n = real()
        collected[flag] = collected.get(flag, 0) + n
        return n

    monkeypatch.setattr(procstop, "collect_adopted_zombies", counting)
    for flag in ("0", "1"):
        monkeypatch.setenv(ZOMBIE_COLLECTOR_ENV, flag)
        stderr = io.StringIO()
        runner = _runner(tmp_path, [sys.executable, "-c", AGENT, "2.5", str(exit_code)], stderr)
        assert runner._zombies.enabled is (flag == "1")
        if runner._zombies.enabled:
            runner._zombies = ZombieCollector(interval=0.1)  # not timing-sensitive under load
        assert runner.run() == exit_code
        assert runner.summary.exit_code == exit_code
        assert runner.summary.reason == "child_exit"
        outputs[flag] = _summary_without_run_id(stderr.getvalue())

    assert collected.get("0", 0) == 0, "flag off must never collect"
    assert collected.get("1", 0) == 2, "flag on collects the in-group job and the daemon"
    # Same summary either way (the setsid helper is still swept at the end).
    assert outputs["0"] == outputs["1"]
