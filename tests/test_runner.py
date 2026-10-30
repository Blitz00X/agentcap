"""Runner: proxy lifecycle, child environment, kills and summaries."""

from __future__ import annotations

import io
import json
import os
import sys
import time
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from agentcap.alerts import AlertDispatcher
from agentcap.ledger import Ledger
from agentcap.license import FREE_FEATURES, LicenseStatus, features
from agentcap.runner import EXIT_CAP_EXCEEDED, EXIT_LOOP_DETECTED, Runner, cap_from_env


def _alerts(err: io.StringIO, feature_set=FREE_FEATURES) -> AlertDispatcher:
    return AlertDispatcher(features=feature_set, stderr=err, background=False)


def _chat_usage(prompt: int = 100, completion: int = 5000) -> httpx.MockTransport:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-mock",
                "object": "chat.completion",
                "choices": [],
                "usage": {
                    "prompt_tokens": prompt,
                    "completion_tokens": completion,
                },
            },
        )

    return httpx.MockTransport(handler)


def _runner(
    tmp_path: Path,
    argv,
    *,
    cap="0.01",
    transport=None,
    err=None,
    feature_set=FREE_FEATURES,
    **kwargs,
) -> tuple[Runner, io.StringIO]:
    stderr = err or io.StringIO()
    poll_interval = kwargs.pop("poll_interval", 0.01)
    runner = Runner(
        argv,
        cap=cap,
        ledger=Ledger(tmp_path / f"ledger-{time.time_ns()}.db"),
        transport=transport or _chat_usage(),
        stderr=stderr,
        alerts=_alerts(stderr, feature_set),
        features=feature_set,
        poll_interval=poll_interval,
        term_timeout=2.0,
        **kwargs,
    )
    return runner, stderr


def test_cap_exceeded_kills_child_and_exits_3(tmp_path):
    child = [
        sys.executable,
        "-c",
        (
            "import os, time, httpx\n"
            "base = os.environ['OPENAI_BASE_URL']\n"
            "for _ in range(200):\n"
            "    try:\n"
            "        httpx.post(base + '/chat/completions',\n"
            "                   json={'model': 'gpt-4o-mini',\n"
            "                         'messages': [{'role': 'user', 'content': 'hi'}]},\n"
            "                   timeout=5)\n"
            "        time.sleep(0.01)\n"
            "    except Exception:\n"
            "        time.sleep(0.01)\n"
        ),
    ]
    runner, stderr = _runner(tmp_path, child, cap="0.01")
    assert runner.alerts is not None

    code = runner.run()

    assert code == EXIT_CAP_EXCEEDED == 3
    assert runner.kill_reason == "cap_exceeded"
    assert runner.summary is not None
    assert runner.summary.reason == "cap_exceeded"
    # Clamping lets the run actually use the cap: requests 2-4 are limited to
    # what fits (the mock still reports 5000 tokens, so the 4th overshoots) and
    # only then is the next request rejected.
    assert runner.summary.requests == 4
    assert runner.summary.clamped_requests == 3
    assert runner.summary.reserved_usd == Decimal("0")
    # One allowed request may settle above the cap by at most its own cost.
    assert runner.summary.spent_usd <= Decimal("0.01") + Decimal("0.003015")

    output = stderr.getvalue()
    assert output.count("RUN CAP HIT") == 1
    assert "agentcap run summary" in output
    assert "reason: cap_exceeded" in output
    assert "reserved-but-unsettled: $0.0000" in output
    assert "clamped requests: 3" in output


def test_cap_kill_stops_upstream_traffic(tmp_path):
    calls = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(
            200,
            json={
                "choices": [],
                "usage": {"prompt_tokens": 100, "completion_tokens": 5000},
            },
        )

    child = [
        sys.executable,
        "-c",
        (
            "import os, time, httpx\n"
            "base = os.environ['OPENAI_BASE_URL']\n"
            "for _ in range(200):\n"
            "    try:\n"
            "        httpx.post(base + '/chat/completions',\n"
            "                   json={'model': 'gpt-4o-mini',\n"
            "                         'messages': [{'role': 'user', 'content': 'hi'}]},\n"
            "                   timeout=5)\n"
            "        time.sleep(0.01)\n"
            "    except Exception:\n"
            "        time.sleep(0.01)\n"
        ),
    ]
    runner, _ = _runner(
        tmp_path, child, transport=httpx.MockTransport(handler), cap="0.01"
    )
    assert runner.run() == 3
    after_exit = calls["n"]
    time.sleep(0.2)
    assert calls["n"] == after_exit
    # Budget-fit clamping allows four requests to use the cap before the kill.
    assert after_exit == 4


