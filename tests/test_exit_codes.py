"""R5 + R6: one exit-code policy for every ``agentcap run`` path.

2 setup/auth refusal · 3 cap · 4 loop · 5 internal (state/lock/license/HOME or
an internal fault) · 127 only when the child command itself cannot be started.
"""

from __future__ import annotations

import errno
import io
import os
import socket
import sys
import time
from pathlib import Path

import httpx
import pytest

import agentcap.cli as cli
from agentcap.alerts import AlertDispatcher
from agentcap.failsafe import EXIT_GUARD_FAILED
from agentcap.ledger import Ledger
from agentcap.license import FREE_FEATURES
from agentcap.runner import EXIT_INTERNAL_ERROR, Runner

SLEEPER = [sys.executable, "-c", "import time; time.sleep(30)"]


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    """A fresh HOME with every AgentCap state path inside it."""

    home = tmp_path / "home"
    home.mkdir()
    for name in (
        "AGENTCAP_DB",
        "AGENTCAP_RECEIPT_DIR",
        "AGENTCAP_QUOTA_DIR",
        "AGENTCAP_LICENSE_FILE",
        "AGENTCAP_CAP",
        "AGENTCAP_ALLOW_PARALLEL",
        "GEMINI_API_KEY",
        "GOOGLE_API_KEY",
        "AGENTCAP_WEBHOOK_URL",
        "AGENTCAP_TELEGRAM_BOT_TOKEN",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    return home


def _symlinked_lock(tmp_path: Path, monkeypatch) -> Path:
    state = tmp_path / "quota-state"
    state.mkdir(mode=0o700)
    target = tmp_path / "elsewhere"
    target.write_text("")
    (state / "quota.lock").symlink_to(target)
    monkeypatch.setenv("AGENTCAP_QUOTA_DIR", str(state))
    return state / "quota.lock"


def _readonly_home(home: Path):
    home.chmod(0o500)
    if os.access(home, os.W_OK):  # root ignores the mode bits
        pytest.skip("cannot make HOME unwritable as this user")


# ------------------------------------------------------------ R5: state errors
QUOTA_ARGS = ["run", "--quota", "5%", "--", "codex", "exec", "hi"]
HARNESS_ARGS = ["run", "--tokens", "100", "--quota", "5%", "--", "agy", "-p", "hi"]


@pytest.mark.parametrize("args", [QUOTA_ARGS, HARNESS_ARGS], ids=["quota", "tokens"])
def test_symlinked_quota_lock_is_an_internal_state_error(isolated, tmp_path, monkeypatch, capfd, args):
    lock = _symlinked_lock(tmp_path, monkeypatch)

    code = cli.main(args)
    err = capfd.readouterr().err

    assert code == 5 == EXIT_GUARD_FAILED
    assert "could not start" not in err
    assert "internal state error" in err
    assert "quota lock" in err and str(lock) in err


def test_unwritable_home_is_an_internal_state_error_for_cap(isolated, capfd):
    _readonly_home(isolated)
    try:
        code = cli.main(["run", "--cap", "1", "--", sys.executable, "-c", "pass"])
    finally:
        isolated.chmod(0o700)
    err = capfd.readouterr().err

    assert code == 5
    assert "could not start" not in err
    assert "internal state error" in err
    assert str(isolated) in err


@pytest.mark.parametrize("args", [QUOTA_ARGS, HARNESS_ARGS], ids=["quota", "tokens"])
def test_unwritable_home_is_an_internal_state_error_for_quota_paths(isolated, capfd, args):
    _readonly_home(isolated)
    try:
        code = cli.main(args)
    finally:
        isolated.chmod(0o700)
    err = capfd.readouterr().err

    assert code == 5
    assert "could not start" not in err
    assert "internal state error" in err


@pytest.mark.parametrize(
    "args",
    [
        ["run", "--cap", "1", "--", "agentcap-test-no-such-command"],
        ["run", "--quota", "5%", "--", "codex", "exec", "hi"],
        ["run", "--tokens", "100", "--", "agy", "-p", "hi"],
    ],
    ids=["cap", "quota", "tokens"],
)
def test_missing_command_is_still_127_on_every_path(isolated, tmp_path, monkeypatch, capfd, args):
    empty = tmp_path / "emptybin"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    monkeypatch.setenv("AGENTCAP_QUOTA_DIR", str(tmp_path / "quota-state"))

    code = cli.main(args)
    err = capfd.readouterr().err

    assert code == 127
    assert "agentcap: could not start the command (No such file or directory)" in err
    assert "internal state error" not in err
    assert "no model requests observed" not in err


@pytest.mark.parametrize(
    "target,args",
    [
        ("agentcap.quota.GroupWatchdog.start", QUOTA_ARGS),
        ("agentcap.harness.GroupWatchdog.start", HARNESS_ARGS),
    ],
    ids=["quota", "tokens"],
)
def test_internal_oserror_before_spawn_is_5_not_127(isolated, tmp_path, monkeypatch, capfd, target, args):
    monkeypatch.setenv("AGENTCAP_QUOTA_DIR", str(tmp_path / "quota-state"))

    def boom(self):
        raise OSError(errno.EACCES, "Permission denied", str(tmp_path / "watch"))

    monkeypatch.setattr(target, boom)
    code = cli.main(args)
    err = capfd.readouterr().err

    assert code == 5
    assert "could not start" not in err
    assert "internal state error" in err and str(tmp_path / "watch") in err


def test_joblock_wraps_a_symlinked_lock_in_an_internal_state_error(tmp_path):
    from agentcap.failsafe import InternalStateError, JobLock

    target = tmp_path / "t"
    target.write_text("")
    (tmp_path / "quota.lock").symlink_to(target)
    with pytest.raises(InternalStateError) as info:
        JobLock(tmp_path / "quota.lock").acquire()
    assert info.value.path == tmp_path / "quota.lock"
    assert "open the quota lock" in str(info.value)
    assert isinstance(info.value, OSError)  # existing `except OSError` callers still work
    assert info.value.errno == errno.ELOOP


# -------------------------------------------------- R6: Runner internal errors
def _runner(tmp_path: Path, argv=SLEEPER, **kwargs) -> tuple[Runner, io.StringIO]:
    stderr = io.StringIO()
    runner = Runner(
        argv,
        cap="1",
        ledger=Ledger(tmp_path / f"ledger-{time.time_ns()}.db"),
        transport=httpx.MockTransport(lambda r: httpx.Response(500)),
        stderr=stderr,
        alerts=AlertDispatcher(features=FREE_FEATURES, stderr=stderr, background=False),
        features=FREE_FEATURES,
        poll_interval=0.01,
        term_timeout=2.0,
        **kwargs,
    )
    return runner, stderr


def _assert_internal_stop(runner: Runner, stderr: io.StringIO, code: int) -> None:
    out = stderr.getvalue()
    assert code == EXIT_INTERNAL_ERROR == 5
    assert runner.summary is not None
    assert runner.summary.exit_code == 5
    assert runner.summary.reason == "internal_error"
    assert "exit code: 5" in out
    assert "exit code: 1" not in out
    assert "reason: internal_error" in out
    assert "agent stopped by an internal error" in out
    assert "no model requests observed" not in out
    assert "saw no API requests" not in out
    assert "could not start" not in out


@pytest.mark.parametrize("exc", [RuntimeError("boom"), ValueError("bad"), OSError(errno.EIO, "I/O error")])
def test_internal_error_while_monitoring_stops_the_agent_and_exits_5(tmp_path, monkeypatch, exc):
    runner, stderr = _runner(tmp_path)

    def broken_monitor():
        time.sleep(0.2)  # the agent is running
        raise exc

    monkeypatch.setattr(runner, "_monitor", broken_monitor)
    started = time.monotonic()
    code = runner.run()

    _assert_internal_stop(runner, stderr, code)
    assert runner.child is not None and runner.child.poll() is not None, "agent left running"
    assert time.monotonic() - started < 15


def test_internal_oserror_while_preparing_the_child_exits_5(tmp_path, monkeypatch):
    def broken_prepare(*_a, **_k):
        raise OSError(errno.EACCES, "Permission denied", str(tmp_path / "adapter-config"))

    monkeypatch.setattr("agentcap.runner.prepare_child", broken_prepare)
    runner, stderr = _runner(tmp_path)
    code = runner.run()

    _assert_internal_stop(runner, stderr, code)
    assert runner.child is None
    assert str(tmp_path / "adapter-config") in stderr.getvalue()


def test_proxy_port_in_use_is_an_internal_error_not_a_start_failure(tmp_path):
    busy = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    busy.bind(("127.0.0.1", 0))
    busy.listen(1)
    try:
        runner, stderr = _runner(tmp_path, port=busy.getsockname()[1])
        code = runner.run()
    finally:
        busy.close()
    _assert_internal_stop(runner, stderr, code)
    assert runner.child is None


def test_internal_error_is_sticky_over_a_late_kill_reason(tmp_path, monkeypatch):
    runner, stderr = _runner(tmp_path)

    def broken_monitor():
        runner._trip("cap_exceeded", {})
        raise RuntimeError("boom")

    monkeypatch.setattr(runner, "_monitor", broken_monitor)
    code = runner.run()
    _assert_internal_stop(runner, stderr, code)


def test_child_start_failure_summary_says_127_then_reraises(tmp_path):
    runner, stderr = _runner(tmp_path, ["agentcap-test-no-such-command"])
    with pytest.raises(FileNotFoundError):
        runner.run()
    out = stderr.getvalue()
    assert runner.summary is not None and runner.summary.exit_code == 127
    assert "exit code: 127" in out
    assert "internal_error" not in out
    assert "no model requests observed" not in out


def test_adapter_conflict_is_still_2(tmp_path, monkeypatch):
    from agentcap.adapters import AdapterConflict

    def conflict(*_a, **_k):
        raise AdapterConflict("conflicting routing")

    monkeypatch.setattr("agentcap.runner.prepare_child", conflict)
    runner, stderr = _runner(tmp_path)
    assert runner.run() == 2
    assert "agentcap: conflicting routing" in stderr.getvalue()
    assert runner.summary is not None and runner.summary.exit_code == 2


def test_cli_returns_5_when_the_runner_hits_an_internal_error(isolated, monkeypatch, capfd):
    def broken_monitor(self):
        raise RuntimeError("boom")

    monkeypatch.setattr(Runner, "_monitor", broken_monitor)
    code = cli.main(["run", "--cap", "1", "--", *SLEEPER])
    err = capfd.readouterr().err
    assert code == 5
    assert "exit code: 5" in err
    assert "could not start" not in err


# ------------------------------------------------------- regressions: 2 / 3 / 4
def test_regression_cap_loop_auth_codes_are_unchanged():
    from agentcap import harness, quota, runner

    assert runner.EXIT_CAP_EXCEEDED == 3
    assert runner.EXIT_LOOP_DETECTED == 4
    assert runner.EXIT_UPSTREAM_AUTH_FAILED == 2
    assert runner.EXIT_REASONS["internal_error"] == 5
    assert harness.EXIT_CAP_EXCEEDED == 3 and harness.EXIT_LOOP_DETECTED == 4
    assert quota.EXIT_QUOTA_EXCEEDED == 3 and quota.EXIT_LOOP_DETECTED == 4


def test_regression_another_quota_job_holding_the_lock_is_still_2(isolated, tmp_path, monkeypatch, capfd):
    from agentcap.failsafe import JobLock

    state = tmp_path / "quota-state"
    monkeypatch.setenv("AGENTCAP_QUOTA_DIR", str(state))
    held = JobLock(state / "quota.lock")
    held.acquire()
    try:
        code = cli.main(QUOTA_ARGS)
    finally:
        held.release()
    err = capfd.readouterr().err
    assert code == 2
    assert "another AgentCap quota job is already running" in err
