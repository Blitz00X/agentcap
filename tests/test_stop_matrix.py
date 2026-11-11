"""Stop-path matrix for quota mode (AgentCap 0.4, stage 1).

Every test here drives :class:`agentcap.quota.QuotaRunner` end to end with a
fake agent executable and checks the three things a stop path must prove: the
*real* exit code, the receipt it leaves, and -- where a process tree is
involved -- that no guarded process survived.

The guards themselves are unit-tested elsewhere: signing and identity handling
in ``test_procstop.py``, the parent-death lifeline in ``test_lifeline.py``, the
loop/error digests in ``test_loop.py``, and hooks/settings in ``test_quota.py``.
This module covers the integration paths of the 0.4 stop matrix that were not
exercised end to end: the stop leaves no survivor, the signal ladder keeps
escalating past an agent that ignores SIGINT/SIGTERM, Claude's 8-failure error
breaker reaches exit 4, a missing Codex schema fails closed with exit 5, and
receipts stay private and absent in observe-only mode.
"""

from __future__ import annotations

import json
import os
import stat
import sys
import textwrap
import time
from pathlib import Path

import pytest

from agentcap import quota as q

# One fake Codex that can play every Codex scenario in this module.  It never
# imports AgentCap: it writes the documented rollout lines only, like the CLI.
MATRIX_CODEX = textwrap.dedent(
    '''\
    #!__PYTHON__
    """Fake Codex: rollout token_count lines, a helper in its group and one that
    leaves the group (setsid), optional SIGINT/SIGTERM immunity and a tool loop."""
    import datetime, json, os, signal, subprocess, sys, time

    home = os.environ["CODEX_HOME"]
    day = datetime.date.today()
    session_dir = os.path.join(home, "sessions", f"{day:%Y}", f"{day:%m}", f"{day:%d}")
    os.makedirs(session_dir, exist_ok=True)
    path = os.path.join(session_dir, "rollout-%d-matrix.jsonl" % time.time())
    week = float(os.environ.get("MATRIX_START", "40"))
    step = float(os.environ.get("MATRIX_STEP", "1"))
    fh = open(path, "a")

    def line(obj):
        fh.write(json.dumps(obj) + "\\n")
        fh.flush()

    def reading(value):
        payload = {"type": "token_count", "info": {}}
        if not os.environ.get("MATRIX_NO_LIMITS"):
            payload["rate_limits"] = {
                "primary": {"used_percent": 10.0, "window_minutes": 300,
                            "resets_at": int(time.time() + 3600)},
                "secondary": {"used_percent": value, "window_minutes": 10080,
                              "resets_at": int(time.time() + 86400)},
            }
        line({"timestamp": "t", "type": "event_msg", "payload": payload})

    def on_int(sig, frame):
        # real Codex writes the interrupted response's usage, then turn_aborted
        reading(week + step)
        line({"tag": "t", "type": "event_msg", "payload": {"type": "turn_aborted"}})
        os._exit(130)

    if os.environ.get("MATRIX_IGNORE_SIGNALS"):
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    else:
        signal.signal(signal.SIGINT, on_int)

    # one helper inside the agent's process group, one that escapes it
    inside = subprocess.Popen(["sleep", "120"])
    outside = subprocess.Popen(
        ["setsid", sys.executable, "-c", "import time; time.sleep(120)"])
    with open(os.environ["MATRIX_PIDS"], "w") as out:
        out.write(f"{os.getpid()} {inside.pid} {outside.pid}")

    line({"type": "session_meta", "payload": {"id": "matrix"}})
    line({"type": "event_msg", "payload": {"type": "task_started"}})
    for i in range(int(os.environ.get("MATRIX_LIMIT", "400"))):
        week += step
        reading(week)
        if os.environ.get("MATRIX_LOOP"):
            line({"type": "response_item", "payload": {
                "type": "function_call", "name": "shell",
                "arguments": json.dumps({"command": ["pytest"]}), "call_id": f"c{i}"}})
            line({"type": "response_item", "payload": {
                "type": "function_call_output", "call_id": f"c{i}",
                "output": json.dumps({"output": "x", "metadata": {"exit_code": 0}})}})
        time.sleep(0.1)
    '''
)

