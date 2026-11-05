"""The Linux parent-death lifeline: AgentCap's child must not outlive AgentCap."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from agentcap import lifeline

SHIM = Path(lifeline.__file__).with_name("_lifeline_exec.py")


def test_guarded_argv_wraps_the_command_on_linux():
    argv = lifeline.guarded_argv(["echo", "hi"])
    if not lifeline.supported():
        assert argv == ["echo", "hi"]
        return
    assert argv[:3] == [sys.executable, "-I", str(SHIM)]
    assert argv[3:6] == ["--parent", str(os.getpid()), "--"]
    assert argv[6:] == ["echo", "hi"]


def test_shim_execs_the_command():
    result = subprocess.run(
        [sys.executable, str(SHIM), "--parent", str(os.getpid()), "--",
         sys.executable, "-c", "print('lifeline-ok')"],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0
    assert result.stdout.strip() == "lifeline-ok"


def test_shim_refuses_when_the_parent_already_changed():
    result = subprocess.run(
        [sys.executable, str(SHIM), "--parent", "1", "--", sys.executable, "-c", "pass"],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 137


def test_shim_rejects_malformed_arguments():
    result = subprocess.run([sys.executable, str(SHIM)], capture_output=True, text=True, timeout=30)
    assert result.returncode == 2


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="PR_SET_PDEATHSIG is Linux-only")
def test_child_dies_when_its_parent_is_sigkilled():
    parent_code = f"""
import os, subprocess, sys, time
child = subprocess.Popen([
    sys.executable, {str(SHIM)!r}, "--parent", str(os.getpid()), "--",
    sys.executable, "-c", "import time; time.sleep(60)",
])
print(child.pid, flush=True)
time.sleep(60)
"""
    parent = subprocess.Popen(
        [sys.executable, "-c", parent_code], stdout=subprocess.PIPE, text=True
    )
    try:
        line = parent.stdout.readline().strip()
        assert line.isdigit(), line
        grandchild = int(line)
        time.sleep(0.5)  # let the shim reach prctl/exec
        parent.kill()
        parent.wait(timeout=10)
        deadline = time.time() + 5
        while time.time() < deadline and Path(f"/proc/{grandchild}").exists():
            time.sleep(0.05)
        assert not Path(f"/proc/{grandchild}").exists(), "guarded child survived AgentCap's SIGKILL"
    finally:
        if parent.poll() is None:
            parent.kill()
            parent.wait(timeout=10)


def test_cli_reports_a_missing_command_as_could_not_start(tmp_path, monkeypatch, capfd):
    import agentcap.cli as cli

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("AGENTCAP_DB", str(tmp_path / "state" / "ledger.sqlite3"))
    monkeypatch.setenv("AGENTCAP_RECEIPT_DIR", str(tmp_path / "state" / "receipts"))

    code = cli.main(["run", "--cap", "1", "--", "agentcap-test-no-such-command"])
    err = capfd.readouterr().err

    assert code == 127
    assert "agentcap: could not start the command (No such file or directory)" in err
    assert "no model requests observed" not in err
    assert "lifeline:" not in err, "the shim's own message duplicates the CLI's"


@pytest.mark.skipif(not lifeline.supported(), reason="the lifeline shim is Linux-only")
def test_spawn_execs_the_command_in_place():
    # exec keeps the PID: the process Popen returned *is* the agent, still our child.
    child = lifeline.spawn(
        [sys.executable, "-c", "import os, sys; print(os.getppid() == int(sys.argv[1]))",
         str(os.getpid())],
        stdout=subprocess.PIPE, text=True,
    )
    out, _ = child.communicate(timeout=30)
    assert child.returncode == 0
    assert out.strip() == "True"


@pytest.mark.skipif(not lifeline.supported(), reason="the lifeline shim is Linux-only")
def test_spawned_agent_does_not_inherit_the_error_pipe():
    probe = (
        "import os\n"
        "found = []\n"
        "for fd in os.listdir('/proc/self/fd'):\n"
        "    try:\n"
        "        target = os.readlink(f'/proc/self/fd/{fd}')\n"
        "    except OSError:\n"
        "        continue\n"
        "    if int(fd) > 2 and target.startswith('pipe:'):\n"
        "        found.append(fd)\n"
        "print(found)\n"
    )
    child = lifeline.spawn([sys.executable, "-c", probe], stdout=subprocess.PIPE, text=True)
    out, _ = child.communicate(timeout=30)
    assert child.returncode == 0
    assert out.strip() == "[]", "the shim's error pipe leaked into the agent"


# --------------------------------------------------------- AgentCap SIGKILLed
# PR_SET_PDEATHSIG reaches only the process AgentCap started.  A launcher that
# spawns the real agent as a child (Codex's npm wrapper on Node < 22.15 has no
# process.execve) and the agent's tool processes are in the agent's process
# group but are not that process: they must die with AgentCap too.
FAKE_AGENT = """#!{python}
import json, os, subprocess, sys, time
tool = subprocess.Popen(["sleep", "60"])  # same process group, not daemonized
with open(os.environ["FAKE_TOOL_PIDFILE"], "w") as handle:
    handle.write(str(tool.pid))
