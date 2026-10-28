"""Run a child command behind the local spend-cap proxy.

The proxy listens on ``127.0.0.1`` only, on a random free port (or ``--port``).
The child receives base-URL environment variables pointing at the proxy, so any
tool that honours them is metered without code changes.

Killing is centralised here so it can be tested: :class:`Runner` accepts an
injectable upstream transport, ``Popen`` factory, clock and alert dispatcher.
"""

from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, Callable, Iterable, Mapping, Sequence, TextIO

import httpx
import uvicorn

from .alerts import AlertDispatcher, dispatcher_from_env
from .ledger import Caps, Ledger
from .license import features as license_features
from .license import status_from_environment
from .pricing import PricingStore
from .proxy import (
    DEFAULT_GEMINI_UPSTREAM,
    DEFAULT_OPENAI_UPSTREAM,
    ProxyConfig,
    create_app,
)

EXIT_CAP_EXCEEDED = 3
EXIT_LOOP_DETECTED = 4

EXIT_REASONS = {
    "cap_exceeded": EXIT_CAP_EXCEEDED,
    "loop_detected": EXIT_LOOP_DETECTED,
}

CAP_ENV = "AGENTCAP_CAP"
DEFAULT_CAP = Decimal("5")


@dataclass
class RunSummary:
    """Numbers printed when a run ends."""

    run_id: str
    requests: int
    spent_usd: Decimal
    cap_usd: Decimal | None
    reserved_usd: Decimal
    reason: str
    exit_code: int
    clamped_requests: int = 0

    def lines(self) -> list[str]:
        cap = "none" if self.cap_usd is None else f"${self.cap_usd:.2f}"
        return [
            "agentcap run summary",
            f"  run id: {self.run_id}",
            f"  requests: {self.requests}",
            f"  spent: ${self.spent_usd:.4f}",
            f"  cap: {cap}",
            f"  reserved-but-unsettled: ${self.reserved_usd:.4f}",
            f"  clamped requests: {self.clamped_requests}",
            f"  reason: {self.reason}",
            f"  exit code: {self.exit_code}",
        ]


def cap_from_env(default: Decimal | str | int = DEFAULT_CAP) -> Decimal:
    raw = os.environ.get(CAP_ENV)
    if not raw:
        return Decimal(str(default))
    try:
        value = Decimal(raw.strip())
    except Exception:  # noqa: BLE001 - fall back to the documented default
        return Decimal(str(default))
    return value if value >= 0 else Decimal(str(default))


def _default_popen(argv: Sequence[str], env: Mapping[str, str]) -> subprocess.Popen:
    return subprocess.Popen(
        list(argv),
        env=dict(env),
        start_new_session=True,
    )


