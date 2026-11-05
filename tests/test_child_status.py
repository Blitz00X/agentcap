"""Exit statuses survive a run in which AgentCap holds adopted zombies.

As child subreaper (Linux) AgentCap adopts the agent's orphaned helpers, and
the ones that exit stay AgentCap's zombies until the end of the run: the
agent's group is reaped after its leader, everything else by the final sweep.
They are deliberately not reaped while the run is going on.  ``/proc`` only
shows a process's *current* parent, so a ``setsid`` helper AgentCap adopted is
indistinguishable from a child this process started with
``start_new_session=True`` (same parent, ``pgrp == sid == pid``, same start
tick), and a reaper that guesses wrong takes that child's exit status: CPython
then reports exit code 0 for it (ECHILD is mapped to returncode 0).

These tests pin the safe side in every run mode.  After the agent's helpers
have exited (and, today, sit as AgentCap's zombies), children started during
the run by another thread of the same process -- in AgentCap's own group, in a
new session, in a new group -- keep their exact exit statuses, and so does the
agent.  A blanket ``waitpid(-1)``, "every zombie child except the agent and
the watchdog" or "every zombie child outside AgentCap's session" reaper in the
monitor loop makes them fail.  Only processes the test itself creates are
touched.
"""

from __future__ import annotations

import io
import os
import subprocess
import sys
import threading
import time

import httpx
import pytest

from agentcap.procstop import subreaper_supported

pytestmark = pytest.mark.skipif(
    not subreaper_supported(), reason="the Linux child subreaper is required"
)

# Long enough for many monitor ticks (10-50 ms) and for a reaper that runs at
# most once a second.
RUN_WINDOW = 1.5
AGENT_EXIT = 5

# The agent leaves the three kinds of adopted zombie behind, publishes their
# pids and then waits for the test before it exits with AGENT_EXIT.
AGENT = """#!{python}
import json, os, subprocess, sys, time

def background(command):
    out = subprocess.run(["bash", "-c", command + " >/dev/null 2>&1 & echo $!"],
                         capture_output=True, text=True, check=True)
    return int(out.stdout)

pids = [
    background("sleep 0.1"),          # job of an exited shell: still in the agent's group
    background("setsid sleep 0.1"),   # setsid helper of an exited shell: leads its own session
]
read_end, write_end = os.pipe()       # double-fork daemon: non-leader of a dead leader's session
first = os.fork()
if first == 0:
    os.setsid()
    if os.fork() == 0:
        os.write(write_end, str(os.getpid()).encode())
        time.sleep(0.1)
        os._exit(3)
    os._exit(0)
os.close(write_end)
os.waitpid(first, 0)
pids.append(int(os.read(read_end, 32)))
os.close(read_end)
target = os.environ["ADOPTED_PIDS"]
with open(target + ".tmp", "w") as handle:
    handle.write(" ".join(map(str, pids)))
os.replace(target + ".tmp", target)
print(json.dumps({{"type": "step_finish", "part": {{"type": "step-finish", "cost": 0,
      "tokens": {{"input": 1, "output": 1, "reasoning": 0, "cache": {{"read": 0, "write": 0}}}}}}}}),
      flush=True)
deadline = time.time() + 60
while time.time() < deadline and not os.path.exists(os.environ["AGENT_GO"]):
    time.sleep(0.02)
sys.exit({exit_code})
"""


def _stat(pid: int) -> tuple[str, int] | None:
    """(state, ppid) of ``pid`` from one ``/proc`` read, or None once it is gone."""
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8", errors="replace") as handle:
            fields = handle.read().rsplit(")", 1)[1].split()
    except (OSError, IndexError):
        return None
    return fields[0], int(fields[1])


def _our_zombie(pid: int) -> bool:
    """True while ``pid`` is an exited child of this process that nobody reaped."""
    return _stat(pid) == ("Z", os.getpid())


def _exited(pid: int) -> bool:
    """True once ``pid`` no longer runs: a zombie, or already reaped."""
    info = _stat(pid)
    return info is None or info[0] == "Z"


def _wait_until(predicate, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.02)
    return True


def _cap_run(tmp_path):
    from agentcap.alerts import AlertDispatcher
    from agentcap.ledger import Ledger
    from agentcap.license import FREE_FEATURES
    from agentcap.runner import Runner

    err = io.StringIO()
    runner = Runner(
        ["zombie-agent"],
        cap="1",
        ledger=Ledger(tmp_path / "ledger.sqlite3"),
        transport=httpx.MockTransport(lambda _request: httpx.Response(500)),
        stderr=err,
        alerts=AlertDispatcher(features=FREE_FEATURES, stderr=err, background=False),
        features=FREE_FEATURES,
        poll_interval=0.01,
        term_timeout=2.0,
    )
    return runner.run


