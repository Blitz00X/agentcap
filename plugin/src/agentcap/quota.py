"""Quota mode: cap one job's share of a *subscription* usage window.

ToS-safe by construction.  AgentCap never proxies, reads, copies or reuses
subscription OAuth credentials and never touches subscription traffic.  It
only *reads* official local signals and stops the local agent:

* Claude Code (Pro/Max login): a statusline command (Claude Code's official
  ``statusLine`` setting) receives ``rate_limits.five_hour/seven_day`` on
  stdin; our wrapper records them in a job state file and then runs the
  user's own statusline command unchanged.  A ``PreToolUse`` hook (official
  hooks API) denies tool calls once the job's share reaches the quota.  Both
  are injected for this run only via ``claude --settings <file>`` (an extra
  settings layer); the user's settings files are never modified.
* Codex CLI (ChatGPT login): Codex writes ``token_count`` events with
  ``rate_limits`` into ``~/.codex/sessions/**/rollout-*.jsonl``; we tail them
  and SIGINT the child at the limit.

Interactive Claude Code is never signalled while the hook works: the deny
ends the turn (``continue: false``) and the session stays open, so the user
can ``agentcap approve`` (+50%, at most twice) or quit.  Headless runs
(``claude -p``) and Codex are still SIGINT'd at the limit.

Percentages are account-level, so parallel sessions are counted too (we warn
when we detect them).  Values update once per model response, so a job can
overshoot by up to one request.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence, TextIO

from .failsafe import EXIT_GUARD_FAILED, GuardFailure, JobLock
from .lifeline import GroupWatchdog, mark_child_start_failure
from .lifeline import spawn as lifeline_spawn
from .private import ensure_private_dir, open_private, write_private_text
from .procstop import ProcessTreeGuard, ZombieCollector, denied_warning, reap_group, stop_tree

WINDOWS = ("week", "5h")
WARN_FRACTION = 0.8
# Below this a whole-percent cap leaves room for about one model response (Codex, 10 Nov 2026:
# one gpt-6.1-sol high response used 1% of the 5-hour window).
SMALL_CAP_PCT = 3.0
EXIT_QUOTA_EXCEEDED = 3
EXIT_LOOP_DETECTED = 4
BASELINE_MAX_AGE_S = 15 * 60
APPROVE_FACTOR = 1.5
APPROVE_MAX = 2

# ------------------------------------------------------ measurement contract
# One reading record is the only quota representation used by the product.
# Every surface (status line, `agentcap quota`, the hook, the receipt and the
# Claude Code plugin) renders these fields through the same state function;
# none of them invents a share, an age or a state of its own.
READING_MAX_AGE_S = {"claude": 60.0, "codex": 600.0}
READING_RESOLUTION_PCT = 1.0           # both supported sources report whole percent
READING_UNCERTAINTY_PCT = (1.0, 3.0)   # one source step + up to one model response
DEFAULT_MAX_AGE_S = READING_MAX_AGE_S["codex"]
NO_DATA_WARN_S = 90.0  # startup grace: no reading yet is normal for this long

STATE_STARTING = "starting"
STATE_PROTECTED = "protected"
STATE_STALE = "stale"
STATE_STOPPED = "stopped"

STATE_TEXT = {
    STATE_STARTING: "waiting for the first reading",
    STATE_PROTECTED: "protection active",
    STATE_STALE: "data stale (protection not verified)",
    STATE_STOPPED: "stopped",
}
WATCH_ONLY_TEXT = "watch only (Pro stops at the limit)"


def uncertainty_text() -> str:
    """The honesty marker printed next to every measured percentage."""

    low, high = READING_UNCERTAINTY_PCT
    return f"±{low:g}-{high:g}%"


def protection_text(prot: "Protection", *, enforce: bool = True) -> str:
    """The state text a surface may show: Free never claims enforcement."""

    if prot.state == STATE_PROTECTED and not enforce:
        return WATCH_ONLY_TEXT
    return prot.text
# Codex writes usage only when a model response ends; at high reasoning effort one response
# ran 6+ minutes with no reading (11 Nov 2026, gpt-6-astra max).  Quota mode cuts a single
# response after this long (Pro) so the job cannot spend an unseen amount; SIGINT makes Codex
# write the response's usage, which the final read then picks up.
RESPONSE_TIMEOUT_S = 300.0
FINAL_READ_S = 10.0
LONG_EFFORTS = ("xhigh", "max")


HEADLESS_CLAUDE_QUOTA_WARNING = (
    "agentcap: WARNING: quota % is only supported in interactive Claude Code sessions. "
    "Headless `claude -p` exposes no usage % to AgentCap, so this job will NOT be stopped "
    "at the quota; the loop and error breakers are still active. Run `claude` "
    "interactively for quota mode."
)


def is_headless_claude(argv: Sequence[str]) -> bool:
    """``claude -p`` / ``--print`` runs non-interactively; nobody is there to approve."""

    return any(a in ("-p", "--print") or a.startswith("--print=") for a in argv[1:])


def _running_jobs(root: Path) -> list[Path]:
    jobs = root / "jobs"
    if not jobs.is_dir():
        return []
    out = [d for d in jobs.iterdir()
           if (d / "job.json").exists() and not (d / "done.json").exists()]
    return sorted(out, key=lambda d: (d / "job.json").stat().st_mtime, reverse=True)


def approve_job(root: Path | None = None) -> str:
    """Raise the newest running quota job's limit by 50% (max twice) and clear a loop stop."""

    root = root or state_root()
    running = _running_jobs(root)
    if not running:
        return "agentcap: no running quota job to approve."
    d = running[0]
    job = _read_json(d / "job.json") or {}
    if not job.get("enforce"):
        return "agentcap: this job is observe-only (Free); approve is a Pro feature."
    if job.get("stopped"):
        return ("agentcap: this job was already stopped (SIGINT), so approve cannot resume it. "
                "Start it again with a higher --quota.")
    n = int(job.get("approvals") or 0)
    if n >= APPROVE_MAX:
        return (f"agentcap: already approved twice (limit {float(job.get('quota', 0)):g}%); "
                "start a new job if you really need more.")
    new = round(float(job.get("quota", 0)) * APPROVE_FACTOR, 4)
    loop_cleared = (d / "loop.json").exists()
    error_cleared = (d / "errors.json").exists()
    job.update({"quota": new, "approvals": n + 1, "approved_at": time.time()})
    _atomic_write(d / "job.json", job)
    status = _read_json(d / "status.json")
    if status:
        status["limit_hit"] = False
        status["quota"] = new
        _atomic_write(d / "status.json", status)
    for name in ("loop.json", "errors.json", "denies.log"):
        try:
            (d / name).unlink()
        except OSError:
            pass
    tools = _read_json(d / "tools.json")
    if tools:  # keep the last tool name for the receipt; restart the streak
        tools.update({"count": 0, "digest": None})
        _atomic_write(d / "tools.json", tools)
    if new >= 100:
        head = (f"agentcap: approved ({n + 1}/{APPROVE_MAX}): limit is now {new:g}% "
                "(above 100%), so the quota no longer stops this job")
    else:
        head = (f"agentcap: approved ({n + 1}/{APPROVE_MAX}): limit raised to {new:g}% of the "
                f"{'weekly' if job.get('window') == 'week' else '5-hour'} quota")
    if loop_cleared:
        head += "; loop breaker cleared"
    elif error_cleared:
        head += "; error breaker cleared"
    return head + "; the run continues."


AGENT_ENV_MARKERS = ("AGENTCAP_QUOTA_JOB", "AGENTCAP_GUARDED", "CLAUDECODE")
SELF_APPROVE_RE = re.compile(r"\bagentcap(?:\.cli)?\s+approve\b")
_QUOTE_OR_ESCAPE_RE = re.compile(r"[\"'\\]")
_SHELL_IFS_RE = re.compile(r"\$\{IFS\}|\$IFS")


def _normalized_shell_text(command: str) -> str:
    """Strip the cheap shell obfuscations an agent can use around a word.

    The hook sees the raw Bash string, so ``agentcap 'approve'`` and
    ``agentcap a''pprove`` reach the same argv as ``agentcap approve``.  Quotes,
    backslash escapes and the ``$IFS`` word separator do not change the command
    a shell runs, so they must not hide a self-approve from the guard.
    """

    text = _QUOTE_OR_ESCAPE_RE.sub("", command)
    text = _SHELL_IFS_RE.sub(" ", text)
    return " ".join(text.split())


def is_self_approve_command(command: str) -> bool:
    """True when a Bash command would run ``agentcap approve``."""

    return bool(SELF_APPROVE_RE.search(_normalized_shell_text(command)))