def test_loop_detection_kills_child_with_exit_4(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTCAP_LICENSE_FILE", str(tmp_path / "license.json"))
    monkeypatch.setattr(
        "agentcap.runner.status_from_environment",
        lambda: LicenseStatus(True, "pro", "license valid"),
    )

    child = [
        sys.executable,
        "-c",
        (
            "import os, time, httpx\n"
            "base = os.environ['OPENAI_BASE_URL']\n"
            "for _ in range(100):\n"
            "    try:\n"
            "        httpx.post(base + '/chat/completions',\n"
            "                   json={'model': 'gpt-4o-mini',\n"
            "                         'messages': [{'role': 'user', 'content': 'same'}]},\n"
            "                   timeout=5)\n"
            "    except Exception:\n"
            "        pass\n"
            "    time.sleep(0.01)\n"
        ),
    ]
    stderr = io.StringIO()
    calls = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(
            200,
            json={
                "choices": [],
                "usage": {"prompt_tokens": 100, "completion_tokens": 1},
            },
        )

    runner = Runner(
        child,
        cap="5",
        ledger=Ledger(tmp_path / "loop.db"),
        transport=httpx.MockTransport(handler),
        stderr=stderr,
        alerts=_alerts(stderr, features("pro")),
        poll_interval=0.01,
        term_timeout=2.0,
    )
    # Features come from the (monkeypatched) Pro licence status.
    assert "loop_detection" in runner.features
    code = runner.run()

    assert code == EXIT_LOOP_DETECTED == 4
    assert runner.summary.reason == "loop_detected"
    # Threshold is 5; the fifth request is blocked before reaching upstream.
    assert calls["n"] == 4
    output = stderr.getvalue()
    assert "runaway loop detected" in output
    assert "reason: loop_detected" in output


def test_child_exit_code_is_returned_and_summary_printed(tmp_path):
    child = [sys.executable, "-c", "import sys; sys.exit(7)"]
    runner, stderr = _runner(tmp_path, child)
    assert runner.run() == 7
    assert runner.summary.exit_code == 7
    assert runner.summary.requests == 0
    assert runner.summary.reason == "child_exit"
    output = stderr.getvalue()
    assert "requests: 0" in output
    assert "spent: $0.0000" in output
    assert "exit code: 7" in output


def test_child_gets_proxy_environment_and_loopback_binding(tmp_path):
    env_file = tmp_path / "child-env.json"
    script = (
        "import json, os, sys, httpx\n"
        f"path = {str(env_file)!r}\n"
        "base = os.environ['OPENAI_BASE_URL']\n"
        "assert base == os.environ['AGENTCAP_PROXY'] + '/openai/v1'\n"
        "assert base.startswith('http://127.0.0.1:')\n"
        "assert os.environ['GEMINI_API_BASE_URL'] == os.environ['GOOGLE_GEMINI_BASE_URL']\n"
        "r = httpx.get(os.environ['AGENTCAP_PROXY'] + '/_agentcap/status', timeout=5)\n"
        "json.dump({'base': base,\n"
        "           'gemini': os.environ['GOOGLE_GEMINI_BASE_URL'],\n"
        "           'run_id': os.environ['AGENTCAP_RUN_ID'],\n"
        "           'status': r.status_code}, open(path, 'w'))\n"
        "sys.exit(0)\n"
    )
    runner, _ = _runner(tmp_path, [sys.executable, "-c", script])
    assert runner.run() == 0
    payload = json.loads(env_file.read_text())
    assert payload["base"].startswith("http://127.0.0.1:")
    assert payload["gemini"].startswith("http://127.0.0.1:")
    assert payload["run_id"] == runner.run_id
    assert payload["status"] == 200


def test_runner_refuses_non_loopback_binding(tmp_path):
    with pytest.raises(ValueError):
        Runner(
            [sys.executable, "-c", "pass"],
            host="0.0.0.0",
            ledger=Ledger(tmp_path / "x.db"),
        )


class _TtyStringIO(io.StringIO):
    def isatty(self) -> bool:  # pragma: no cover - trivial
        return True


def test_zero_request_warning_when_proxy_saw_no_traffic(tmp_path):
    runner, stderr = _runner(tmp_path, [sys.executable, "-c", "pass"])
    assert runner.run() == 0
    assert runner.summary.requests == 0
    output = stderr.getvalue()
    assert "WARNING: no model requests observed" in output
    assert "README: Supported CLIs" in output
    assert "\033[33m" not in output


def test_zero_request_warning_is_yellow_on_a_tty(tmp_path):
    stderr = _TtyStringIO()
    runner, _ = _runner(tmp_path, [sys.executable, "-c", "pass"], err=stderr)
    assert runner.run() == 0
    output = stderr.getvalue()
    assert "\033[33m" in output
    assert "\033[0m" in output
    # The actionable hint follows the coloured warning, uncoloured.
    assert output.rstrip().splitlines()[-1].startswith("hint: ")


def test_no_zero_request_warning_when_requests_were_metered(tmp_path):
    child = [
        sys.executable,
        "-c",
        (
            "import os, httpx\n"
            "httpx.post(os.environ['OPENAI_BASE_URL'] + '/chat/completions',\n"
            "           json={'model': 'gpt-4o-mini',\n"
            "                 'messages': [{'role': 'user', 'content': 'hi'}]},\n"
            "           timeout=5)\n"
        ),
    ]
    runner, stderr = _runner(tmp_path, child, cap="1")
    assert runner.run() == 0
    assert runner.summary.requests == 1
    assert "no model requests observed" not in stderr.getvalue()


def test_refused_request_counts_as_observed_and_suppresses_warning(tmp_path):
    child = [
        sys.executable,
        "-c",
        (
            "import os, sys, httpx\n"
            "httpx.post(os.environ['OPENAI_BASE_URL'] + '/chat/completions',\n"
            "           json={'model': 'gpt-4o-mini',\n"
            "                 'messages': [{'role': 'user', 'content': 'hi'}],\n"
            "                 'max_completion_tokens': 100},\n"
            "           timeout=5)\n"
            "sys.exit(0)\n"
        ),
    ]
    runner, stderr = _runner(tmp_path, child, cap="0")
    assert runner.run() == EXIT_CAP_EXCEEDED
    assert runner.summary is not None
    # The proxy refused the request, but it still reached us: this is not a
    # "no model requests observed" run.
    assert runner.summary.requests >= 1
    assert runner.summary.refused_requests >= 1
    output = stderr.getvalue()
    assert "refused requests:" in output
    assert "no model requests observed" not in output


def test_settlement_over_cap_exits_3_even_when_child_exits_zero(tmp_path):
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [],
                "usage": {"prompt_tokens": 100, "completion_tokens": 5000},
            },
        )

    child = [
        sys.executable,
        "-c",
        (
            "import os, sys, httpx\n"
            "httpx.post(os.environ['OPENAI_BASE_URL'] + '/chat/completions',\n"
            "           json={'model': 'gpt-4o-mini',\n"
            "                 'messages': [{'role': 'user', 'content': 'hi'}],\n"
            "                 'max_completion_tokens': 100},\n"
            "           timeout=5)\n"
            "sys.exit(0)\n"
        ),
    ]
    # A slow monitor poll lets the child finish with exit code 0 before the
    # kill event is observed; the final reason must still be cap_exceeded.
    runner, stderr = _runner(
        tmp_path,
        child,
        cap="0.001",
        transport=httpx.MockTransport(handler),
        poll_interval=0.3,
    )
    assert runner.run() == EXIT_CAP_EXCEEDED
    assert runner.summary is not None
    assert runner.summary.reason == "cap_exceeded"
    assert runner.summary.spent_usd > Decimal("0.001")
    assert "reason: cap_exceeded" in stderr.getvalue()