# Fake Claude Code: renders the statusline the way Claude Code does, reports
# failing tool results through the PostToolUseFailure hook, then asks the
# PreToolUse hook before its next tool call and stops when it is denied.
MATRIX_CLAUDE = textwrap.dedent(
    '''\
    #!__PYTHON__
    import json, os, subprocess, sys, time

    args = sys.argv[1:]
    assert args[0] == "--settings", args
    settings = json.load(open(args[1]))
    statusline = settings["statusLine"]["command"]
    pre = settings["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
    post = settings["hooks"]["PostToolUseFailure"][0]["hooks"][0]["command"]

    for i in range(30):
        payload = {"session_id": "matrix", "model": {"display_name": "Opus"},
                   "rate_limits": {
                       "five_hour": {"used_percentage": 10, "resets_at": time.time() + 3600},
                       "seven_day": {"used_percentage": 40 + i * 0.05,
                                     "resets_at": time.time() + 86400}}}
        subprocess.run(statusline, shell=True, input=json.dumps(payload),
                       capture_output=True, text=True)
        for _ in range(int(os.environ.get("MATRIX_FAILS", "8"))):
            subprocess.run(post, shell=True,
                           input=json.dumps({"hook_event_name": "PostToolUseFailure"}),
                           capture_output=True, text=True)
        answer = subprocess.run(
            pre, shell=True,
            input=json.dumps({"hook_event_name": "PreToolUse", "tool_name": "Bash",
                              "tool_input": {"command": f"step {i}"}}),
            capture_output=True, text=True).stdout.strip()
        if answer:
            decision = json.loads(answer)
            if decision["hookSpecificOutput"]["permissionDecision"] == "deny":
                print("DENIED", decision["stopReason"], flush=True)
                sys.exit(0)
        print("TOOL", i, flush=True)
        time.sleep(0.1)
    '''
)