def self_approve_refusal(env: Mapping[str, str]) -> str | None:
    """Refuse an approve made from inside an agent session: only a human may raise a limit."""

    for name in AGENT_ENV_MARKERS:
        if env.get(name):
            return (f"agentcap: approve refused: it was run from inside an agent session "
                    f"(found {name} in the environment). Only a human may raise a limit: run "
                    "`agentcap approve` in a separate terminal.")
    return None


CLAUDE_KEYS = {"5h": "five_hour", "week": "seven_day"}
CODEX_MINUTES = {"5h": 300, "week": 10080}
STATE_ENV = "AGENTCAP_QUOTA_DIR"


def state_root() -> Path:
    env = os.environ.get(STATE_ENV)
    if env:
        return Path(env).expanduser()
    return Path.home() / ".local" / "state" / "agentcap" / "quota"


def codex_home() -> Path:
    env = os.environ.get("CODEX_HOME")
    return Path(env).expanduser() if env else Path.home() / ".codex"


def parse_quota(raw: str) -> float:
    text = str(raw).strip().rstrip("%").strip()
    try:
        value = float(text)
    except ValueError:
        raise ValueError(f"--quota must look like 5% (got {raw!r})") from None
    if not (0 < value <= 100):
        raise ValueError("--quota must be between 0 and 100 percent")
    return value


def parse_duration(raw: str) -> float | None:
    """'5m', '90s', '1h', '300' (seconds) or 'off' -> seconds (None = off)."""

    text = str(raw).strip().lower()
    if text in ("off", "0", "none", "no"):
        return None
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*(s|sec|m|min|h)?", text)
    if not m:
        raise ValueError(f"--response-timeout: use e.g. 5m, 90s or off, not {raw!r}")
    value = float(m.group(1)) * {"s": 1, "sec": 1, None: 1, "m": 60, "min": 60, "h": 3600}[m.group(2)]
    if value < 30:
        raise ValueError("--response-timeout must be at least 30s (or off)")
    return value


def _atomic_write(path: Path, data: Mapping[str, Any]) -> None:
    write_private_text(path, json.dumps(data))


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


# ------------------------------------------------------------------ readings
@dataclass(frozen=True)
class Reading:
    """One quota observation, from one source, for one window.

    ``read_at`` is the wall-clock time the source produced this reading.  A
    cached or re-read record keeps its original ``read_at``; only a genuinely
    new observation moves it forward.  ``resolution`` is the step the source
    reports in (whole percent today), and ``observed`` is False only for a
    placeholder that carries no measurement.
    """

    percent: float  # 0-100, account level
    resets_at: float | None = None  # unix seconds
    source: str = "unknown"
    read_at: float = 0.0
    max_age: float = DEFAULT_MAX_AGE_S
    window: str = ""
    resolution: float = READING_RESOLUTION_PCT
    observed: bool = True

    @property
    def used(self) -> float:
        """Legacy name; the history importer and older sample logs use it."""

        return self.percent

    def age(self, now: float) -> float | None:
        return None if self.read_at <= 0 else max(0.0, now - self.read_at)

    def to_dict(self) -> dict[str, Any]:
        return {
            "percent": self.percent,
            "used": self.percent,  # legacy alias for sample logs written before 0.4
            "resets_at": self.resets_at,
            "source": self.source,
            "read_at": self.read_at,
            "max_age": self.max_age,
            "window": self.window,
            "resolution": self.resolution,
            "observed": self.observed,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | None) -> "Reading | None":
        if not isinstance(data, Mapping):
            return None
        percent = _num(data.get("percent"))
        if percent is None:
            percent = _num(data.get("used"))
        if percent is None:
            return None
        return cls(
            percent=percent,
            resets_at=_num(data.get("resets_at")),
            source=str(data.get("source") or "unknown"),
            read_at=_num(data.get("read_at")) or 0.0,
            max_age=_num(data.get("max_age")) or DEFAULT_MAX_AGE_S,
            window=str(data.get("window") or ""),
            resolution=_num(data.get("resolution")) or READING_RESOLUTION_PCT,
            observed=bool(data.get("observed", True)),
        )


@dataclass(frozen=True)
class Protection:
    """What AgentCap may truthfully say about the guard at one instant."""

    state: str
    text: str
    source: str | None = None
    window: str | None = None
    percent: float | None = None
    read_at: float | None = None
    age_s: float | None = None
    max_age_s: float | None = None
    resolution: float | None = None
    observed: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "state_text": self.text,
            "source": self.source,
            "window": self.window,
            "percent": self.percent,
            "read_at": self.read_at,
            "age_s": self.age_s,
            "max_age_s": self.max_age_s,
            "resolution_pct": self.resolution,
            "observed": self.observed,
        }


def state_text(state: str) -> str:
    return STATE_TEXT.get(state, STATE_TEXT[STATE_STALE])


def protection_state(
    *,
    started_at: float,
    now: float,
    reading: Reading | None = None,
    source: str | None = None,
    window: str | None = None,
    max_age: float | None = None,
    stopped: bool = False,
    source_lost: bool = False,
    startup_grace_s: float = NO_DATA_WARN_S,
) -> Protection:
    """The single mapping from evidence to the four user-visible states.

    No reading is not the same as a zero reading.  A reading older than its
    ``max_age`` is never refreshed here and can never read as protected, and a
    source that has gone away stays "data stale (protection not verified)"
    until a genuinely new reading arrives.
    """

    observed = reading is not None and reading.observed
    source = source or (reading.source if reading is not None else None)
    window = window or (reading.window if reading is not None else None)
    max_age_s = max_age or (reading.max_age if reading is not None else None) or DEFAULT_MAX_AGE_S
    percent = reading.percent if observed else None
    read_at = reading.read_at if observed else None
    age_s = max(0.0, now - read_at) if read_at is not None and read_at > 0 else None
    if stopped:
        state = STATE_STOPPED
    elif source_lost:
        state = STATE_STALE
    elif percent is None or age_s is None:
        state = STATE_STARTING if now - started_at < startup_grace_s else STATE_STALE
    elif age_s > max_age_s:
        state = STATE_STALE
    else:
        state = STATE_PROTECTED
    return Protection(
        state=state,
        text=STATE_TEXT[state],
        source=source,
        window=window,
        percent=percent,
        read_at=read_at,
        age_s=age_s,
        max_age_s=max_age_s,
        resolution=reading.resolution if reading is not None else None,
        observed=observed,
    )


def _num(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def claude_readings(payload: Mapping[str, Any], *, now: float | None = None) -> dict[str, Reading]:
    """Parse Claude Code statusline stdin JSON (``rate_limits`` object)."""

    now = time.time() if now is None else now
    out: dict[str, Reading] = {}
    limits = payload.get("rate_limits") if isinstance(payload, Mapping) else None
    if not isinstance(limits, Mapping):
        return out
    for window, key in CLAUDE_KEYS.items():
        block = limits.get(key)
        if not isinstance(block, Mapping):
            continue
        used = _num(block.get("used_percentage"))
        if used is None:
            continue
        out[window] = Reading(
            used,
            _num(block.get("resets_at")),
            source="claude",
            read_at=now,
            max_age=READING_MAX_AGE_S["claude"],
            window=window,
        )
    return out


def codex_readings(event: Mapping[str, Any], *, now: float | None = None) -> dict[str, Reading]:
    """Parse one Codex rollout line (``event_msg`` / ``token_count``)."""

    out: dict[str, Reading] = {}
    if not isinstance(event, Mapping):
        return out
    payload = event.get("payload") if event.get("type") == "event_msg" else event
    if not isinstance(payload, Mapping) or payload.get("type") != "token_count":
        return out
    limits = payload.get("rate_limits")
    if not isinstance(limits, Mapping):
        return out
    now = time.time() if now is None else now
    for slot in ("primary", "secondary"):
        block = limits.get(slot)
        if not isinstance(block, Mapping):
            continue
        used = _num(block.get("used_percent"))
        minutes = _num(block.get("window_minutes"))
        if used is None or minutes is None:
            continue
        resets = _num(block.get("resets_at"))
        if resets is None:
            rel = _num(block.get("resets_in_seconds"))
            resets = now + rel if rel is not None else None
        for window, expected in CODEX_MINUTES.items():
            if abs(minutes - expected) <= expected * 0.05:
                out[window] = Reading(
                    used,
                    resets,
                    source="codex",
                    read_at=now,
                    max_age=READING_MAX_AGE_S["codex"],
                    window=window,
                )
    return out


# ------------------------------------------------------------------- tracker
@dataclass
class ShareTracker:
    """Job share = usage since the baseline, across window resets."""

    baseline: float | None = None
    last_used: float | None = None
    last_reset: float | None = None
    carried: float = 0.0
    resets_seen: int = 0
    baseline_exact: bool = False

    def seed(self, reading: Reading) -> None:
        self.baseline = reading.used
        self.last_used = reading.used
        self.last_reset = reading.resets_at
        self.baseline_exact = True

    def update(self, reading: Reading) -> float:
        if self.baseline is None:
            self.baseline = reading.used
            self.last_used = reading.used
            self.last_reset = reading.resets_at
            return self.share
        new_window = False
        if (
            reading.resets_at is not None
            and self.last_reset is not None
            and reading.resets_at > self.last_reset + 60
        ):
            new_window = True
        elif (
            self.last_used is not None
            and reading.used < self.last_used - 0.5
            and not (reading.resets_at is not None and self.last_reset is not None
                     and abs(reading.resets_at - self.last_reset) <= 60)
        ):
            new_window = True
        elif self.last_used is not None and reading.used < self.last_used:
            # Same window, lower %: whole-percent jitter between sources
            # (seen live: 12 -> 13 -> 12).  Keep the high-water mark.
            return self.share
        if new_window:
            self.carried += max(0.0, (self.last_used or 0.0) - self.baseline)
            self.baseline = 0.0
            self.resets_seen += 1
        self.last_used = reading.used
        if reading.resets_at is not None:
            self.last_reset = reading.resets_at
        return self.share

    @property
    def share(self) -> float:
        if self.baseline is None or self.last_used is None:
            return 0.0
        return round(self.carried + max(0.0, self.last_used - self.baseline), 3)

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | None) -> "ShareTracker":
        tracker = cls()
        for key in tracker.__dict__:
            if data and key in data:
                setattr(tracker, key, data[key])
        return tracker