def test_cap_from_env(monkeypatch):
    monkeypatch.delenv("AGENTCAP_CAP", raising=False)
    assert cap_from_env() == Decimal("5")
    monkeypatch.setenv("AGENTCAP_CAP", "0.25")
    assert cap_from_env() == Decimal("0.25")
    monkeypatch.setenv("AGENTCAP_CAP", "not-money")
    assert cap_from_env() == Decimal("5")


def _process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    # A zombie is already dead for our purposes; the important thing is that
    # it no longer runs (and will be reaped by init/subreaper).
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return True
    return stat.split()[2] != "Z"


@pytest.mark.skipif(os.name == "nt", reason="process groups are POSIX-only")
def test_terminate_kills_grandchild_after_leader_already_exited(tmp_path):
    """The saved PGID must still kill descendants when the leader is gone."""

    pidfile = tmp_path / "grandchild.pid"
    leader = (
        "import subprocess, sys, time\n"
        "child = subprocess.Popen([\n"
        "    sys.executable, '-c',\n"
        "    'import signal, time; "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)',\n"
        "])\n"
        f"open({str(pidfile)!r}, 'w').write(str(child.pid))\n"
        "time.sleep(0.3)\n"
        "sys.exit(0)\n"
    )
    runner, _ = _runner(tmp_path, [sys.executable, "-c", "pass"])
    runner.child = runner._spawn([sys.executable, "-c", leader], env=dict(os.environ))

    runner.child.wait(timeout=5)
    assert runner.child.poll() is not None
    assert runner._pgid is not None

    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and not pidfile.exists():
        time.sleep(0.02)
    grandchild = int(pidfile.read_text())
    assert _process_alive(grandchild)

    runner._terminate()

    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and _process_alive(grandchild):
        time.sleep(0.02)
    assert not _process_alive(grandchild)


