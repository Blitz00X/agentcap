"""Alert fan-out for spend and safety events.

Every alert goes to the terminal (stderr, red).  Pro users can additionally
receive webhook and Telegram notifications; those are delivered from a
background thread so a slow endpoint never blocks the proxy.

Payloads are built from a strict field whitelist and pass through
:func:`redact` before leaving the process, so prompts, response text and
credentials can never be included.
"""

from __future__ import annotations

import os
import re
import sys
import threading
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Mapping, TextIO

import httpx

RED = "\x1b[31m"
YELLOW = "\x1b[33m"
RESET = "\x1b[0m"

TERMINAL_EVENTS = frozenset(
    {
        "cap_threshold",
        "cap_exceeded",
        "loop_detected",
        "usage_missing",
        "overshoot",
        "output_clamped",
        "request_too_large",
        "loop_nudge",
        "run_receipt",
        "quota_threshold",
        "quota_exceeded",
        "quota_parallel",
        "quota_receipt",
    }
)

#: Free-tier hint: shown in the terminal only, never sent anywhere.
LOCAL_ONLY_EVENTS = frozenset({"loop_nudge"})
#: The end-of-run receipt is already printed as the run summary; it only goes
#: to the Pro channels (webhook / Telegram).
REMOTE_ONLY_EVENTS = frozenset({"run_receipt", "quota_receipt"})

PRICING_URL = "https://agentcap.launchbench.studio/#pricing"

# Alerts that must never be repeated for one run (a run has one kill and one
# budget-clamp notice, no matter how many later requests are rejected).
ONCE_EVENTS = frozenset(
    {"cap_exceeded", "loop_detected", "output_clamped", "loop_nudge", "run_receipt",
     "quota_threshold", "quota_exceeded", "quota_parallel", "quota_receipt"}
)

# Only these fields may appear in an alert payload.  Anything else is dropped.
ALLOWED_FIELDS = frozenset(
    {
        "type",
        "reason",
        "scope",
        "cap_usd",
        "spent_usd",
        "needed_usd",
        "cost_usd",
        "reserved_usd",
        "run_spent_usd",
        "charged_usd",
        "model",
        "provider",
        "threshold",
        "window_s",
        "request_hash",
        "run_id",
        "allowed_output_tokens",
        "remaining_usd",
        "requests",
        "exit_code",
        "window",
        "quota_pct",
        "share_pct",
    }
)

_SECRET_PATTERNS = (
    re.compile(r"sk-[A-Za-z0-9_\-]{4,}"),
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._\-]+"),
    re.compile(r"(?i)(x-goog-api-key|api[_-]?key|authorization)\s*[:=]\s*\S+"),
)


def redact(text: str) -> str:
    """Scrub credential-shaped substrings from a string."""

    if not isinstance(text, str):
        return text
    result = text
    for pattern in _SECRET_PATTERNS:
        result = pattern.sub("[redacted]", result)
    return result