def baseline_from_last(last: Mapping[str, Any] | None, window: str, *, now: float) -> Reading | None:
    """A recent pre-job reading in the same window gives an exact baseline."""

    if not last:
        return None
    ts = _num(last.get("ts"))
    block = (last.get("readings") or {}).get(window)
    if ts is None or not isinstance(block, Mapping) or now - ts > BASELINE_MAX_AGE_S:
        return None
    reading = Reading.from_dict(block)
    if reading is None:
        return None
    if reading.resets_at is not None and reading.resets_at <= now:
        return None
    return Reading(
        reading.percent,
        reading.resets_at,
        source=reading.source if reading.source != "unknown" else "claude",
        read_at=ts,
        max_age=reading.max_age,
        window=window,
        resolution=reading.resolution,
    )


def _append_sample(record: Mapping[str, Any]) -> None:
    """Keep a small local history of quota % changes for `agentcap report`."""

    try:
        path = state_root() / "claude_samples.jsonl"
        ensure_private_dir(path.parent)
        fd = open_private(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND)
        with os.fdopen(fd, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\n")
    except OSError:
        pass


def _readings_dict(readings: Mapping[str, Reading]) -> dict[str, Any]:
    return {w: r.to_dict() for w, r in readings.items()}


def _reading_signature(readings: Any) -> dict[str, tuple[float | None, float | None]]:
    """The measured values only: metadata (read_at) changes every render."""

    if not isinstance(readings, Mapping):
        return {}
    out: dict[str, tuple[float | None, float | None]] = {}
    for window, block in readings.items():
        if not isinstance(block, Mapping):
            continue
        out[str(window)] = (_num(block.get("percent", block.get("used"))), _num(block.get("resets_at")))
    return out


# ------------------------------------------------- Claude: statusline + hook
def user_statusline_command(cwd: Path | None = None, home: Path | None = None) -> str | None:
    """The statusLine command the user would get without AgentCap.

    Precedence (low → high): user, project, local project settings.
    """

    home = home or Path.home()
    cwd = cwd or Path.cwd()
    found: str | None = None
    for path in (
        home / ".claude" / "settings.json",
        cwd / ".claude" / "settings.json",
        cwd / ".claude" / "settings.local.json",
    ):
        data = _read_json(path)
        if not data or "statusLine" not in data:
            continue
        block = data.get("statusLine")
        if isinstance(block, Mapping) and block.get("type", "command") == "command":
            cmd = block.get("command")
            found = cmd if isinstance(cmd, str) and cmd.strip() else None
        else:
            found = None
    return found


def claude_settings(job_dir: Path, python: str | None = None) -> dict[str, Any]:
    py = shlex.quote(python or sys.executable)
    job = shlex.quote(str(job_dir))
    base = f"{py} -m agentcap.quota"
    return {
        "statusLine": {
            "type": "command",
            "command": f"{base} statusline --job {job}",
            "refreshInterval": 5,
        },
        "hooks": {
            "PreToolUse": [
                {
                    "matcher": "*",
                    "hooks": [
                        {"type": "command", "command": f"{base} hook --job {job}", "timeout": 10}
                    ],
                }
            ],
            # Parallel tool batches: results reach the transcript only after the
            # batch, so failures are also counted as they happen.
            "PostToolUse": [
                {"matcher": "*", "hooks": [
                    {"type": "command", "command": f"{base} post --job {job}", "timeout": 10}]}
            ],
            "PostToolUseFailure": [
                {"matcher": "*", "hooks": [
                    {"type": "command", "command": f"{base} post --job {job}", "timeout": 10}]}
            ],
        },
    }


def _api_calls_seen(payload: Any) -> bool:
    cost = payload.get("cost") if isinstance(payload, Mapping) else None
    if not isinstance(cost, Mapping):
        return True  # unknown statusline schema: keep the warning
    try:
        return float(cost.get("total_api_duration_ms") or 0) > 0
    except (TypeError, ValueError):
        return True


def statusline_main(job_dir: Path, stdin: TextIO, stdout: TextIO, *, now: float | None = None) -> int:
    """Record rate_limits, then render the user's statusline unchanged."""

    raw = stdin.read()
    now = time.time() if now is None else now
    job = _read_json(job_dir / "job.json") or {}
    line = ""
    try:
        payload = json.loads(raw) if raw.strip() else {}
        readings = claude_readings(payload, now=now)
        status = _read_json(job_dir / "status.json") or {}
        sessions = set(status.get("sessions") or [])
        sid = payload.get("session_id") if isinstance(payload, Mapping) else None
        if isinstance(sid, str) and sid:
            sessions.add(sid)
        window = job.get("window", "week")
        quota = float(job.get("quota", 100))
        enforce = bool(job.get("enforce", True))
        previous_tracker = ShareTracker.from_dict(status.get("tracker"))
        tracker = ShareTracker.from_dict(status.get("tracker"))
        if window in readings:
            reading = readings[window]
            if tracker.baseline is None:
                seed = baseline_from_last(_read_json(state_root() / "claude_last.json"), window, now=now)
                if seed is not None and seed.used <= reading.used and (seed.resets_at == reading.resets_at or seed.resets_at is None):
                    tracker.seed(seed)
                    tracker.update(reading)
                else:
                    tracker.update(reading)
                    tracker.baseline_exact = False
            else:
                tracker.update(reading)
        share = tracker.share
        # A render without readings must not wipe the last observation: keep it
        # with its original read_at so its age keeps growing and the state can
        # never pass a stale reading off as fresh.
        stored = status.get("readings") if isinstance(status.get("readings"), dict) else {}
        if readings:
            stored = {**stored, **_readings_dict(readings)}
            read_at: float | None = now
            source_lost = False
        else:
            read_at = _num(status.get("read_at"))
            source_lost = bool(status.get("source_lost"))
            if read_at is None and not stored and _api_calls_seen(payload):
                source_lost = True  # this login cannot expose subscription quota at all
        current = readings.get(window)
        if current is None and isinstance(stored.get(window), Mapping):
            current = Reading.from_dict(stored[window])
        used = current.percent if current is not None else None
        resets_at = current.resets_at if current is not None else None
        protection = protection_state(
            started_at=float(job.get("started_at") or now), now=now, reading=current,
            source="claude", window=window, max_age=READING_MAX_AGE_S["claude"],
            source_lost=source_lost,
        )
        status_out = {
            "ts": now,
            "read_at": read_at,
            "source": "claude",
            "max_age": READING_MAX_AGE_S["claude"],
            "resolution": READING_RESOLUTION_PCT,
            "readings": stored,
            "agent": "claude",
            "window": window,
            "quota": quota,
            "share": share,
            "used": used,
            "resets_at": resets_at,
            "has_rate_limits": bool(readings),
            "source_lost": source_lost,
            "api_calls_seen": _api_calls_seen(payload),
            "limit_hit": bool(enforce and window in readings and share >= quota),
            "sessions": sorted(sessions),
            "tracker": tracker.to_dict(),
            "state": protection.state,
            "state_text": protection_text(protection, enforce=enforce),
            "window_reset": bool(tracker.resets_seen > previous_tracker.resets_seen),
        }
        _atomic_write(job_dir / "status.json", status_out)
        if readings:
            current_dict = _readings_dict(readings)
            previous = (_read_json(state_root() / "claude_last.json") or {}).get("readings")
            _atomic_write(state_root() / "claude_last.json", {"ts": now, "readings": current_dict})
            if _reading_signature(previous) != _reading_signature(current_dict):
                _append_sample({"ts": now, "readings": current_dict})
            meter = f"agentcap: job {share:.1f}% / {quota:g}% ({window}) {uncertainty_text()}"
            line = f"{meter} — {protection_text(protection, enforce=enforce)}"
        else:
            line = f"agentcap: {protection.text}"
    except Exception:
        line = "agentcap: quota unavailable"
    chain = job.get("chain")
    if isinstance(chain, str) and chain.strip():
        try:
            result = subprocess.run(
                chain, shell=True, input=raw, capture_output=True, text=True, timeout=8
            )
            if result.stdout:
                stdout.write(result.stdout if result.stdout.endswith("\n") else result.stdout + "\n")
        except Exception:
            pass
    stdout.write(line + "\n")
    stdout.flush()
    return 0


LOOP_REPEATS = 4   # same as the proxy loop breaker: same step 4x in a row
LOOP_ERRORS = 8    # 8 failing tool calls in a row


class LoopWatch:
    """Repeated-identical-tool-call / tool-error streaks from tool calls we can see
    without touching traffic: Claude ``PreToolUse`` payloads, Codex rollout lines."""

    def __init__(self) -> None:
        self.digest: str | None = None
        self.tool = ""
        self.count = 0
        self.errors = 0
        self.tripped: str | None = None

    def call(self, name: str, payload: Any) -> None:
        from .waste import POLL_TOOLS, _digest

        if name in POLL_TOOLS:
            return
        d = _digest(name, payload)
        self.count = self.count + 1 if d == self.digest else 1
        self.digest, self.tool = d, name
        if self.count >= LOOP_REPEATS and not self.tripped:
            self.tripped = f"same tool call {self.count} times in a row ({name[:40]})"

    def result(self, error: bool) -> None:
        self.errors = self.errors + 1 if error else 0
        if self.errors >= LOOP_ERRORS and not self.tripped:
            self.tripped = f"{self.errors} failing tool calls in a row ({self.tool[:40]})"

    def feed_codex(self, obj: Any) -> None:
        from .waste import _codex_error

        if not isinstance(obj, dict) or obj.get("type") != "response_item":
            return
        p = obj.get("payload")
        if not isinstance(p, dict):
            return
        if p.get("type") in ("function_call", "custom_tool_call", "local_shell_call"):
            self.call(str(p.get("name") or p.get("type")),
                      p.get("arguments", p.get("input", p.get("action"))))
        elif p.get("type") in ("function_call_output", "custom_tool_call_output"):
            self.result(_codex_error(p.get("output")))


def _loop_hook(job_dir: Path, payload: Mapping[str, Any]) -> str | None:
    """Track identical PreToolUse calls; return a deny reason when Pro should stop."""

    from .waste import POLL_TOOLS, _digest

    name = payload.get("tool_name")
    if not isinstance(name, str) or name in POLL_TOOLS:
        return None

    d = _digest(name, payload.get("tool_input"))
    state = _read_json(job_dir / "tools.json") or {}
    count = int(state.get("count") or 0) + 1 if state.get("digest") == d else 1
    _atomic_write(job_dir / "tools.json", {"digest": d, "count": count, "tool": name[:40]})
    if count < LOOP_REPEATS:
        return None
    enforce = bool((_read_json(job_dir / "job.json") or {}).get("enforce"))
    detail = f"same tool call {count} times in a row ({name[:40]})"
    _atomic_write(job_dir / "loop.json", {"count": count, "tool": name[:40], "detail": detail,
                                          "stopped": enforce, "ts": time.time()})
    if not enforce:
        return None
    _note_deny(job_dir)
    return (f"agentcap loop breaker: {detail}. "
            f"{PAUSE_TEXT}")


TRANSCRIPT_TAIL_BYTES = 1 << 20


def transcript_error_streak(path: Any) -> int:
    """Failing tool results in a row at the end of a Claude Code transcript.

    Claude Code's own session log (``transcript_path`` in every hook payload)
    records each ``tool_result`` with ``is_error``.  This also covers calls that
    never ran (permission blocks), which fire no PostToolUse hook.
    """

    if not isinstance(path, str) or not path:
        return 0
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - TRANSCRIPT_TAIL_BYTES))
            data = fh.read().decode("utf-8", "replace")
    except OSError:
        return 0
    streak = 0
    for line in data.splitlines():
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        msg = obj.get("message") if isinstance(obj, dict) else None
        content = msg.get("content") if isinstance(msg, dict) else None
        if not isinstance(content, list):
            continue
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                streak = streak + 1 if block.get("is_error") is True else 0
    return streak