def _install(tmp_path: Path, monkeypatch, name: str, source: str) -> Path:
    bindir = tmp_path / "matrixbin"
    bindir.mkdir(exist_ok=True)
    exe = bindir / name
    exe.write_text(source.replace("__PYTHON__", sys.executable))
    exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("AGENTCAP_QUOTA_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    return exe


def _runner(argv, tmp_path: Path, *, quota: float = 5.0, enforce: bool = True, **kw):
    err = __import__("io").StringIO()
    runner = q.QuotaRunner(
        argv, quota=quota, enforce=enforce, stderr=err, poll_s=0.05,
        claude_grace_s=0.4, final_read_s=1.0, receipt_dir=tmp_path / "receipts",
        cwd=tmp_path, home=tmp_path / "home", **kw,
    )
    return runner, err


def _receipt(tmp_path: Path) -> dict:
    files = list((tmp_path / "receipts").glob("*.json"))
    assert len(files) == 1, files
    return json.loads(files[0].read_text())


def _alive(pid: int) -> bool:
    try:
        state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
    except OSError:
        return False
    return state not in ("Z", "X")


def _wait_gone(pids, timeout: float = 10.0) -> list[int]:
    deadline = time.time() + timeout
    left = [pid for pid in pids if _alive(pid)]
    while left and time.time() < deadline:
        time.sleep(0.05)
        left = [pid for pid in left if _alive(pid)]
    return left


def _pids(tmp_path: Path) -> tuple[int, int, int]:
    return tuple(int(part) for part in (tmp_path / "matrix.pids").read_text().split())


# --------------------------------------------------------------- quota stop
def test_codex_quota_stop_exits_3_with_receipt_and_leaves_no_survivor(tmp_path, monkeypatch):
    """Quota stop (exit 3): receipt written, agent and both helpers are gone."""

    _install(tmp_path, monkeypatch, "codex", MATRIX_CODEX)
    monkeypatch.setenv("MATRIX_PIDS", str(tmp_path / "matrix.pids"))
    runner, err = _runner(["codex", "exec", "work"], tmp_path)
    assert runner.run() == q.EXIT_QUOTA_EXCEEDED
    assert runner.summary.reason == "quota_exceeded"
    receipt = _receipt(tmp_path)
    assert receipt["type"] == "agentcap_quota_receipt"
    assert receipt["exit_code"] == 3 and receipt["reason"] == "quota_exceeded"
    assert receipt["agent"] == "codex" and receipt["measure"] == "account_increase"
    assert receipt["final_read"] == "complete"
    receipt_file = next((tmp_path / "receipts").glob("*.json"))
    assert stat.S_IMODE(receipt_file.stat().st_mode) == 0o600
    agent, inside, outside = _pids(tmp_path)
    assert not _alive(agent)
    assert _wait_gone([inside, outside]) == [], err.getvalue()
    assert "left the agent's process group" in err.getvalue()


def test_codex_stop_escalates_past_ignored_sigint_and_sigterm(tmp_path, monkeypatch):
    """SIGINT is not enough: the ladder reaches SIGKILL and still exits 3."""

    _install(tmp_path, monkeypatch, "codex", MATRIX_CODEX)
    monkeypatch.setenv("MATRIX_PIDS", str(tmp_path / "matrix.pids"))
    monkeypatch.setenv("MATRIX_IGNORE_SIGNALS", "1")
    runner, err = _runner(["codex", "exec", "work"], tmp_path)
    started = time.time()
    assert runner.run() == q.EXIT_QUOTA_EXCEEDED
    elapsed = time.time() - started
    # SIGINT wait (5 s) + SIGTERM wait (3 s) must pass before SIGKILL lands.
    assert 5.0 <= elapsed < 30.0, elapsed
    agent, inside, outside = _pids(tmp_path)
    assert not _alive(agent)
    assert _wait_gone([inside, outside]) == [], err.getvalue()


# ------------------------------------------------------- loop/error breaker
def test_codex_loop_breaker_exits_4_with_receipt(tmp_path, monkeypatch):
    """Loop breaker (exit 4): the same tool call four times in a row."""

    _install(tmp_path, monkeypatch, "codex", MATRIX_CODEX)
    monkeypatch.setenv("MATRIX_PIDS", str(tmp_path / "matrix.pids"))
    monkeypatch.setenv("MATRIX_LOOP", "1")
    monkeypatch.setenv("MATRIX_STEP", "0.01")
    runner, err = _runner(["codex", "exec", "work"], tmp_path, quota=50.0)
    assert runner.run() == q.EXIT_LOOP_DETECTED
    assert runner.summary.reason == "loop_detected"
    assert "same tool call 4 times" in err.getvalue()
    receipt = _receipt(tmp_path)
    assert receipt["exit_code"] == 4 and receipt["reason"] == "loop_detected"


@pytest.mark.parametrize("argv", [["claude", "work"], ["claude", "-p", "work"]])
def test_claude_error_breaker_stops_after_eight_failures(tmp_path, monkeypatch, argv):
    """Error breaker (exit 4), interactive and headless: 8 failing tool calls."""

    _install(tmp_path, monkeypatch, "claude", MATRIX_CLAUDE)
    runner, err = _runner(argv, tmp_path, quota=50.0)
    assert runner.run() == q.EXIT_LOOP_DETECTED
    assert runner.summary.reason == "loop_detected"
    receipt = _receipt(tmp_path)
    assert receipt["exit_code"] == 4 and receipt["reason"] == "loop_detected"
    assert (runner.job_dir / "denies.log").exists()
    # The deny payload (continue: false) and the interactive "session stays open"
    # message need a running session to observe; those are deterministic in
    # test_quota.py::test_e2e_interactive_claude_is_not_sigint_when_hook_denies
    # and test_quota.py::test_e2e_headless_claude_still_sigint.


# ------------------------------------------------------------- fail-closed
def test_codex_missing_schema_fails_closed_with_exit_5(tmp_path, monkeypatch):
    """No subscription rate_limits in the log: stop, exit 5, say why."""

    _install(tmp_path, monkeypatch, "codex", MATRIX_CODEX)
    monkeypatch.setenv("MATRIX_PIDS", str(tmp_path / "matrix.pids"))
    monkeypatch.setenv("MATRIX_NO_LIMITS", "1")
    runner, err = _runner(["codex", "exec", "work"], tmp_path)
    assert runner.run() == q.EXIT_GUARD_FAILED
    assert runner.summary.reason == "guard_failed"
    assert "no subscription rate_limits" in err.getvalue()
    receipt = _receipt(tmp_path)
    assert receipt["exit_code"] == 5 and receipt["reason"] == "guard_failed"
    # never present an unreadable run as protected
    assert runner.summary.observed is False


# --------------------------------------------------------------- receipts
def test_observe_only_mode_writes_no_receipt(tmp_path, monkeypatch):
    """Free/observe-only: the same stop paths report, but nothing is receipted."""

    _install(tmp_path, monkeypatch, "codex", MATRIX_CODEX)
    monkeypatch.setenv("MATRIX_PIDS", str(tmp_path / "matrix.pids"))
    monkeypatch.setenv("MATRIX_STEP", "0.01")
    monkeypatch.setenv("MATRIX_LIMIT", "20")
    runner, err = _runner(["codex", "exec", "work"], tmp_path, quota=50.0, enforce=False)
    assert runner.run() == 0
    assert not list((tmp_path / "receipts").glob("*.json"))
    assert "Pro's quota mode would have stopped this job" not in err.getvalue()