def sanitize(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Keep only whitelisted fields and redact every string value."""

    clean: dict[str, Any] = {}
    for key, value in payload.items():
        if key not in ALLOWED_FIELDS:
            continue
        if isinstance(value, str):
            clean[key] = redact(value)
        elif isinstance(value, (int, float, bool)) or value is None:
            clean[key] = value
        else:
            clean[key] = redact(str(value))
    return clean


def format_alert(event: Mapping[str, Any]) -> str:
    """One-line, human-readable terminal message for an event."""

    kind = str(event.get("type", "event"))
    run = event.get("run_id")
    prefix = "agentcap" + (f" [{redact(str(run))}]" if run else "")

    if kind == "cap_threshold":
        threshold = event.get("threshold", "?")
        return (
            f"{prefix}: {threshold}% of the ${event.get('cap_usd', '?')} run cap "
            f"used (${event.get('spent_usd', '?')})."
        )
    if kind == "cap_exceeded":
        return (
            f"{prefix}: RUN CAP HIT (scope={event.get('scope', 'run')}, "
            f"cap=${event.get('cap_usd', '?')}, spent=${event.get('spent_usd', '?')}). "
            "Stopping the agent."
        )
    if kind == "loop_detected":
        return (
            f"{prefix}: runaway loop detected ("
            f"{event.get('detail') or 'identical request repeated'}). Stopping the agent."
        )
    if kind == "loop_nudge":
        spent = event.get("spent_usd")
        try:
            amount = f"${float(spent):.2f}"
        except (TypeError, ValueError):
            amount = "$?"
        return (
            f"{prefix}: this looks like a runaway loop ("
            f"{event.get('detail') or 'identical request repeated'}). Pro's loop breaker would have "
            f"stopped this at {amount}. {PRICING_URL}"
        )
    if kind == "run_receipt":
        spent = event.get("spent_usd")
        try:
            amount = f"${float(spent):.4f}"
        except (TypeError, ValueError):
            amount = "$?"
        cap = event.get("cap_usd")
        return (
            f"{prefix}: run finished ({event.get('reason', '?')}, exit "
            f"{event.get('exit_code', '?')}): spent {amount}"
            + (f" of ${cap} cap" if cap not in (None, "None") else "")
            + f", {event.get('requests', '?')} requests."
        )
    if kind.startswith("quota_"):
        label = "weekly" if event.get("window") == "week" else "5-hour"
        share = event.get("share_pct", "?")
        quota = event.get("quota_pct", "?")
        if kind == "quota_threshold":
            return f"{prefix}: job used {share}% of the {label} subscription quota (80% of its {quota}% limit)."
        if kind == "quota_exceeded":
            return f"{prefix}: QUOTA LIMIT HIT: job used {share}% of the {label} quota (limit {quota}%). Stopping the agent."
        if kind == "quota_parallel":
            return f"{prefix}: another session is using the same subscription; its usage counts against this job."
        return (f"{prefix}: job finished ({event.get('reason', '?')}, exit {event.get('exit_code', '?')}): "
                f"used {share}% of the {label} quota (limit {quota}%).")
    if kind == "usage_missing":
        return (
            f"{prefix}: upstream returned no usage; charged the full "
            f"reservation (${event.get('charged_usd', '?')}) to stay conservative."
        )
    if kind == "overshoot":
        return (
            f"{prefix}: a request settled above its reservation "
            f"(${event.get('cost_usd', '?')} > ${event.get('reserved_usd', '?')}); "
            "the cap is still enforced on the next request."
        )
    if kind == "output_clamped":
        return (
            f"{prefix}: budget low, limiting output to "
            f"{event.get('allowed_output_tokens', '?')} tokens per request"
        )
    if kind == "request_too_large":
        return (
            f"{prefix}: a request needs ${event.get('needed_usd', '?')} but only "
            f"${event.get('remaining_usd', event.get('cap_usd', '?'))} of budget "
            "remains; not sent. Raise the cap or enable output clamping."
        )
    return f"{prefix}: {kind}"


class AlertDispatcher:
    """Terminal + optional webhook/Telegram alert fan-out.

    ``transport`` is an ``httpx`` transport, injected in tests so no real
    network call is ever made.
    """

    def __init__(
        self,
        *,
        features: Iterable[str] | None = None,
        webhook_url: str | None = None,
        telegram_token: str | None = None,
        telegram_chat_id: str | None = None,
        transport: httpx.BaseTransport | None = None,
        stderr: TextIO | None = None,
        clock: Callable[[], datetime] | None = None,
        background: bool = True,
        timeout_s: float = 5.0,
    ) -> None:
        self.features = frozenset(features or ())
        self.webhook_url = webhook_url or None
        self.telegram_token = telegram_token or None
        self.telegram_chat_id = telegram_chat_id or None
        self.transport = transport
        self.stderr = stderr if stderr is not None else sys.stderr
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.background = background
        self.timeout_s = float(timeout_s)
        self._threads: list[threading.Thread] = []
        self._lock = threading.Lock()
        self._seen_once: set[tuple[str, object]] = set()

    # --------------------------------------------------------------- sending
    def __call__(self, event: Mapping[str, Any]) -> None:
        self.emit(event)

    def emit(self, event: Mapping[str, Any]) -> None:
        if not isinstance(event, Mapping):
            return
        kind = str(event.get("type", ""))
        if kind not in TERMINAL_EVENTS:
            # Per-request accounting events stay local; only alerts fan out.
            return
        if kind in ONCE_EVENTS:
            key = (kind, event.get("run_id"))
            with self._lock:
                if key in self._seen_once:
                    return
                self._seen_once.add(key)
        message = format_alert(event)
        color = (
            YELLOW
            if kind in {"output_clamped", "request_too_large", "loop_nudge", "quota_threshold", "quota_parallel"}
            else RED
        )
        if kind not in REMOTE_ONLY_EVENTS:
            self._terminal(message, color=color)
        if kind in LOCAL_ONLY_EVENTS:
            return

        clean = sanitize(event)
        clean.setdefault("run_id", None)
        payload = {
            "source": "agentcap",
            "sent_at": self.clock().isoformat(),
            "event": clean,
            "message": redact(message),
        }
        if self.webhook_url and "webhook_alerts" in self.features:
            self._deliver(self._post_webhook, payload)
        if (
            self.telegram_token
            and self.telegram_chat_id
            and "telegram_alerts" in self.features
        ):
            self._deliver(self._post_telegram, payload)

    def _terminal(self, message: str, *, color: str = RED) -> None:
        try:
            print(f"{color}{redact(message)}{RESET}", file=self.stderr, flush=True)
        except Exception:
            pass

    def _deliver(self, sender: Callable[[dict[str, Any]], None], payload: dict[str, Any]) -> None:
        if not self.background:
            sender(payload)
            return
        thread = threading.Thread(target=sender, args=(payload,), daemon=True)
        with self._lock:
            self._threads.append(thread)
        thread.start()

    def wait(self, timeout: float | None = 5.0) -> None:
        """Join outstanding delivery threads (used by tests and shutdown)."""

        with self._lock:
            threads = list(self._threads)
            self._threads.clear()
        for thread in threads:
            thread.join(timeout)

    def _post_webhook(self, payload: dict[str, Any]) -> None:
        try:
            with httpx.Client(
                transport=self.transport, timeout=self.timeout_s, trust_env=False
            ) as client:
                response = client.post(self.webhook_url, json=payload)
                response.raise_for_status()
        except Exception as exc:
            # Never echo the URL or exception text: it may contain a token.
            self._warn("webhook", exc)

    def _post_telegram(self, payload: dict[str, Any]) -> None:
        try:
            text = payload.get("message") or "agentcap alert"
            url = f"https://api.telegram.org/bot{self.telegram_token}/sendMessage"
            with httpx.Client(
                transport=self.transport, timeout=self.timeout_s, trust_env=False
            ) as client:
                response = client.post(
                    url,
                    json={"chat_id": self.telegram_chat_id, "text": text},
                )
                response.raise_for_status()
        except Exception as exc:
            self._warn("telegram", exc)

    def _warn(self, channel: str, exc: BaseException) -> None:
        try:
            print(
                f"agentcap: alert delivery failed ({channel}: "
                f"{type(exc).__name__}); continuing.",
                file=self.stderr,
                flush=True,
            )
        except Exception:
            pass


def dispatcher_from_env(
    *,
    features: Iterable[str] | None = None,
    transport: httpx.BaseTransport | None = None,
    stderr: TextIO | None = None,
    background: bool = True,
) -> AlertDispatcher:
    """Build a dispatcher from ``AGENTCAP_WEBHOOK_URL`` / Telegram env vars."""

    return AlertDispatcher(
        features=features,
        webhook_url=os.environ.get("AGENTCAP_WEBHOOK_URL"),
        telegram_token=os.environ.get("AGENTCAP_TELEGRAM_BOT_TOKEN"),
        telegram_chat_id=os.environ.get("AGENTCAP_TELEGRAM_CHAT_ID"),
        transport=transport,
        stderr=stderr,
        background=background,
    )


__all__ = [
    "AlertDispatcher",
    "ALLOWED_FIELDS",
    "TERMINAL_EVENTS",
    "dispatcher_from_env",
    "format_alert",
    "redact",
    "sanitize",
]