def _error_hook(job_dir: Path, payload: Mapping[str, Any]) -> str | None:
    """Error breaker: 8 failing tool calls in a row, read from the transcript."""

    errors = max(transcript_error_streak(payload.get("transcript_path")),
                 int((_read_json(job_dir / "post_errors.json") or {}).get("errors") or 0))
    name = str(payload.get("tool_name") or "")[:40]
    _atomic_write(job_dir / "errors.json", {"errors": errors, "tool": name})
    if errors < LOOP_ERRORS:
        return None
    enforce = bool((_read_json(job_dir / "job.json") or {}).get("enforce"))
    detail = f"{errors} failing tool calls in a row ({name})"
    if not (job_dir / "loop.json").exists():
        _atomic_write(job_dir / "loop.json", {"count": errors, "tool": name, "detail": detail,
                                              "kind": "errors", "stopped": enforce, "ts": time.time()})
    if not enforce:
        return None
    _note_deny(job_dir)
    return (f"agentcap error breaker: {detail}. "
            f"{PAUSE_TEXT}")


def post_main(job_dir: Path, stdin: TextIO) -> int:
    """PostToolUse / PostToolUseFailure: live failing-call streak (fail-open)."""

    try:
        payload = json.loads(stdin.read() or "{}")
    except Exception:
        return 0
    if not isinstance(payload, dict):
        return 0
    event = payload.get("hook_event_name")
    path = job_dir / "post_errors.json"
    if event == "PostToolUseFailure":
        errors = int((_read_json(path) or {}).get("errors") or 0) + 1
    elif event == "PostToolUse":
        errors = 0
    else:
        return 0
    _atomic_write(path, {"errors": errors})
    return 0


PAUSE_TEXT = (
    'Pause here: summarise what is done and what is left, then wait for the user. This is a pause, not a permanent stop: if the user runs `agentcap approve` or tells you to continue, carry on with tool calls as normal.'
)


def _note_deny(job_dir: Path) -> None:
    try:
        with open(job_dir / "denies.log", "a", encoding="utf-8") as fh:
            fh.write(f"{time.time()}\n")
    except OSError:
        pass


def _deny(stdout: TextIO, reason: str) -> int:
    json.dump(
        {
            "continue": False,
            "stopReason": reason,
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": reason,
            },
        },
        stdout,
    )
    stdout.write("\n")
    return 0


def hook_main(job_dir: Path, stdin: TextIO, stdout: TextIO) -> int:
    """PreToolUse: deny on a tool loop or once the job's share reached the quota (fail-open)."""

    try:
        payload = json.loads(stdin.read() or "{}")
    except Exception:
        payload = {}
    if isinstance(payload, dict) and payload.get("tool_name") == "Bash":
        tool_input = payload.get("tool_input")
        command = tool_input.get("command") if isinstance(tool_input, Mapping) else None
        if isinstance(command, str) and is_self_approve_command(command):
            json.dump(
                {
                    "hookSpecificOutput": {
                        "hookEventName": "PreToolUse",
                        "permissionDecision": "deny",
                        "permissionDecisionReason": (
                            "agentcap: an agent may not approve its own limit; ask the user to run "
                            "agentcap approve in their own terminal."
                        ),
                    },
                },
                stdout,
            )
            stdout.write("\n")
            return 0
    try:
        loop_reason = _loop_hook(job_dir, payload) if isinstance(payload, dict) else None
    except Exception:
        loop_reason = None
    if loop_reason:
        return _deny(stdout, loop_reason)
    try:
        error_reason = _error_hook(job_dir, payload) if isinstance(payload, dict) else None
    except Exception:
        error_reason = None
    if error_reason:
        return _deny(stdout, error_reason)
    status = _read_json(job_dir / "status.json")
    if not status or not status.get("limit_hit"):
        return 0
    _note_deny(job_dir)
    reading = None
    block = (status.get("readings") or {}).get(status.get("window")) \
        if isinstance(status.get("readings"), Mapping) else None
    if isinstance(block, Mapping):
        reading = Reading.from_dict(block)
    elif status.get("used") is not None:
        reading = Reading(
            float(status["used"]), _num(status.get("resets_at")), source="claude",
            read_at=float(status.get("read_at") or status.get("ts") or 0.0),
            max_age=READING_MAX_AGE_S["claude"], window=str(status.get("window") or ""),
        )
    prot = protection_state(
        started_at=float(status.get("read_at") or status.get("ts") or time.time()),
        now=time.time(), reading=reading, source="claude",
        window=str(status.get("window") or ""), max_age=READING_MAX_AGE_S["claude"],
    )
    stale_note = (f" The last reading is {prot.age_s or 0:.0f}s old (max {prot.max_age_s or 0:.0f}s), "
                  f"so {STATE_TEXT[STATE_STALE]}." if prot.state == STATE_STALE else "")
    reason = (
        f"agentcap quota mode: this job used {float(status.get('share', 0)):.1f}% of the "
        f"{'weekly' if status.get('window') == 'week' else '5-hour'} subscription quota "
        f"{uncertainty_text()} (limit {float(status.get('quota', 0)):g}%) and the limit was hit."
        f"{stale_note} "
        f"{PAUSE_TEXT}"
    )
    json.dump(
        {
            "continue": False,
            "stopReason": reason,
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": reason,
            },
        },
        stdout,
    )
    stdout.write("\n")
    return 0


