"""Shared helpers for the ops tracking tests.

Nothing here talks to a model or to the internet: endpoints are local
``127.0.0.1`` servers (or closed ports), git repositories live in ``tmp_path``
with an empty git config, and HOME points at a throwaway directory.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = str(ROOT / "src")
PY = sys.executable

requires_git = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")


@pytest.fixture
def ops_env(tmp_path, monkeypatch):
    """Isolated HOME/git config; ``python -m agentcap.ops`` importable in children."""

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    # Never let git walk out of the test's temp area into a real repository.
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path.parent))
    for name in list(os.environ):
        if name.startswith("AGENTCAP_OPS_"):
            monkeypatch.delenv(name)
    existing = [part for part in os.environ.get("PYTHONPATH", "").split(os.pathsep) if part]
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join([SRC, *existing]))
    return home


def git(cwd: Path | str, *args: str) -> str:
    """Run git with a fixed identity and no signing; fail loudly."""

    result = subprocess.run(
        [
            "git", "-C", str(cwd),
            "-c", "user.name=ops-test", "-c", "user.email=ops-test@example.invalid",
            "-c", "commit.gpgsign=false", "-c", "init.defaultBranch=main",
            *args,
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if result.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {result.stderr}")
    return result.stdout.strip()


def make_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    git(path, "init", "-q")
    return path.resolve()


def closed_port() -> int:
    """A local port with nothing listening (connection refused)."""

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def write_config(directory: Path, data: dict[str, Any], mode: int = 0o600) -> Path:
    path = directory / "endpoint.json"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(data, handle)
    os.chmod(path, mode)
    return path


class FakeClock:
    """Injectable epoch clock so backoff can be tested without sleeping."""

    def __init__(self, start: float = 1_800_000_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class Endpoint:
    """Local HTTP endpoint that records every POST; statuses are scripted."""

    def __init__(self, statuses: list[int] | None = None) -> None:
        self.requests: list[dict[str, Any]] = []
        self.statuses = list(statuses or [])
        self._lock = threading.Lock()
        endpoint = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802 - http.server API
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length)
                with endpoint._lock:
                    endpoint.requests.append(
                        {
                            "path": self.path,
                            "headers": {k.lower(): v for k, v in self.headers.items()},
                            "body": body,
                        }
                    )
                    status = endpoint.statuses.pop(0) if endpoint.statuses else 200
                self.send_response(status)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *args: Any) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}/hook"

    def __enter__(self) -> "Endpoint":
        self.thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=10)