print(json.dumps({{"type": "step_finish", "part": {{"type": "step-finish", "cost": 0,
      "tokens": {{"input": 1, "output": 1, "reasoning": 0, "cache": {{"read": 0, "write": 0}}}}}}}}),
      flush=True)
time.sleep(60)
"""

AGENTCAP_CLI = "import sys; from agentcap.cli import main; raise SystemExit(main(sys.argv[1:]))"


def _pid_alive(pid: int) -> bool:
    try:
        state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
    except (OSError, IndexError):
        return False
    return state != "Z"


def _sigkill_agentcap_and_check_tool(tmp_path, args):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for name in ("fake-agent", "opencode", "codex"):
        script = bindir / name
        script.write_text(FAKE_AGENT.format(python=sys.executable))
        script.chmod(0o755)
    pidfile = tmp_path / "tool.pid"
    state = tmp_path / "state"
    env = {
        **os.environ,
        "PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}",
        "HOME": str(tmp_path),
        "AGENTCAP_DB": str(state / "ledger.sqlite3"),
        "AGENTCAP_RECEIPT_DIR": str(state / "receipts"),
        "AGENTCAP_QUOTA_DIR": str(state / "quota"),
        "CODEX_HOME": str(tmp_path / "codex"),
        "FAKE_TOOL_PIDFILE": str(pidfile),
    }
    agentcap = subprocess.Popen(
        [sys.executable, "-c", AGENTCAP_CLI, *args],
        env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
    )
    tool = None
    try:
        deadline = time.time() + 15
        while time.time() < deadline and not (pidfile.exists() and pidfile.read_text()):
            time.sleep(0.05)
        assert pidfile.exists() and pidfile.read_text(), "the fake agent never started its tool"
        tool = int(pidfile.read_text())
        assert _pid_alive(tool)
        agentcap.kill()  # SIGKILL: no AgentCap cleanup code can run
        agentcap.wait(timeout=10)
        deadline = time.time() + 5
        while time.time() < deadline and _pid_alive(tool):
            time.sleep(0.05)
        assert not _pid_alive(tool), "the agent's tool process outlived AgentCap's SIGKILL"
    finally:
        if agentcap.poll() is None:
            agentcap.kill()
            agentcap.wait(timeout=10)
        if tool is not None and _pid_alive(tool):
            os.kill(tool, 9)


@pytest.mark.skipif(not lifeline.supported(), reason="the lifeline is Linux-only")
def test_agent_group_dies_with_a_sigkilled_agentcap_in_cap_mode(tmp_path):
    _sigkill_agentcap_and_check_tool(tmp_path, ["run", "--cap", "1", "--", "fake-agent"])


@pytest.mark.skipif(not lifeline.supported(), reason="the lifeline is Linux-only")
def test_agent_group_dies_with_a_sigkilled_agentcap_in_harness_mode(tmp_path):
    _sigkill_agentcap_and_check_tool(
        tmp_path, ["run", "--tokens", "1000000000", "--", "opencode", "run", "hi"])


@pytest.mark.skipif(not lifeline.supported(), reason="the lifeline is Linux-only")
def test_agent_group_dies_with_a_sigkilled_agentcap_in_quota_mode(tmp_path):
    _sigkill_agentcap_and_check_tool(
        tmp_path, ["run", "--quota", "50%", "--", "codex", "exec", "hi"])


@pytest.mark.skipif(not lifeline.supported(), reason="the lifeline is Linux-only")
def test_disarmed_watchdog_leaves_the_group_alone():
    group = subprocess.Popen(["sleep", "60"], start_new_session=True)
    watchdog = lifeline.GroupWatchdog()
    try:
        assert watchdog.start() is True
        watchdog.watch(group.pid)
        watchdog.disarm()  # AgentCap finished normally: nothing may be killed
        time.sleep(0.3)
        assert group.poll() is None, "a disarmed watchdog killed the agent group"
    finally:
        group.kill()
        group.wait(timeout=10)


@pytest.mark.skipif(not lifeline.supported(), reason="the lifeline shim is Linux-only")
def test_shim_only_sees_the_standard_library(tmp_path, monkeypatch):
    # The shim sits in the agentcap package directory.  Run as a plain script,
    # that directory (and the user's PYTHONPATH) would come first on sys.path,
    # so a module named like a stdlib one -- a future agentcap/signal.py, or a
    # project's types.py on PYTHONPATH -- would break every guarded run.
    shim_dir = tmp_path / "pkg"
    shim_dir.mkdir()
    shim = shim_dir / "_lifeline_exec.py"
    shim.write_text(SHIM.read_text())
    (shim_dir / "signal.py").write_text("raise ImportError('package dir shadowed the stdlib')\n")
    user_path = tmp_path / "project"
    user_path.mkdir()
    (user_path / "types.py").write_text("raise ImportError('PYTHONPATH shadowed the stdlib')\n")
    monkeypatch.setattr(lifeline, "_SHIM", shim)

    child = lifeline.spawn(
        [sys.executable, "-I", "-c", "print('agent-ran')"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        env={**os.environ, "PYTHONPATH": str(user_path)},
    )
    out, err = child.communicate(timeout=30)

    assert child.returncode == 0, err
    assert out.strip() == "agent-ran"