def inject_claude_settings(argv: Sequence[str], settings_path: Path) -> list[str]:
    argv = list(argv)
    return [argv[0], "--settings", str(settings_path), *argv[1:]]


# -------------------------------------------------------------- Codex tailer
class CodexTailer:
    def __init__(self, sessions_dir: Path, started_at: float) -> None:
        self.dir = sessions_dir
        self.started_at = started_at
        self.preexisting = {p: self._size(p) for p in self._files()}
        self.offsets: dict[Path, int] = {}
        self.own: Path | None = None
        self.others: set[Path] = set()
        self.loop = LoopWatch()
        self.read_errors = 0
        # Turn state from the rollout events (Codex 0.167): a model response is "in flight"
        # from task_started / a tool output / token_count until the next tool call or the
        # end of the turn.  Usage (token_count) is only written when a response ends, so the
        # in-flight time is AgentCap's blind spot.
        self.response_since: float | None = None
        self.pending_calls = 0
        self.turns = 0
        self.effort: str | None = None
        self.token_counts = 0          # token_count events in this job's rollout
        self.readings_seen = 0         # ... of which carried a reading for the window

    def track(self, obj: Any, now: float) -> None:
        if not isinstance(obj, dict):
            return
        kind = obj.get("type")
        p = obj.get("payload")
        ptype = p.get("type") if isinstance(p, dict) else None
        if kind == "turn_context" and isinstance(p, dict) and isinstance(p.get("effort"), str):
            self.effort = p["effort"]
        elif kind == "event_msg" and ptype == "task_started":
            self.turns += 1
            self.pending_calls = 0
            self.response_since = now
        elif kind == "event_msg" and ptype in ("task_complete", "turn_aborted"):
            self.pending_calls = 0
            self.response_since = None
        elif kind == "event_msg" and ptype == "token_count":
            self.token_counts += 1
            if self.pending_calls == 0 and self.turns:
                self.response_since = now  # the previous response ended; the next one starts
        elif kind == "response_item" and ptype in ("function_call", "custom_tool_call", "local_shell_call"):
            self.pending_calls += 1
            self.response_since = None     # a tool is running: no model time is being spent
        elif kind == "response_item" and ptype in ("function_call_output", "custom_tool_call_output"):
            self.pending_calls = max(0, self.pending_calls - 1)
            if self.pending_calls == 0:
                self.response_since = now

    def _files(self) -> list[Path]:
        if not self.dir.exists():
            return []
        return list(self.dir.rglob("rollout-*.jsonl"))

    @staticmethod
    def _size(path: Path) -> int:
        try:
            return path.stat().st_size
        except OSError:
            return 0

    def last_reading_before(self, window: str) -> tuple[float, Reading] | None:
        best: tuple[float, Reading] | None = None
        for path in self.preexisting:
            try:
                mtime = path.stat().st_mtime
                lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                continue
            for line in reversed(lines):
                try:
                    reading = codex_readings(json.loads(line), now=mtime).get(window)
                except ValueError:
                    continue
                if reading:
                    if best is None or mtime > best[0]:
                        best = (mtime, reading)
                    break
        return best

    def poll(self, window: str) -> list[Reading]:
        """New readings from this job's rollout file (first new file)."""

        readings: list[Reading] = []
        for path in self._files():
            if path in self.preexisting:
                if self._size(path) > self.preexisting[path]:
                    self.others.add(path)
                continue
            if self.own is None:
                self.own = path
            elif path != self.own:
                self.others.add(path)
        if self.own is None:
            return readings
        offset = self.offsets.get(self.own, 0)
        try:
            with open(self.own, "rb") as fh:
                fh.seek(offset)
                chunk = fh.read()
        except OSError:
            self.read_errors += 1
            return readings
        self.read_errors = 0
        end = chunk.rfind(b"\n")
        if end < 0:
            return readings
        self.offsets[self.own] = offset + end + 1
        now = time.time()
        for line in chunk[: end + 1].decode("utf-8", "replace").splitlines():
            try:
                obj = json.loads(line)
                self.loop.feed_codex(obj)
                self.track(obj, now)
                reading = codex_readings(obj).get(window)
            except ValueError:
                continue
            if reading:
                self.readings_seen += 1
                readings.append(reading)
        return readings

    @property
    def response_open(self) -> bool:
        return self.response_since is not None


# --------------------------------------------------------------- the runner
def detect_quota_agent(argv: Sequence[str]) -> str | None:
    name = Path(argv[0]).name if argv else ""
    if name in ("claude", "claude-code"):
        return "claude"
    if name == "codex":
        return "codex"
    return None


@dataclass
class QuotaSummary:
    agent: str
    window: str
    quota: float
    share: float
    exit_code: int
    reason: str
    baseline_exact: bool
    resets_seen: int
    parallel: bool
    observed: bool
    final_read: str = "n/a"        # codex: "complete" when the last turn's usage was read
    effort: str | None = None
    protection: Protection | None = None


class _Terminated(Exception):
    """SIGTERM / SIGHUP reached AgentCap (e.g. `timeout`, a closed terminal)."""

    def __init__(self, sig: int) -> None:
        super().__init__(signal.Signals(sig).name)
        self.sig = sig


