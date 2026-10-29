"""Upstream overrides and the end-to-end fake-upstream workflow."""

from __future__ import annotations

import asyncio
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

import agentcap.cli as cli
from agentcap.devtools.fake_upstream import create_app as create_fake_app
from agentcap.upstreams import (
    GEMINI_UPSTREAM_ENV,
    OPENAI_UPSTREAM_ENV,
    UpstreamConfigError,
    display_url,
    resolve_upstreams,
)

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture()
def fake_runner(monkeypatch, tmp_path):
    class FakeRunner:
        instances: list["FakeRunner"] = []

        def __init__(self, argv, **kwargs) -> None:
            self.argv = list(argv)
            self.kwargs = kwargs
            FakeRunner.instances.append(self)

        def run(self) -> int:
            return 0

    FakeRunner.instances = []
    monkeypatch.setattr(cli, "Runner", FakeRunner)
    monkeypatch.setenv("AGENTCAP_LICENSE_FILE", str(tmp_path / "license.json"))
    monkeypatch.delenv(OPENAI_UPSTREAM_ENV, raising=False)
    monkeypatch.delenv(GEMINI_UPSTREAM_ENV, raising=False)
    return FakeRunner


# --------------------------------------------------------------- validation
def test_https_override_is_accepted():
    overrides = resolve_upstreams(
        {OPENAI_UPSTREAM_ENV: "https://api.example.com/v1/"}
    )
    assert len(overrides) == 1
    assert overrides[0].provider == "openai"
    assert overrides[0].url == "https://api.example.com/v1"


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:9000",
        "http://localhost:9000/v1",
        "http://[::1]:9000",
    ],
)
def test_http_loopback_is_accepted(url):
    overrides = resolve_upstreams({GEMINI_UPSTREAM_ENV: url})
    assert overrides[0].url == url


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com",
        "http://10.0.0.5:9000",
        "ftp://example.com",
        "not-a-url",
        "https://",
    ],
)
def test_unsafe_override_is_rejected(url):
    with pytest.raises(UpstreamConfigError):
        resolve_upstreams({OPENAI_UPSTREAM_ENV: url})


def test_display_url_strips_userinfo_and_query():
    shown = display_url("http://user:pass@127.0.0.1:9000/v1?key=secret#frag")
    assert shown == "http://127.0.0.1:9000/v1"
    assert "user" not in shown and "secret" not in shown


def test_cli_rejects_remote_http_override(fake_runner, monkeypatch, capsys):
    monkeypatch.setenv(OPENAI_UPSTREAM_ENV, "http://example.com")
    assert cli.main(["run", "--", "true"]) == 2
    assert not fake_runner.instances
    err = capsys.readouterr().err
    assert "may use http:// only for 127.0.0.1, localhost or ::1" in err


def test_cli_prints_yellow_notice_without_secrets(
    fake_runner, monkeypatch, capsys
):
    monkeypatch.setenv(
        GEMINI_UPSTREAM_ENV,
        "http://user:secret@127.0.0.1:9000/v1?key=topsecret",
    )
    assert cli.main(["run", "--", "true"]) == 0
    err = capsys.readouterr().err
    assert "\033[33m" in err
    assert "AGENTCAP_GEMINI_UPSTREAM override active" in err
    assert "127.0.0.1:9000/v1" in err
    assert "user:secret" not in err
    assert "topsecret" not in err
    runner = fake_runner.instances[0]
    assert "127.0.0.1:9000/v1" in runner.kwargs["gemini_upstream"]
    assert runner.kwargs["openai_upstream"] is None


# ------------------------------------------------------------------- helpers
def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_ready(port: int, process: subprocess.Popen, timeout: float = 15.0) -> None:
    url = f"http://127.0.0.1:{port}/healthz"
    deadline = time.monotonic() + timeout
    client = httpx.Client(timeout=0.5, trust_env=False)
    try:
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise AssertionError(
                    "fake upstream exited during startup: "
                    f"{process.stderr.read().decode('utf-8', 'replace')}"
                )
            try:
                if client.get(url).status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            time.sleep(0.05)
    finally:
        client.close()
    raise AssertionError("fake upstream did not become ready in time")


def _stop(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5.0)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5.0)


def _subprocess_env(**extra: str) -> dict[str, str]:
    env = dict(os.environ)
    for name in (OPENAI_UPSTREAM_ENV, GEMINI_UPSTREAM_ENV, "AGENTCAP_CAP"):
        env.pop(name, None)
    env["PYTHONPATH"] = os.pathsep.join(
        [str(ROOT / "src"), env.get("PYTHONPATH", "")]
    ).rstrip(os.pathsep)
    env.update(extra)
    return env


