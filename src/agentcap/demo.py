"""``agentcap demo``: a fully local, simulated spend-cap run.

The demo needs no API key and no provider account.  It starts the bundled fake
upstream (``agentcap.devtools.fake_upstream``) on a random loopback port, then
runs a tiny Python child through :class:`~agentcap.runner.Runner` with a five
cent cap.  The child keeps calling ``/v1/chat/completions`` until the cap kills
it, which normally ends the run with exit code 3.

Only package dependencies are used (starlette, uvicorn, httpx), so the command
works from a plain ``pipx install agentcap-cli``.  All traffic stays on
``127.0.0.1``.
"""

from __future__ import annotations

import os
import socket
import sys
import threading
import time
from decimal import Decimal
from typing import TextIO

import uvicorn

from .devtools.fake_upstream import create_app as create_fake_upstream
from .license import FREE_FEATURES
from .runner import Runner

DEMO_CAP = Decimal("0.05")

BANNER = (
    "SIMULATED DEMO — fake local API, no real provider, no real money. "
    "Dollar amounts are simulated."
)
FOOTER = "(simulated)"

# Runs until the proxy kills the process group at the cap.  The iteration bound
# is only a safety net if a future bug stops enforcing the cap.
CHILD_SCRIPT = """\
import os, time, httpx

base = os.environ["OPENAI_BASE_URL"]
url = base + "/chat/completions"
body = {"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "demo"}]}
for _ in range(5000):
    try:
        httpx.post(url, json=body, timeout=5, trust_env=False)
    except Exception:
        pass
    time.sleep(0.01)
"""


class FakeUpstream:
    """An in-process fake upstream bound to a random loopback port."""

    def __init__(self) -> None:
        self.url = ""
        self._socket: socket.socket | None = None
        self._server: uvicorn.Server | None = None
        self._thread: threading.Thread | None = None

    def start(self, timeout: float = 10.0) -> str:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        # 127.0.0.1 only: never expose the fake upstream to the network.
        sock.bind(("127.0.0.1", 0))
        sock.listen(128)
        port = int(sock.getsockname()[1])
        server = uvicorn.Server(
            uvicorn.Config(
                create_fake_upstream(),
                host="127.0.0.1",
                port=port,
                log_level="error",
                access_log=False,
                lifespan="on",
            )
        )
        thread = threading.Thread(
            target=server.run,
            kwargs={"sockets": [sock]},
            name="agentcap-demo-upstream",
            daemon=True,
        )
        thread.start()

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if server.started:
                break
            if not thread.is_alive():
                sock.close()
                raise RuntimeError("demo upstream exited during startup")
            time.sleep(0.02)
        else:
            server.should_exit = True
            thread.join(timeout=2.0)
            sock.close()
            raise RuntimeError("demo upstream did not become ready")

        self._socket = sock
        self._server = server
        self._thread = thread
        self.url = f"http://127.0.0.1:{port}"
        return self.url

    def stop(self) -> None:
        if self._server is not None:
            self._server.should_exit = True
        if self._thread is not None:
            self._thread.join(timeout=5.0)
        if self._socket is not None:
            try:
                self._socket.close()
            except OSError:
                pass


def run_demo(
    *,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> int:
    """Run the simulated demo and return the child run's exit code."""

    out = stdout if stdout is not None else sys.stdout
    err = stderr if stderr is not None else sys.stderr

    print(BANNER, file=out, flush=True)

    upstream = FakeUpstream()
    code = 1
    try:
        url = upstream.start()
        env = dict(os.environ)
        env["AGENTCAP_OPENAI_UPSTREAM"] = url
        runner = Runner(
            [sys.executable, "-c", CHILD_SCRIPT],
            cap=DEMO_CAP,
            features=set(FREE_FEATURES),
            env=env,
            openai_upstream=url,
            stderr=err,
        )
        code = runner.run()
    finally:
        upstream.stop()

    print(FOOTER, file=out, flush=True)
    return code


__all__ = ["BANNER", "CHILD_SCRIPT", "DEMO_CAP", "FOOTER", "FakeUpstream", "run_demo"]