class QuotaRunner:
    def __init__(
        self,
        argv: Sequence[str],
        *,
        quota: float,
        window: str = "week",
        enforce: bool = True,
        alerts: Callable[[Mapping[str, Any]], None] | None = None,
        stderr: TextIO | None = None,
        poll_s: float = 0.5,
        claude_grace_s: float = 10.0,
        no_data_warn_s: float = 90.0,
        response_timeout_s: float | None = RESPONSE_TIMEOUT_S,
        final_read_s: float = FINAL_READ_S,
        reading_max_age_s: float | None = None,
        quiet: bool = False,
        job_root: Path | None = None,
        sessions_dir: Path | None = None,
        receipt_dir: Path | None = None,
        cwd: Path | None = None,
        home: Path | None = None,
    ) -> None:
        if window not in WINDOWS:
            raise ValueError("--window must be 'week' or '5h'")
        self.argv = list(argv)
        self.agent = detect_quota_agent(self.argv)
        if self.agent is None:
            raise ValueError("quota mode supports `claude` and `codex` only")
        self.headless = self.agent == "codex" or is_headless_claude(self.argv)
        self.quota = quota
        self.window = window
        self.enforce = enforce
        self.alerts = alerts or (lambda event: None)
        self.stderr = stderr or sys.stderr
        self.poll_s = poll_s
        self.claude_grace_s = claude_grace_s
        self.no_data_warn_s = no_data_warn_s
        self.response_timeout_s = response_timeout_s or None
        self.final_read_s = final_read_s
        self.reading_max_age_s = reading_max_age_s or READING_MAX_AGE_S.get(self.agent, DEFAULT_MAX_AGE_S)
        self.quiet = quiet
        self.job_root = job_root or state_root()
        self.sessions_dir = sessions_dir or (codex_home() / "sessions")
        self.receipt_dir = receipt_dir
        self.cwd = cwd
        self.home = home
        self.job_id = f"q{int(time.time())}-{os.getpid()}"
        self.job_dir = self.job_root / "jobs" / self.job_id
        self.child: subprocess.Popen | None = None
        self._pgid: int | None = None
        self.summary: QuotaSummary | None = None
        self.baseline_age_s: float | None = None  # set when the baseline is an older same-window reading
        self._started_at = 0.0
        self._last_reading: Reading | None = None
        self._source_lost = False
        self._state: str | None = None
        self._protection_now: Protection | None = None
        self._resets_announced = 0

    def _say(self, msg: str) -> None:
        if not self.quiet:
            print(msg, file=self.stderr, flush=True)

    def _event(self, kind: str, share: float, **extra: Any) -> None:
        if not self.enforce:
            return  # Free: observe-only, terminal output only
        event = {
            "type": kind,
            "run_id": self.job_id,
            "window": self.window,
            "quota_pct": self.quota,
            "share_pct": round(share, 2),
        }
        event.update(extra)
        try:
            self.alerts(event)
        except Exception:
            pass

    def _signal(self, sig: int) -> None:
        child = self.child
        if child is None or child.poll() is not None:
            return
        try:
            os.killpg(os.getpgid(child.pid), sig)
        except (ProcessLookupError, PermissionError, OSError):
            try:
                child.send_signal(sig)
            except OSError:
                pass

    def _stop(self) -> None:
        job = _read_json(self.job_dir / "job.json") or {}
        job["stopped"] = True  # a signalled job cannot be resumed by `agentcap approve`
        try:
            _atomic_write(self.job_dir / "job.json", job)
        except OSError:
            pass
        if self.child is not None:
            stop_tree(self.child, self._pgid, int_timeout=5.0, term_timeout=3.0, kill_timeout=2.0)

    def _protection(self, now: float, *, stopped: bool = False) -> Protection:
        """The one place a runner decides what it may claim about the guard."""

        return protection_state(
            started_at=self._started_at or now,
            now=now,
            reading=self._last_reading,
            source=self.agent,
            window=self.window,
            max_age=self.reading_max_age_s,
            stopped=stopped,
            source_lost=self._source_lost and not stopped,
            startup_grace_s=self.no_data_warn_s,
        )

    def _stale_detail(self, prot: Protection, now: float, share: float) -> str:
        if prot.percent is None and self._source_lost:
            if self.agent == "claude":
                return ("the source reports no subscription rate_limits (a Pro/Max login is "
                        "needed; use --cap for API keys)")
            return "the source reports no subscription rate_limits (a ChatGPT login is needed)"
        if prot.percent is None or prot.age_s is None:
            waited = max(0.0, now - (self._started_at or now))
            return f"no quota data yet (no reading in {waited:.0f}s, max {prot.max_age_s:.0f}s)"
        detail = (f"last reading {prot.age_s:.0f}s ago, older than {prot.max_age_s:.0f}s "
                  f"(last known share {share:.1f}%)")
        if self.agent == "claude":
            detail += "; the Claude statusline stopped reporting"
        elif self._last_reading is not None:
            detail += "; the Codex rollout stopped reporting"
        return detail

    def _apply_state(self, prot: Protection, now: float, share: float = 0.0) -> None:
        """Announce a state change once; never refresh a reading here."""

        self._protection_now = prot
        if prot.state == self._state:
            return
        previous, self._state = self._state, prot.state
        if prot.state == STATE_PROTECTED:
            age = f", reading {prot.age_s:.0f}s old" if prot.age_s is not None else ""
            self._say(f"agentcap: {protection_text(prot, enforce=self.enforce)} "
                      f"({self.agent}, {self.window}{age})")
        elif prot.state == STATE_STALE:
            self._say(f"agentcap: {prot.text}: {self._stale_detail(prot, now, share)}")
        elif prot.state == STATE_STOPPED and previous is not None:
            self._say(f"agentcap: {prot.text} at {share:.1f}% (limit {self.quota:g}%)")

    def run(self) -> int:
        lock = JobLock(state_root() / "quota.lock")
        try:
            lock.acquire()
        except GuardFailure as e:
            self._say('agentcap: ' + str(e))
            return 2
        watchdog = GroupWatchdog()
        tree_guard = ProcessTreeGuard()
        zombies = ZombieCollector()
        old_handlers: dict[int, Any] = {}
        try:
            started = time.time()
            self._started_at = started
            ensure_private_dir(self.job_dir)
            argv = list(self.argv)
            tailer: CodexTailer | None = None
            tracker = ShareTracker()
            if self.agent == "claude":
                job = {
                    "quota": self.quota,
                    "window": self.window,
                    "started_at": started,
                    "enforce": self.enforce,
                    "chain": user_statusline_command(self.cwd, self.home),
                }
                _atomic_write(self.job_dir / "job.json", job)
                settings = self.job_dir / "settings.json"
                _atomic_write(settings, claude_settings(self.job_dir))
                argv = inject_claude_settings(argv, settings)
            else:
                _atomic_write(self.job_dir / "job.json", {
                    "quota": self.quota, "window": self.window, "started_at": started,
                    "enforce": self.enforce, "agent": "codex",
                })
                tailer = CodexTailer(self.sessions_dir, started)
                self._tailer = tailer
                prior = tailer.last_reading_before(self.window)
                if prior and started - prior[0] <= BASELINE_MAX_AGE_S and (
                    prior[1].resets_at is None or prior[1].resets_at > started
                ):
                    tracker.seed(prior[1])
                elif prior and prior[1].resets_at is not None and prior[1].resets_at > started:
                    # An older reading from the *same* window (live 11 Nov 2026: a reading
                    # 20 min old was ignored, the first response's 1% went uncounted and a
                    # 2% cap let the account rise 3%).  Using it can only over-count: other
                    # usage since then is charged to this job, so it stops early, never late.
                    tracker.seed(prior[1])
                    self.baseline_age_s = started - prior[0]

            env = dict(os.environ)
            env["AGENTCAP_QUOTA_JOB"] = str(self.job_dir)
            if not (self.agent == "claude" and self.headless):  # headless: see the warning below
                self._say(
                    f"agentcap: quota mode ({self.agent}, {self.window}): {STATE_TEXT[STATE_STARTING]}; "
                    + (f"will stop this job at {self.quota:g}% of the account quota" if self.enforce
                       else f"observe only (Free); Pro would stop it at {self.quota:g}%")
                    + ". Read-only; your login and traffic are untouched."
                )
            if self.agent == "codex" and self.quota < 1:
                self._say(f"agentcap: WARNING: Codex reports quota in whole percent, so a {self.quota:g}% cap "
                          "cannot trigger until the job has used a full 1%. Use --quota 1% or more for Codex.")
            if 1 <= self.quota < SMALL_CAP_PCT and not (self.agent == "claude" and self.headless):
                self._say(f"agentcap: note: {self.agent} reports quota in 1% steps and one model response "
                          f"can use a full 1%, so a {self.quota:g}% cap may stop the job after its first "
                          f"response. Use about {SMALL_CAP_PCT:g}% or more for room to work.")
            if self.agent == "claude" and self.headless:
                self._say(HEADLESS_CLAUDE_QUOTA_WARNING)
            elif self.agent == "claude" and self.quota < 1:
                self._say(f"agentcap: WARNING: Claude Code reports quota in whole percent, so a {self.quota:g}% "
                          "cap trips at the next 1% step of the account (it may stop at once or overshoot). "
                          "Use --quota 1% or more for Claude Code.")
            watchdog.start()  # before the tree guard, so its baseline excludes it
            tree_guard.start()
            try:
                self.child = lifeline_spawn(argv, env=env, start_new_session=True, cwd=self.cwd)
            except OSError as exc:
                raise mark_child_start_failure(exc)
            try:
                self._pgid = os.getpgid(self.child.pid)
            except OSError:
                self._pgid = None
            watchdog.watch(self._pgid)

            if threading.current_thread() is threading.main_thread():
                def _term(sig: int, _frame: Any) -> None:
                    raise _Terminated(sig)
                for sig in (signal.SIGTERM, signal.SIGHUP):
                    try:
                        old_handlers[sig] = signal.signal(sig, _term)
                    except (ValueError, OSError):
                        pass
            exec_mode = self.agent == "codex" and len(self.argv) > 1 and self.argv[1] in ("exec", "e")
            effort_said = timeout_said = False
            timed_out = False
            warned = hit = parallel = observed = False
            loop_detail: str | None = None
            loop_at: float | None = None
            hit_at: float | None = None
            reason = "exited"
            share = 0.0
            stopped = False
            open_said = False
            approvals = 0
            guard_msg: str | None = None
            try:
                while True:
                    code = self.child.poll()
                    now = time.time()
                    jobnow = _read_json(self.job_dir / "job.json") or {}
                    if int(jobnow.get("approvals") or 0) > approvals and code is None:
                        approvals = int(jobnow["approvals"])
                        self.quota = float(jobnow.get("quota", self.quota))
                        hit = warned = stopped = open_said = False
                        hit_at = loop_at = None
                        loop_detail = None
                        self._say(f"agentcap: approved ({approvals}/{APPROVE_MAX}): limit is now "
                                  f"{self.quota:g}%; continuing.")
                        self._event("quota_approved", share, approvals=approvals)
                    if self.agent == "claude":
                        status = _read_json(self.job_dir / "status.json") or {}
                        current: Reading | None = None
                        block = (status.get("readings") or {}).get(self.window) \
                            if isinstance(status.get("readings"), Mapping) else None
                        if isinstance(block, Mapping):
                            current = Reading.from_dict(block)
                        elif status.get("used") is not None:
                            current = Reading(
                                float(status["used"]), _num(status.get("resets_at")),
                                source="claude",
                                read_at=float(status.get("read_at") or status.get("ts") or 0.0),
                                max_age=self.reading_max_age_s, window=self.window,
                            )
                        if current is not None:
                            self._last_reading = current
                            observed = True
                            share = float(status.get("share") or 0.0)
                            tracker = ShareTracker.from_dict(status.get("tracker"))
                        if status:
                            if status.get("has_rate_limits"):
                                self._source_lost = False
                            elif current is None and status.get("api_calls_seen", True):
                                self._source_lost = True
                        if len(status.get("sessions") or []) > 1 and not parallel:
                            parallel = True
                    else:
                        assert tailer is not None
                        for reading in tailer.poll(self.window):
                            observed = True
                            self._last_reading = reading
                            tracker.update(reading)
                        share = tracker.share
                        if tailer.read_errors >= 3 and guard_msg is None:
                            self._source_lost = True
                            guard_msg = ('Codex session log is unreadable; the guard cannot see '
                                         'this run, stopping it (fail-closed).')
                            if self.enforce:
                                self._say('agentcap: ' + guard_msg)
                                self._event('guard_failed', share, detail=guard_msg)
                                self._stop()
                                stopped = True
                            else:
                                self._say('agentcap: WARNING: ' + guard_msg.replace(
                                    'stopping it (fail-closed)', 'Pro would stop it'))
                        if tailer.others and not parallel:
                            parallel = True
                        strict = None
                        if guard_msg is None and tailer.token_counts >= 2 and tailer.readings_seen == 0:
                            self._source_lost = True
                            strict = (f"Codex reports no subscription rate_limits for the {self.window} "
                                      "window (API-key login, or a changed log format); the guard cannot "
                                      "see this run, stopping it (fail-closed).")
                        elif (guard_msg is None and exec_mode and tailer.own is None and code is None
                              and now - started > self.no_data_warn_s):
                            self._source_lost = True
                            strict = (f"no Codex session log appeared in {int(self.no_data_warn_s)}s "
                                      f"(looked in {self.sessions_dir}); the guard cannot see this run, "
                                      "stopping it (fail-closed).")
                        if strict:
                            guard_msg = strict
                            if self.enforce:
                                self._say("agentcap: " + strict)
                                self._event("guard_failed", share, detail=strict)
                                self._stop()
                                stopped = True
                            else:
                                self._say("agentcap: WARNING: " + strict.replace(
                                    "stopping it (fail-closed)", "Pro would stop it"))
                        if tailer.effort in LONG_EFFORTS and not effort_said:
                            effort_said = True
                            cut = (f"; AgentCap cuts a single response after "
                                   f"{self.response_timeout_s / 60:g} min" if self.response_timeout_s else "")
                            self._say(f"agentcap: note: Codex runs at {tailer.effort} effort. One response "
                                      "can think for minutes and its quota use only shows when it ends, so "
                                      f"the stop can come late{cut}. Use high or lower effort for tight caps.")
                        if (self.response_timeout_s and tailer.response_since is not None
                                and not stopped and code is None
                                and now - tailer.response_since > self.response_timeout_s
                                and not timeout_said):
                            timeout_said = True
                            mins = f"{self.response_timeout_s / 60:g} min"
                            if self.enforce:
                                timed_out = True
                                self._say(f"agentcap: one model response has run for more than {mins} with "
                                          "no usage reading; stopping the agent so it cannot spend an unseen "
                                          "amount of quota (response timeout).")
                                self._event("response_timeout", share, seconds=int(self.response_timeout_s))
                                self._stop()
                                stopped = True
                            else:
                                self._say(f"agentcap: WARNING: one model response has run for more than "
                                          f"{mins} with no usage reading; Pro would stop it here.")
                    if parallel and not getattr(self, "_par_said", False):
                        self._par_said = True
                        self._say("agentcap: WARNING: another session is using the same account; "
                                  "quota % is account-level, so its usage counts against this job.")
                        self._event("quota_parallel", share)
                    if tracker.resets_seen > self._resets_announced:
                        self._resets_announced = tracker.resets_seen
                        label = "weekly" if self.window == "week" else "5-hour"
                        self._say(f"agentcap: the {label} window reset; the job's share restarts in the "
                                  f"new window (previous {tracker.carried:.1f}% kept, the reset itself "
                                  "adds no usage).")
                    # Whole-percent sources can jump straight past 80% to the limit; a warning
                    # printed together with the stop tells the user nothing, so skip it then.
                    if observed and not warned and self.quota * WARN_FRACTION <= share < self.quota:
                        warned = True
                        self._event("quota_threshold", share, threshold=80)
                    if observed and not hit and share >= self.quota:
                        hit = True
                        hit_at = now
                        if self.enforce:
                            self._event("quota_exceeded", share)
                            if self.agent == "codex":
                                self._stop()
                                stopped = True
                    if (
                        hit and self.enforce and self.agent == "claude" and not stopped
                        and hit_at is not None and code is None
                    ):
                        denies = (self.job_dir / "denies.log").exists()
                        if not self.headless and denies:
                            open_said = self._say_open(open_said, "the quota limit")
                        elif now - hit_at >= (self.claude_grace_s if denies else self.claude_grace_s * 3):
                            self._stop()  # headless, or the hook never ran (fallback)
                            stopped = True
                    if loop_detail is None:
                        if self.agent == "claude":
                            lj = _read_json(self.job_dir / "loop.json")
                            loop_detail = lj.get("detail") if lj else None
                        elif tailer is not None:
                            loop_detail = tailer.loop.tripped
                        if loop_detail:
                            loop_at = now
                            if self.enforce:
                                self._say(f"agentcap: loop breaker: {loop_detail}. Stopping the agent.")
                                self._event("loop_detected", share, detail=loop_detail)
                                if self.agent == "codex":
                                    self._stop()
                                    stopped = True
                    if (loop_detail and self.enforce and not stopped and code is None
                            and loop_at is not None):
                        denies = (self.job_dir / "denies.log").exists()
                        if not self.headless and denies:
                            open_said = self._say_open(open_said, "the loop breaker")
                        elif now - loop_at >= (self.claude_grace_s if denies or self.headless
                                               else self.claude_grace_s * 3):
                            self._stop()
                            stopped = True
                    state_stopped = bool(self.enforce and (hit or loop_detail or timed_out))
                    self._apply_state(self._protection(now, stopped=state_stopped), now, share)
                    self._live(share, observed, hit)
                    if code is not None:
                        break
                    zombies.tick()  # no-op unless AGENTCAP_COLLECT_ZOMBIES=1
                    time.sleep(self.poll_s)
            except KeyboardInterrupt:
                self._stop()
                reason = "interrupted"
            except _Terminated as term:
                self._say(f"agentcap: {term} received; stopping the agent and writing the receipt.")
                self._stop()
                reason = "terminated"
            except Exception as exc:
                guard_msg = f'internal error ({type(exc).__name__}: {exc})'
                if self.enforce:
                    self._say(f'agentcap: {guard_msg}; stopping the agent so it does not run unguarded (fail-closed).')
                    self._stop()
                else:
                    self._say(f'agentcap: {guard_msg}; observe-only run continues without AgentCap.')
            code = self.child.wait()
            for sig in old_handlers:  # the stop is done; a second TERM must not cut the receipt
                try:
                    signal.signal(sig, signal.SIG_IGN)
                except (ValueError, OSError):
                    pass
            reap_group(self._pgid)
            final_read = "n/a"
            if tailer is not None:
                deadline = time.time() + self.final_read_s
                while True:
                    for reading in tailer.poll(self.window):
                        observed = True
                        self._last_reading = reading
                        tracker.update(reading)
                    if not tailer.response_open or tailer.own is None or time.time() >= deadline:
                        break
                    time.sleep(min(0.25, self.poll_s * 5))
                share = tracker.share
                final_read = ("complete" if tailer.own is not None and not tailer.response_open
                              else "none" if tailer.own is None else "incomplete")
                if self.enforce and observed and not hit and share >= self.quota:
                    hit = True  # usage written as the agent stopped took the job over the limit
            try:
                escaped = tree_guard.sweep()
            finally:
                tree_guard.close()
            if escaped:
                self._say(
                    f"agentcap: killed {escaped} process(es) that left the agent's "
                    "process group (setsid/daemonized helpers)."
                )
            denied = denied_warning(tree_guard.denied_pids)
            if denied:
                self._say(denied)
            if loop_detail and self.enforce and not hit:
                reason = "loop_detected"
                code = EXIT_LOOP_DETECTED
            elif hit and self.enforce:
                reason = "quota_exceeded"
                code = EXIT_QUOTA_EXCEEDED
            elif timed_out and self.enforce:
                reason = "response_timeout"
                code = EXIT_LOOP_DETECTED
            elif guard_msg and self.enforce:
                reason = "guard_failed"
                code = EXIT_GUARD_FAILED
            if loop_detail and not self.enforce:
                self._say(f"agentcap: Pro's loop breaker would have stopped this job at {LOOP_REPEATS} repeats"
                          f" ({loop_detail}). https://agentcap.launchbench.studio/#pricing")
            state_stopped = bool(self.enforce and (hit or loop_detail or timed_out))
            if guard_msg and self.enforce:
                self._source_lost = True
            self._protection_now = self._protection(time.time(), stopped=state_stopped)
            self.summary = QuotaSummary(
                self.agent, self.window, self.quota, share, code, reason,
                tracker.baseline_exact, tracker.resets_seen, parallel, observed,
                final_read, tailer.effort if tailer is not None else None,
                self._protection_now,
            )
            self._receipt(self.summary)
            try:
                _atomic_write(self.job_dir / "done.json", {"ts": time.time(), "exit_code": code})
            except OSError:
                pass
            return code
        finally:
            try:
                tree_guard.sweep()
            except Exception:
                pass
            tree_guard.close()
            watchdog.disarm()
            lock.release()
            for sig, handler in old_handlers.items():
                try:
                    signal.signal(sig, handler)
                except (ValueError, OSError, TypeError):
                    pass

    def _say_open(self, said: bool, what: str) -> bool:
        if not said:
            self._say(f"agentcap: {what} stopped this turn (tool calls are denied). The session stays "
                      "open: run `agentcap approve` in another terminal to raise the limit by 50% "
                      f"(at most {APPROVE_MAX}x), or quit Claude Code.")
        return True

    def _last_tool(self) -> str | None:
        t = _read_json(self.job_dir / "tools.json") or {}
        name = t.get("tool")
        if not name and self.agent == "codex":
            tailer = getattr(self, "_tailer", None)
            name = tailer.loop.tool if tailer is not None else None
        return str(name)[:40] if name else None

    def _live(self, share: float, observed: bool, hit: bool) -> None:
        """Optional live status for editor integrations (AGENTCAP_QUOTA_LIVE)."""

        path = os.environ.get("AGENTCAP_QUOTA_LIVE")
        if not path:
            return
        prot = self._protection_now or self._protection(time.time())
        try:
            _atomic_write(Path(path), {
                "agent": self.agent, "window": self.window, "quota_pct": self.quota,
                "share_pct": share, "observed": observed, "limit_hit": hit,
                "enforce": self.enforce, "ts": time.time(),
                "state": prot.state,
                "state_text": protection_text(prot, enforce=self.enforce),
                "read_at": prot.read_at, "age_s": prot.age_s, "max_age_s": prot.max_age_s,
                "resolution_pct": prot.resolution,
                "uncertainty_pct": list(READING_UNCERTAINTY_PCT),
            })
        except OSError:
            pass

    def _receipt(self, s: QuotaSummary) -> None:
        label = "weekly" if s.window == "week" else "5-hour"
        prot = s.protection or self._protection(time.time())
        trust = uncertainty_text()
        if not s.observed:
            if s.reason in ("exited", "interrupted", "terminated"):
                # Nothing was measured: a receipt here would put a number or a
                # stop on a job AgentCap never saw.  Say so and write nothing.
                self._say("agentcap: no quota receipt: no reading was observed for this job, "
                          "so this run makes no claim about the account.")
                return
            self._say(f"agentcap: quota receipt: {prot.text}; no reading was observed for this job.")
        else:
            notes = []
            if not s.baseline_exact:
                notes.append("baseline taken after the first response, may undercount it")
            elif self.baseline_age_s is not None:
                notes.append(f"baseline is a reading from {self.baseline_age_s / 60:.0f} min before the "
                             "job, so other usage since then is counted too")
            if s.resets_seen:
                notes.append(f"window reset {s.resets_seen}x during the job (not counted as usage)")
            if s.parallel:
                notes.append("includes other sessions on the same account")
            if s.final_read == "incomplete":
                notes.append("the last response's usage was not written yet, may undercount")
            if prot.state == STATE_STALE:
                notes.append(
                    f"the last reading was {prot.age_s or 0:.0f}s old (max {prot.max_age_s or 0:.0f}s), "
                    "so protection was not verified at the end")
            self._say(
                f"agentcap: the account's {label} quota rose {s.share:.1f}% {trust} while this job ran"
                + (f" (limit {s.quota:g}%)" if self.enforce else "")
                + (f"; {'; '.join(notes)}" if notes else "") + "."
            )
            self._say(f"agentcap: protection at the end: {protection_text(prot, enforce=self.enforce)}")
        last = self._last_tool()
        if last:
            self._say(f"agentcap: last tool call: {last}")
        if not self.enforce and s.observed and s.share >= s.quota:
            self._say(
                f"agentcap: Pro's quota mode would have stopped this job at {s.quota:g}%. "
                "https://agentcap.launchbench.studio/#pricing"
            )
        if self.enforce:
            self._event("quota_receipt", s.share, reason=s.reason, exit_code=s.exit_code)
            if self.receipt_dir is not None:
                try:
                    _atomic_write(self.receipt_dir / f"{self.job_id}.json", {
                        "type": "agentcap_quota_receipt", "agent": s.agent, "window": s.window,
                        "quota_pct": s.quota,
                        "share_pct": round(s.share, 3) if s.observed else None,
                        "exit_code": s.exit_code,
                        "reason": s.reason, "baseline_exact": s.baseline_exact,
                        "parallel_sessions": s.parallel, "window_resets": s.resets_seen,
                        "last_tool": last, "measure": "account_increase" if s.observed else "none",
                        "baseline_age_s": (round(self.baseline_age_s) if self.baseline_age_s is not None
                                           else None),
                        "final_read": s.final_read, "effort": s.effort,
                        "response_timeout_s": self.response_timeout_s if s.agent == "codex" else None,
                        "observed": s.observed,
                        "state": prot.state, "state_text": prot.text,
                        "read_at": prot.read_at, "reading_age_s": prot.age_s,
                        "max_age_s": prot.max_age_s, "source": prot.source,
                        "resolution_pct": prot.resolution or READING_RESOLUTION_PCT,
                        "uncertainty_pct": {"low": READING_UNCERTAINTY_PCT[0],
                                            "high": READING_UNCERTAINTY_PCT[1]},
                        "protection": prot.to_dict(),
                    })
                except OSError:
                    pass


