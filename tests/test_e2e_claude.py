"""End-to-end: a fake `claude` executable, the real proxy/runner and the fake
Anthropic upstream served over real HTTP on loopback."""
from __future__ import annotations

import io
import json
import os
import socket
import stat
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path

import pytest
import uvicorn

from agentcap.devtools.fake_upstream import create_app as fake_app
from agentcap.ledger import Ledger
from agentcap.license import FREE_FEATURES, PRO_FEATURES
from agentcap.runner import Runner

SRC = str(Path(__file__).resolve().parents[1] / "src")

FAKE_CLAUDE = textwrap.dedent('''\
    #!{python}
    """Behaves like `claude -p`: POSTs /v1/messages to ANTHROPIC_BASE_URL."""
    import json, os, sys, httpx
    base = os.environ["ANTHROPIC_BASE_URL"]
    mode = os.environ.get("FAKE_MODE", "ok")
    n = int(os.environ.get("FAKE_N", "1"))
    body = {{"model": "claude-sonnet-5-5", "max_tokens": int(os.environ.get("FAKE_MAX", "800")),
            "stream": mode == "stream",
            "system": [{{"type": "text", "text": "you are a coder " * 200, "cache_control": {{"type": "ephemeral"}}}}],
            "messages": [{{"role": "user", "content": "fix the bug"}}]}}
    for i in range(n):
        if mode == "distinct":
            body["messages"][0]["content"] = f"step {{i}}"
        r = httpx.post(base + "/v1/messages?beta=true", json=body,
                       headers={{"x-api-key": os.environ.get("ANTHROPIC_API_KEY", ""), "anthropic-version": "2023-06-01"}},
                       timeout=30)
        print(i, r.status_code, flush=True)
        if r.status_code != 200:
            sys.exit(1)
''')


@pytest.fixture(scope="module")
def upstream():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(fake_app(), log_level="error", access_log=False))
    t = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    t.start()
    while not server.started:
        time.sleep(0.02)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    t.join(5)


@pytest.fixture
def claude_bin(tmp_path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    exe = bindir / "claude"
    exe.write_text(FAKE_CLAUDE.format(python=sys.executable))
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    return exe


def run(tmp_path, upstream, claude_bin, *, features=FREE_FEATURES, cap="5", env_extra=None, **kw):
    env = {"PATH": os.environ["PATH"], "HOME": str(tmp_path), "PYTHONPATH": SRC,
           "ANTHROPIC_API_KEY": "sk-ant-e2e-SECRET"}
    env.update(env_extra or {})
    err = io.StringIO()
    runner = Runner([str(claude_bin), "-p", "fix the bug"], cap=cap, features=features,
                    ledger=Ledger(tmp_path / "l.sqlite3"), env=env, stderr=err,
                    anthropic_upstream=upstream, receipt_dir=tmp_path / "receipts", **kw)
    code = runner.run()
    return code, runner, err.getvalue()


def test_e2e_metered_run(tmp_path, upstream, claude_bin):
    code, r, err = run(tmp_path, upstream, claude_bin, env_extra={"FAKE_N": "2"})
    assert code == 0, err
    assert r.summary.requests == 2 and r.summary.spent_usd > 0
    assert "SECRET" not in err


def test_e2e_streaming_run(tmp_path, upstream, claude_bin):
    code, r, err = run(tmp_path, upstream, claude_bin, env_extra={"FAKE_MODE": "stream"})
    assert code == 0, err
    assert r.summary.spent_usd > 0 and r.summary.reserved_usd == 0


def test_e2e_cap_hit_exit_3(tmp_path, upstream, claude_bin):
    code, r, err = run(tmp_path, upstream, claude_bin, cap="0.03",
                       env_extra={"FAKE_N": "50", "FAKE_MODE": "distinct", "FAKE_MAX": "2000"})
    assert code == 3, err
    assert r.summary.spent_usd <= r.summary.cap_usd


def test_e2e_pro_loop_breaker_exit_4_and_receipt(tmp_path, upstream, claude_bin):
    code, r, err = run(tmp_path, upstream, claude_bin, features=PRO_FEATURES, env_extra={"FAKE_N": "20"})
    assert code == 4, err
    receipt = json.loads(r.receipt_path.read_text())
    assert receipt["reason"] == "loop_detected" and receipt["loop_detected"] is True
    assert receipt["requests"] == 5 and receipt["command"] == "claude"
    assert "SECRET" not in r.receipt_path.read_text() and "fix the bug" not in r.receipt_path.read_text()


def test_e2e_free_loop_nudge_and_no_receipt(tmp_path, upstream, claude_bin):
    code, r, err = run(tmp_path, upstream, claude_bin, env_extra={"FAKE_N": "8"})
    assert code == 0
    assert err.count("Pro's loop breaker would have stopped this at $") == 2  # live + under summary
    assert r.receipt_path is None and not (tmp_path / "receipts").exists()


def test_e2e_bedrock_refused_exit_2(tmp_path, upstream, claude_bin):
    code, r, err = run(tmp_path, upstream, claude_bin, env_extra={"CLAUDE_CODE_USE_BEDROCK": "1"})
    assert code == 2 and "refusing to run Claude Code" in err and r.child is None


def test_e2e_cli_subprocess(tmp_path, upstream, claude_bin):
    env = {"PATH": os.environ["PATH"], "HOME": str(tmp_path), "PYTHONPATH": SRC,
           "ANTHROPIC_API_KEY": "sk-ant-e2e-SECRET", "AGENTCAP_ANTHROPIC_UPSTREAM": upstream,
           "AGENTCAP_DB": str(tmp_path / "cli.sqlite3")}
    p = subprocess.run([sys.executable, "-c", "import sys; from agentcap.cli import main; sys.exit(main())",
                        "run", "--cap", "1", "--hour-cap", "2", "--", str(claude_bin), "-p", "x"],
                       env=env, capture_output=True, text=True, timeout=60)
    assert p.returncode == 0, p.stderr
    assert "requests: 1" in p.stderr and "AGENTCAP_ANTHROPIC_UPSTREAM override active" in p.stderr
    assert "SECRET" not in p.stderr + p.stdout