# ------------------------------------------------------------ actionable hints
def _fake_cli(tmp_path: Path, name: str = "codex") -> Path:
    path = tmp_path / name
    path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    path.chmod(0o755)
    return path


def test_zero_request_hint_for_supported_cli(tmp_path):
    runner, stderr = _runner(tmp_path, [str(_fake_cli(tmp_path))])
    assert runner.run() == 0
    output = stderr.getvalue()
    assert "hint: agentcap saw no API requests." in output
    assert "OPENAI_API_KEY" in output
    assert "GEMINI_API_KEY" in output
    assert "README 'Supported'" in output
    assert "hint: supported: codex, gemini" not in output


def test_zero_request_hint_for_unknown_cli_names_supported_tools(tmp_path):
    runner, stderr = _runner(tmp_path, [sys.executable, "-c", "pass"])
    assert runner.run() == 0
    output = stderr.getvalue()
    assert "hint: agentcap saw no API requests." in output
    assert "hint: supported: claude, codex, gemini" in output
    assert "OPENAI_BASE_URL / GOOGLE_GEMINI_BASE_URL" in output
    assert "agentcap run --cap 1 -- python your_script.py" in output
    # The no-traffic hint comes first, then the supported-CLI hint.
    assert output.index("hint: agentcap saw no API requests.") < output.index(
        "hint: supported:"
    )


def test_unknown_cli_hint_is_not_printed_when_traffic_was_metered(tmp_path):
    child = [
        sys.executable,
        "-c",
        (
            "import os, httpx\n"
            "httpx.post(os.environ['OPENAI_BASE_URL'] + '/chat/completions',\n"
            "           json={'model': 'gpt-4o-mini',\n"
            "                 'messages': [{'role': 'user', 'content': 'hi'}]},\n"
            "           timeout=5)\n"
        ),
    ]
    runner, stderr = _runner(tmp_path, child, cap="1")
    assert runner.run() == 0
    output = stderr.getvalue()
    assert "hint: supported:" not in output
    assert "hint: agentcap saw no API requests." not in output


def test_first_request_refusal_hint_suggests_three_times_the_reservation(tmp_path):
    child = [
        sys.executable,
        "-c",
        (
            "import os, sys, httpx\n"
            "httpx.post(os.environ['OPENAI_BASE_URL'] + '/chat/completions',\n"
            "           json={'model': 'gpt-4o-mini',\n"
            "                 'messages': [{'role': 'user', 'content': 'hi'}],\n"
            "                 'max_completion_tokens': 100000},\n"
            "           timeout=5)\n"
            "sys.exit(0)\n"
        ),
    ]
    runner, stderr = _runner(tmp_path, child, cap="0.01", clamp_output=False)
    assert runner.run() == 0
    summary = runner.summary
    assert summary is not None
    assert summary.spent_usd == 0
    assert summary.refused_requests == 1
    reserved = runner._proxy.last_refused_reserved_usd
    assert reserved is not None and reserved > 0
    from agentcap.runner import _hint_usd, _suggested_cap

    output = stderr.getvalue()
    assert (
        f"hint: the first request alone needs about {_hint_usd(reserved)}"
        in output
    )
    assert "but the cap is $0.01." in output
    assert (
        f"Try --cap {_suggested_cap(reserved):.2f} (about 3x one request)"
        in output
    )
    assert "or lower max output." in output


def test_no_first_request_hint_when_requests_were_served(tmp_path):
    child = [
        sys.executable,
        "-c",
        (
            "import os, httpx\n"
            "httpx.post(os.environ['OPENAI_BASE_URL'] + '/chat/completions',\n"
            "           json={'model': 'gpt-4o-mini',\n"
            "                 'messages': [{'role': 'user', 'content': 'hi'}]},\n"
            "           timeout=5)\n"
        ),
    ]
    runner, stderr = _runner(tmp_path, child, cap="1")
    assert runner.run() == 0
    assert "hint: the first request alone" not in stderr.getvalue()


def test_suggested_cap_rounds_up_and_has_a_floor():
    from agentcap.runner import _hint_usd, _suggested_cap

    assert _suggested_cap(Decimal("0.001")) == Decimal("0.05")
    assert _suggested_cap(Decimal("0.0123")) == Decimal("0.05")
    assert _suggested_cap(Decimal("0.02")) == Decimal("0.06")
    assert _suggested_cap(Decimal("0.06")) == Decimal("0.18")
    assert _hint_usd(Decimal("0.01")) == "$0.01"
    assert _hint_usd(Decimal("0.0123")) == "$0.0123"
    assert _hint_usd(Decimal("0")) == "$0.00"