CHILD_SCRIPT = (
    "import os, sys, httpx\n"
    "base = os.environ['GOOGLE_GEMINI_BASE_URL']\n"
    "r = httpx.post(base + '/v1beta/models/gemini-4-argon:generateContent',\n"
    "               json={'contents': [{'parts': [{'text': 'hi'}]}]},\n"
    "               timeout=10, trust_env=False)\n"
    "print(r.headers.get('x-fake-output-limit', 'missing'))\n"
    "sys.exit(0 if r.status_code == 200 else 1)\n"
)


def test_cli_run_against_fake_upstream_clamps_output(tmp_path):
    port = _free_port()
    fake = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "agentcap.devtools.fake_upstream",
            "--port",
            str(port),
        ],
        cwd=str(ROOT),
        env=_subprocess_env(),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    try:
        _wait_ready(port, fake)
        env = _subprocess_env(
            **{
                GEMINI_UPSTREAM_ENV: f"http://127.0.0.1:{port}",
                "AGENTCAP_DB": str(tmp_path / "ledger.db"),
            }
        )
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "agentcap.cli",
                "run",
                "--cap",
                "0.05",
                "--",
                sys.executable,
                "-c",
                CHILD_SCRIPT,
            ],
            cwd=str(ROOT),
            env=env,
            capture_output=True,
            text=True,
            timeout=90,
        )
    except subprocess.TimeoutExpired as exc:
        pytest.fail(f"agentcap run timed out: {exc}")
    finally:
        _stop(fake)

    assert result.returncode == 0, result.stderr
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    assert lines, result.stdout
    clamped = int(lines[-1])
    assert 256 <= clamped < 65536, result.stdout

    spent_lines = [
        line for line in result.stderr.splitlines() if "spent:" in line
    ]
    assert spent_lines, result.stderr
    spent = float(spent_lines[0].split("spent: $", 1)[1])
    assert 0 < spent <= 0.05
    assert "override active" in result.stderr


# ------------------------------------------------------- fake upstream usage
def _fake_post(path: str, payload: dict) -> httpx.Response:
    app = create_fake_app()

    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://fake"
        ) as client:
            return await client.post(path, json=payload)

    return asyncio.run(scenario())


def test_fake_upstream_prompt_tokens_follow_text_length():
    chat = _fake_post(
        "/v1/chat/completions",
        {
            "model": "fake-model",
            "messages": [{"role": "user", "content": "x" * 400}],
        },
    )
    assert chat.status_code == 200
    assert chat.json()["usage"]["prompt_tokens"] == 100

    parts = _fake_post(
        "/v1/responses",
        {
            "model": "fake-model",
            "input": [
                {
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": "a" * 40},
                        {"type": "input_text", "text": "b" * 8},
                    ],
                }
            ],
        },
    )
    assert parts.json()["usage"]["input_tokens"] == 12


def test_fake_upstream_gemini_prompt_tokens_and_output_limit():
    response = _fake_post(
        "/v1beta/models/gemini-test:generateContent",
        {
            "contents": [{"parts": [{"text": "z" * 80}]}],
            "systemInstruction": {"parts": [{"text": "s" * 20}]},
            "tools": [
                {
                    "functionDeclarations": [
                        {"name": "f", "description": "d" * 20}
                    ]
                }
            ],
            "generationConfig": {"maxOutputTokens": 100},
        },
    )
    usage = response.json()["usageMetadata"]
    assert usage["promptTokenCount"] == 30
    assert usage["candidatesTokenCount"] == 100


def test_fake_upstream_gemini_respects_thinking_budget():
    response = _fake_post(
        "/v1beta/models/gemini-test:generateContent",
        {
            "contents": [{"parts": [{"text": "hi"}]}],
            "generationConfig": {
                "maxOutputTokens": 100,
                "thinkingConfig": {"thinkingBudget": 50},
            },
        },
    )
    usage = response.json()["usageMetadata"]
    assert usage["candidatesTokenCount"] == 100
    assert usage["thoughtsTokenCount"] == 50


def test_fake_upstream_prompt_tokens_never_below_one():
    response = _fake_post(
        "/v1/chat/completions",
        {"model": "fake-model", "messages": []},
    )
    assert response.json()["usage"]["prompt_tokens"] == 1