def _harness_run(tmp_path):
    from agentcap.harness import HarnessRunner

    runner = HarnessRunner(
        ["opencode", "run", "hi"], tokens=10**9, enforce=True, alerts=lambda _event: None,
        stderr=io.StringIO(), stdout=io.StringIO(), poll_s=0.05, quota_poll_s=0.2,
        receipt_dir=tmp_path / "receipts", home=tmp_path,
    )
    return runner.run


def _quota_run(tmp_path):
    from agentcap.quota import QuotaRunner

    runner = QuotaRunner(
        ["claude", "-p", "fix"], quota=50.0, alerts=lambda _event: None,
        stderr=io.StringIO(), poll_s=0.05, claude_grace_s=1.0,
        receipt_dir=tmp_path / "receipts", cwd=tmp_path, home=tmp_path / "home",
        job_root=tmp_path / "state",
    )
    return runner.run


RUNS = {"cap": _cap_run, "harness": _harness_run, "quota": _quota_run}


@pytest.mark.parametrize("mode", sorted(RUNS))
def test_children_keep_their_exit_status_while_adopted_zombies_exist(mode, tmp_path, monkeypatch):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for name in ("zombie-agent", "opencode", "claude"):
        exe = bindir / name
        exe.write_text(AGENT.format(python=sys.executable, exit_code=AGENT_EXIT))
        exe.chmod(0o755)
    pidfile, gofile = tmp_path / "adopted.pids", tmp_path / "go"
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("ADOPTED_PIDS", str(pidfile))
    monkeypatch.setenv("AGENT_GO", str(gofile))
    monkeypatch.setenv("AGENTCAP_QUOTA_DIR", str(tmp_path / "state"))

    run = RUNS[mode](tmp_path)
    result: dict[str, object] = {}

    def target() -> None:
        try:
            result["code"] = run()
        except BaseException as exc:  # pragma: no cover - reported below
            result["error"] = exc

    runner_thread = threading.Thread(target=target, daemon=True)
    runner_thread.start()
    adopted: list[int] = []
    own: list[tuple[subprocess.Popen, int]] = []
    try:
        assert _wait_until(pidfile.exists, 20), f"the agent never started: {result!r}"
        adopted = [int(pid) for pid in pidfile.read_text().split()]
        # Today all three stay AgentCap's zombies until the run ends.  Whether
        # they are still there is not the point (a reaper that can prove a
        # zombie was adopted may take it); they only have to have exited.
        assert _wait_until(lambda: all(_exited(pid) for pid in adopted), 10), (
            "the agent's helpers never exited: " + repr({pid: _stat(pid) for pid in adopted})
        )

        # Children of this process started during the run by another thread:
        # in AgentCap's own group (like `agy -p /quota`), leading a new session
        # (the same /proc picture as the adopted setsid helper), in a new group.
        own = [
            (subprocess.Popen([sys.executable, "-c", "raise SystemExit(7)"]), 7),
            (subprocess.Popen([sys.executable, "-c", "raise SystemExit(9)"],
                              start_new_session=True), 9),
            (subprocess.Popen([sys.executable, "-c", "raise SystemExit(11)"],
                              process_group=0), 11),
        ]
        assert _wait_until(lambda: all(_exited(p.pid) for p, _ in own), 10)
        time.sleep(RUN_WINDOW)  # the run keeps monitoring; nothing may reap them

        for child, code in own:
            assert _our_zombie(child.pid), (
                f"{mode}: something took the exit status of child {child.pid} during the run"
            )
            assert child.wait(timeout=5) == code
    finally:
        for child, _code in own:  # never leave one for the end-of-run sweep
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:  # pragma: no cover - defensive
                child.kill()
                child.wait(timeout=5)
        gofile.write_text("go")
        runner_thread.join(timeout=60)

    assert not runner_thread.is_alive(), f"{mode} run did not finish"
    assert "error" not in result, result.get("error")
    assert result.get("code") == AGENT_EXIT, f"{mode}: the agent's exit status was lost"
    assert not any(_our_zombie(pid) for pid in adopted), "an adopted zombie outlived the run"