class Runner:
    """Start the proxy, spawn the child, enforce kills, print a summary."""

    def __init__(
        self,
        argv: Sequence[str],
        *,
        cap: Decimal | str | int | None = None,
        hour_cap: Decimal | str | int | None = None,
        day_cap: Decimal | str | int | None = None,
        allow_unmetered: bool = False,
        clamp_output: bool = True,
        min_clamp_output: int = 256,
        host: str = "127.0.0.1",
        port: int = 0,
        run_id: str | None = None,
        ledger: Ledger | None = None,
        features: Iterable[str] | None = None,
        pricing: PricingStore | None = None,
        config: ProxyConfig | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        popen_factory: Callable[[Sequence[str], Mapping[str, str]], subprocess.Popen]
        | None = None,
        clock: Callable[[], datetime] | None = None,
        stderr: TextIO | None = None,
        alerts: AlertDispatcher | None = None,
        alert_transport: httpx.BaseTransport | None = None,
        env: Mapping[str, str] | None = None,
        ready_timeout: float = 10.0,
        poll_interval: float = 0.05,
        term_timeout: float = 5.0,
        quiet: bool = False,
        openai_upstream: str | None = None,
        gemini_upstream: str | None = None,
    ) -> None:
        if not argv:
            raise ValueError("argv must contain the command to run")
        if host != "127.0.0.1":
            raise ValueError("the proxy must bind to 127.0.0.1")
        self.argv = list(argv)
        self.cap = Decimal(str(cap)) if cap is not None else cap_from_env()
        self.hour_cap = Decimal(str(hour_cap)) if hour_cap is not None else None
        self.day_cap = Decimal(str(day_cap)) if day_cap is not None else None
        self.allow_unmetered = bool(allow_unmetered)
        self.clamp_output = bool(clamp_output)
        self.min_clamp_output = int(min_clamp_output)
        self.host = host
        self.requested_port = int(port)
        self.run_id = run_id or f"run-{uuid.uuid4().hex[:12]}"
        self.ledger = ledger or Ledger()
        if features is None:
            if config is not None:
                self.features = frozenset(config.features or ())
            else:
                self.features = frozenset(
                    license_features(status_from_environment().tier)
                )
        else:
            self.features = frozenset(features)
        self.pricing = pricing
        self.transport = transport
        self.popen_factory = popen_factory or _default_popen
        self.clock = clock
        self.stderr = stderr if stderr is not None else sys.stderr
        self.env = dict(env) if env is not None else None
        self.ready_timeout = float(ready_timeout)
        self.poll_interval = float(poll_interval)
        self.term_timeout = float(term_timeout)
        self.quiet = quiet
        self.openai_upstream = openai_upstream
        self.gemini_upstream = gemini_upstream

        self.port: int | None = None
        self.server: uvicorn.Server | None = None
        self._server_thread: threading.Thread | None = None
        self._socket: socket.socket | None = None
        self.kill_event = threading.Event()
        self.kill_reason: str | None = None
        self._kill_lock = threading.Lock()
        self.child: subprocess.Popen | None = None
        self.summary: RunSummary | None = None

        self.alerts = alerts or dispatcher_from_env(
            features=self.features,
            transport=alert_transport,
            stderr=self.stderr,
        )

        if config is not None:
            if not isinstance(config, ProxyConfig):
                raise TypeError("config must be a ProxyConfig")
            config.on_cap_exceeded = self._on_cap_exceeded
            config.on_loop_detected = self._on_loop_detected
            if openai_upstream:
                config.openai_upstream = openai_upstream.rstrip("/")
            if gemini_upstream:
                config.gemini_upstream = gemini_upstream.rstrip("/")
            self.config = config
            self.run_id = config.run_id
            if config.caps.run_usd is not None:
                self.cap = config.caps.run_usd
            self.hour_cap = config.caps.hour_usd
            self.day_cap = config.caps.day_usd
            if config.pricing is not None:
                self.pricing = config.pricing
            if config.clock is not None and clock is None:
                self.clock = config.clock
        else:
            caps = Caps(
                run_usd=self.cap, hour_usd=self.hour_cap, day_usd=self.day_cap
            )
            self.config = ProxyConfig(
                run_id=self.run_id,
                caps=caps,
                features=self.features,
                allow_unmetered=self.allow_unmetered,
                clamp_output=self.clamp_output,
                min_clamp_output=self.min_clamp_output,
                pricing=self.pricing,
                clock=self.clock,
                on_cap_exceeded=self._on_cap_exceeded,
                on_loop_detected=self._on_loop_detected,
                openai_upstream=openai_upstream or DEFAULT_OPENAI_UPSTREAM,
                gemini_upstream=gemini_upstream or DEFAULT_GEMINI_UPSTREAM,
            )
        self.clamped_requests = 0

    # --------------------------------------------------------------- callbacks
    def _trip(self, reason: str, payload: Mapping[str, Any]) -> None:
        with self._kill_lock:
            if self.kill_reason is None:
                self.kill_reason = reason
        self.kill_event.set()

    def _on_cap_exceeded(self, payload: Mapping[str, Any]) -> None:
        self._trip("cap_exceeded", payload)

    def _on_loop_detected(self, payload: Mapping[str, Any]) -> None:
        self._trip("loop_detected", payload)

    def _emit_event(self, event: Mapping[str, Any]) -> None:
        """Attach the run id before handing an accounting event to alerts."""

        if event.get("type") == "output_clamped":
            self.clamped_requests += 1
        try:
            self.alerts.emit({**event, "run_id": self.run_id})
        except Exception:
            pass

    # ----------------------------------------------------------------- proxy
    @property
    def status_url(self) -> str:
        if self.port is None:
            raise RuntimeError("proxy is not started")
        return f"http://127.0.0.1:{self.port}/_agentcap/status"

    def _start_proxy(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((self.host, self.requested_port))
        sock.listen(128)
        sock.set_inheritable(True)
        self._socket = sock
        self.port = int(sock.getsockname()[1])

        app = create_app(
            self.config,
            self.ledger,
            upstream_transport=self.transport,
            events=self._emit_event,
        )
        server = uvicorn.Server(
            uvicorn.Config(
                app,
                host=self.host,
                port=self.port,
                log_level="error",
                access_log=False,
                lifespan="on",
            )
        )
        self.server = server
        self._server_thread = threading.Thread(
            target=server.run,
            kwargs={"sockets": [sock]},
            name="agentcap-proxy",
            daemon=True,
        )
        self._server_thread.start()
        self._wait_ready()

    def _wait_ready(self) -> None:
        deadline = time.monotonic() + self.ready_timeout
        client = httpx.Client(timeout=0.5, trust_env=False)
        try:
            while time.monotonic() < deadline:
                if self.server is not None and self.server.started:
                    try:
                        response = client.get(self.status_url)
                        if response.status_code == 200:
                            return
                    except httpx.HTTPError:
                        pass
                if self._server_thread is not None and not self._server_thread.is_alive():
                    raise RuntimeError("proxy server thread exited during startup")
                time.sleep(self.poll_interval)
        finally:
            client.close()
        raise RuntimeError(
            f"proxy did not become ready within {self.ready_timeout:g}s"
        )

    def _stop_proxy(self) -> None:
        if self.server is not None:
            self.server.should_exit = True
        if self._server_thread is not None:
            self._server_thread.join(timeout=5.0)
        if self._socket is not None:
            try:
                self._socket.close()
            except OSError:
                pass
        try:
            self.alerts.wait(timeout=5.0)
        except Exception:
            pass

    # ------------------------------------------------------------------ child
    def child_env(self) -> dict[str, str]:
        if self.port is None:
            raise RuntimeError("proxy is not started")
        env = dict(self.env) if self.env is not None else dict(os.environ)
        base = f"http://127.0.0.1:{self.port}"
        env.update(
            {
                "OPENAI_BASE_URL": f"{base}/openai/v1",
                "GOOGLE_GEMINI_BASE_URL": f"{base}/gemini",
                "GEMINI_API_BASE_URL": f"{base}/gemini",
                "AGENTCAP_RUN_ID": self.run_id,
                "AGENTCAP_PROXY": base,
            }
        )
        return env

    def _spawn(self) -> subprocess.Popen:
        return self.popen_factory(self.argv, self.child_env())

    def _signal_group(self, sig: int) -> None:
        child = self.child
        if child is None or child.poll() is not None:
            return
        try:
            pgid = os.getpgid(child.pid)
        except (ProcessLookupError, OSError):
            pgid = None
        try:
            if pgid is not None and pgid != os.getpgrp():
                os.killpg(pgid, sig)
            else:
                # A custom popen factory may not have created a new session;
                # never signal our own process group.
                child.send_signal(sig)
        except (ProcessLookupError, PermissionError, OSError):
            pass

    def _terminate(self) -> None:
        self._signal_group(signal.SIGTERM)
        child = self.child
        if child is None:
            return
        try:
            child.wait(timeout=self.term_timeout)
            return
        except subprocess.TimeoutExpired:
            pass
        self._signal_group(signal.SIGKILL)
        try:
            child.wait(timeout=5.0)
        except subprocess.TimeoutExpired:
            pass

    def _monitor(self) -> int:
        child = self.child
        assert child is not None
        while True:
            if self.kill_event.is_set():
                self._terminate()
                return EXIT_REASONS.get(self.kill_reason or "", 1)
            code = child.poll()
            if code is not None:
                if self.kill_event.is_set():
                    return EXIT_REASONS.get(self.kill_reason or "", 1)
                return int(code)
            time.sleep(self.poll_interval)

    # -------------------------------------------------------------------- run
    def run(self) -> int:
        self._start_proxy()
        reason = "child_exit"
        exit_code = 1
        try:
            self.child = self._spawn()
            try:
                exit_code = self._monitor()
            except KeyboardInterrupt:
                reason = "interrupted"
                self._signal_group(signal.SIGINT)
                try:
                    code = self.child.wait(timeout=self.term_timeout)
                    exit_code = 130 if code is None or code < 0 else int(code)
                except subprocess.TimeoutExpired:
                    self._terminate()
                    code = self.child.returncode
                    exit_code = 130 if code is None or code < 0 else int(code)
            if self.kill_event.is_set():
                reason = self.kill_reason or reason
                exit_code = EXIT_REASONS.get(reason, exit_code)
            else:
                reason = "child_exit"
        finally:
            self._stop_proxy()
            self.summary = self._build_summary(reason, exit_code)
            self._print_summary(self.summary)
        return exit_code

    def _build_summary(self, reason: str, exit_code: int) -> RunSummary:
        try:
            stats = self.ledger.summary(self.run_id)
        except Exception:
            stats = {}
        return RunSummary(
            run_id=self.run_id,
            requests=int(stats.get("reservations", 0) or 0),
            spent_usd=Decimal(str(stats.get("actual_usd", "0"))),
            cap_usd=self.cap,
            reserved_usd=Decimal(str(stats.get("reserved_usd", "0"))),
            reason=reason,
            exit_code=exit_code,
            clamped_requests=self.clamped_requests,
        )

    def _print_summary(self, summary: RunSummary) -> None:
        try:
            for line in summary.lines():
                print(line, file=self.stderr, flush=True)
        except Exception:
            pass


__all__ = [
    "Runner",
    "RunSummary",
    "EXIT_CAP_EXCEEDED",
    "EXIT_LOOP_DETECTED",
    "cap_from_env",
]
