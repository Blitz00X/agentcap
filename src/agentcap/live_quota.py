"""Ask the agent's own official client for the account's current quota.

AgentCap never reads a login token or talks to a provider itself.  It runs the
vendor's own program and reads what that program prints:

* Codex:  ``codex app-server`` (stdio JSON-RPC, the protocol the IDE extension
  uses) answers ``account/rateLimits/read`` with the 5 h and weekly %.
* Claude: ``claude -p /usage`` prints the same numbers as the in-app /usage page.

Neither call runs a model, so neither spends quota (checked live 12 Nov 2026).
Both are best effort: any failure returns ``None`` and callers fall back to the
local readings (rollout files / statusline).  ``AGENTCAP_LIVE_QUOTA=0`` turns
them off (the test suite does this so it never starts a real agent).
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import re
import selectors
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Callable, Mapping

from .quota import CODEX_MINUTES, Reading, _num

LIVE_ENV = "AGENTCAP_LIVE_QUOTA"
CODEX_TIMEOUT_S = 10.0
CLAUDE_TIMEOUT_S = 30.0
_MINUTES_WINDOW = {v: k for k, v in CODEX_MINUTES.items()}

# Environment markers of the job / the calling Claude session; the probe is a
# plain read and must not look like a guarded agent to the vendor CLI.
_DROP_ENV = ("AGENTCAP_QUOTA_JOB", "AGENTCAP_GUARDED", "CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT")


def live_enabled(env: Mapping[str, str] | None = None) -> bool:
    value = (os.environ if env is None else env).get(LIVE_ENV, "1").strip().lower()
    return value not in ("0", "off", "no", "false")


def _probe_env() -> dict[str, str]:
    env = dict(os.environ)
    for key in _DROP_ENV:
        env.pop(key, None)
    return env


# ------------------------------------------------------------------- codex
def parse_codex_rate_limits(result: Any) -> dict[str, Reading]:
    """``account/rateLimits/read`` result -> readings keyed by window."""

    out: dict[str, Reading] = {}
    limits = result.get("rateLimits") if isinstance(result, Mapping) else None
    if not isinstance(limits, Mapping):
        return out
    for key in ("primary", "secondary"):
        block = limits.get(key)
        if not isinstance(block, Mapping):
            continue
        used = _num(block.get("usedPercent"))
        minutes = _num(block.get("windowDurationMins"))
        window = _MINUTES_WINDOW.get(int(minutes)) if minutes is not None else None
        if used is None or window is None:
            continue
        out[window] = Reading(used, _num(block.get("resetsAt")))
    return out


def codex_live(*, binary: str | None = None, timeout: float = CODEX_TIMEOUT_S) -> dict[str, Reading] | None:
    exe = binary or shutil.which("codex")
    if not exe:
        return None
    try:
        proc = subprocess.Popen(
            [exe, "app-server"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, env=_probe_env(), start_new_session=True,
        )
    except OSError:
        return None
    try:
        assert proc.stdin is not None and proc.stdout is not None
        messages = [
            {"id": 1, "method": "initialize",
             "params": {"clientInfo": {"name": "agentcap", "version": "0.4"}}},
            {"method": "initialized"},
            {"id": 2, "method": "account/rateLimits/read", "params": None},
        ]
        proc.stdin.write(("".join(json.dumps(m) + "\n" for m in messages)).encode())
        proc.stdin.flush()
        deadline = time.monotonic() + timeout
        sel = selectors.DefaultSelector()
        sel.register(proc.stdout, selectors.EVENT_READ)
        buf = b""
        while time.monotonic() < deadline:
            if not sel.select(max(0.0, deadline - time.monotonic())):
                break
            chunk = os.read(proc.stdout.fileno(), 65536)
            if not chunk:
                break
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                if isinstance(msg, Mapping) and msg.get("id") == 2:
                    if "error" in msg:
                        return None
                    return parse_codex_rate_limits(msg.get("result")) or None
        return None
    except (OSError, ValueError):
        return None
    finally:
        _close(proc)


def _close(proc: subprocess.Popen) -> None:
    try:
        if proc.stdin:
            proc.stdin.close()
    except OSError:
        pass
    try:
        proc.terminate()
        proc.wait(timeout=3)
    except (OSError, subprocess.TimeoutExpired):
        try:
            proc.kill()
            proc.wait(timeout=3)
        except (OSError, subprocess.TimeoutExpired):
            pass


# ------------------------------------------------------------------ claude
_USAGE_LINE = re.compile(
    r"^\s*Current (session|week \(all models\))\s*:\s*(\d+(?:\.\d+)?)\s*% used"
    r"(?:\s*\S\s*resets\s+(.+?))?\s*$", re.IGNORECASE)
_RESET = re.compile(
    r"^(?:(?P<mon>[A-Za-z]{3})[a-z]*\.?\s+(?P<day>\d{1,2}),?\s+)?"
    r"(?:at\s+)?(?P<h>\d{1,2})(?::(?P<m>\d{2}))?\s*(?P<ap>am|pm)?"
    r"(?:\s*\((?P<tz>[^)]+)\))?\s*$", re.IGNORECASE)
_MONTHS = {m: i for i, m in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1)}


def _zone(name: str | None) -> _dt.tzinfo | None:
    if not name:
        return None
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(name.strip())
    except Exception:
        return None


def parse_reset_text(text: str, now: float) -> float | None:
    """'Nov 12, 5:29pm (Europe/Istanbul)' or '5:29pm (UTC)' -> unix seconds.

    The year is not printed; the candidate closest to *now* that is not more
    than a day in the past wins (a reset is always ahead of us)."""

    m = _RESET.match(text.strip())
    if not m:
        return None
    hour, minute = int(m["h"]), int(m["m"] or 0)
    ap = (m["ap"] or "").lower()
    if ap == "pm" and hour != 12:
        hour += 12
    elif ap == "am" and hour == 12:
        hour = 0
    if hour > 23 or minute > 59:
        return None
    tz = _zone(m["tz"])
    here = _dt.datetime.fromtimestamp(now, tz) if tz else _dt.datetime.fromtimestamp(now).astimezone()
    tz = here.tzinfo
    candidates: list[_dt.datetime] = []
    if m["mon"]:
        month = _MONTHS.get(m["mon"][:3].lower())
        if not month:
            return None
        for year in (here.year - 1, here.year, here.year + 1):
            try:
                candidates.append(_dt.datetime(year, month, int(m["day"]), hour, minute, tzinfo=tz))
            except ValueError:
                continue
    else:
        base = here.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if base.timestamp() <= now:  # a time without a date is the next one
            base += _dt.timedelta(days=1)
        return base.timestamp()
    future = [c.timestamp() for c in candidates if c.timestamp() > now - 86400]
    if not future:
        return None
    return min(future, key=lambda ts: abs(ts - now))


def parse_claude_usage(text: str, now: float) -> dict[str, Reading]:
    out: dict[str, Reading] = {}
    for line in text.splitlines():
        m = _USAGE_LINE.match(line)
        if not m:
            continue
        window = "5h" if m[1].lower() == "session" else "week"
        resets = parse_reset_text(m[3], now) if m[3] else None
        out[window] = Reading(float(m[2]), resets)
    return out


def claude_live(*, binary: str | None = None, timeout: float = CLAUDE_TIMEOUT_S,
                cwd: Path | None = None, now: float | None = None) -> dict[str, Reading] | None:
    exe = binary or shutil.which("claude")
    if not exe:
        return None
    try:
        result = subprocess.run(
            [exe, "-p", "/usage"], stdin=subprocess.DEVNULL, capture_output=True, text=True,
            timeout=timeout, env=_probe_env(), cwd=str(cwd) if cwd else None,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return parse_claude_usage(result.stdout, time.time() if now is None else now) or None


# ------------------------------------------------------------------ either
LiveReader = Callable[[str], "dict[str, Reading] | None"]


def live_readings(agent: str) -> dict[str, Reading] | None:
    """Current account readings for *agent* ('claude' / 'codex'), or None."""

    if not live_enabled():
        return None
    if agent == "codex":
        return codex_live()
    if agent == "claude":
        from .quota import state_root
        probe = state_root() / "probe"
        try:
            probe.mkdir(parents=True, exist_ok=True, mode=0o700)
        except OSError:
            probe = None  # type: ignore[assignment]
        return claude_live(cwd=probe)
    return None