# ------------------------------------------------------------ read-only view
def quota_status(*, now: float | None = None, sessions_dir: Path | None = None) -> list[str]:
    now = time.time() if now is None else now
    lines: list[str] = []
    last = _read_json(state_root() / "claude_last.json")
    if last and isinstance(last.get("readings"), Mapping):
        ts = _num(last.get("ts")) or now
        for window, block in sorted(last["readings"].items()):
            reading = Reading.from_dict(block) if isinstance(block, Mapping) else None
            if reading is None:
                continue
            if reading.read_at <= 0:
                reading = Reading(
                    reading.percent, reading.resets_at, source="claude", read_at=ts,
                    max_age=READING_MAX_AGE_S["claude"], window=window,
                    resolution=reading.resolution,
                )
            prot = protection_state(started_at=ts, now=now, reading=reading,
                                    source="claude", window=window)
            lines.append(f"claude {window}: {reading.percent:.1f}% used {uncertainty_text()}"
                         + _reset_text(reading.resets_at, now)
                         + f" (seen {int(prot.age_s or 0)}s ago) — {prot.text}")
    else:
        lines.append("claude: no data yet (seen only during `agentcap run --quota ... -- claude`)")
    tail = CodexTailer(sessions_dir or (codex_home() / "sessions"), now)
    found = False
    for window in WINDOWS:
        prior = tail.last_reading_before(window)
        if prior:
            found = True
            reading = prior[1]
            prot = protection_state(started_at=prior[0], now=now, reading=reading,
                                    source="codex", window=window)
            lines.append(f"codex {window}: {reading.percent:.1f}% used {uncertainty_text()}"
                         + _reset_text(reading.resets_at, now)
                         + f" (seen {int(prot.age_s or 0)}s ago) — {prot.text}")
    if not found:
        lines.append("codex: no rate_limits found in ~/.codex/sessions")
    return lines


def _reset_text(resets_at: Any, now: float) -> str:
    value = _num(resets_at)
    if value is None:
        return ""
    left = max(0, int(value - now))
    return f", resets in {left // 3600}h{(left % 3600) // 60:02d}m"


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) >= 3 and args[1] == "--job":
        job = Path(args[2])
        if args[0] == "statusline":
            try:
                return statusline_main(job, sys.stdin, sys.stdout)
            except Exception:
                return 0
        if args[0] == "hook":
            try:
                return hook_main(job, sys.stdin, sys.stdout)
            except Exception:
                return 0
        if args[0] == "post":
            try:
                return post_main(job, sys.stdin)
            except Exception:
                return 0
    print("usage: python -m agentcap.quota statusline|hook --job DIR", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
