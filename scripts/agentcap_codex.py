#!/usr/bin/env python3
"""AgentCap Codex plugin: a per-session share of the ChatGPT plan quota, inside Codex.

Stdlib only.  Reuses ``agentcap`` modules from ``<plugin root>/src``, so the plugin
needs no pip install.  Read-only by design: the quota comes from Codex's own
``codex app-server`` (``account/rateLimits/read``) and from the rollout file Codex
writes; no token, login file or model traffic is touched.

Everything happens inside Codex (TUI or ``codex exec``):

  agentcap 10% 5h          this session may use 10% of the 5-hour quota (``week`` too)
  agentcap status          share, limit, where the numbers come from
  agentcap off             clear this session's limit
  agentcap approve         +50% after a stop (Pro, at most twice)
  agentcap default 10% 5h  every new session (also ``codex exec``) starts with this limit
  agentcap default off     no default

Those prompts are handled by the ``UserPromptSubmit`` hook and never reach the model
(no quota spent; the model cannot type a user prompt, so ``approve`` is the user's).
``PreToolUse`` denies tool calls at the limit and on the loop / error breakers;
Codex 0.167 cannot end a turn from a hook (``continue: false`` is rejected there),
so the model is told why every call is denied and normally stops.

Subcommands: ``hook <SessionStart|UserPromptSubmit|PreToolUse|PostToolUse|Stop>``
(JSON on stdin) and ``probe --session ID`` (background app-server read).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable, Mapping, TextIO

ROOT = Path(__file__).resolve().parent.parent
# Codex fingerprints the installed plugin and refuses every hook once its contents
# change ("plugin contents changed; hook approval required"); a __pycache__ written
# into the plugin by the first hook would switch AgentCap off for the rest of the
# session.  Never write bytecode (the hook command also passes -B).
sys.dont_write_bytecode = True
sys.path.insert(0, str(ROOT / "src"))

from agentcap.live_quota import codex_live  # noqa: E402
from agentcap.quota import (  # noqa: E402
    Reading,
    ShareTracker,
    _atomic_write,
    _read_json,
    codex_readings,
    is_self_approve_command,
    parse_quota,
)
from agentcap.quota import state_root as _quota_state_root  # noqa: E402
from agentcap.waste import _codex_error, _digest, command_text, is_wait_call  # noqa: E402

WINDOW_NAMES = {"week": "weekly", "5h": "5-hour"}
WARN_FRACTION = 0.8
LOOP_REPEATS = 4
LOOP_ERRORS = 8
APPROVE_FACTOR, APPROVE_MAX = 1.5, 2
LIVE_POLL_S = 30.0       # background `codex app-server` read while a limit is set
PROBE_STALE_S = 60.0
RESET_MATCH_S = 120.0
ROLLOUT_TAIL_BYTES = 256 * 1024
PRICING_URL = "https://agentcap.launchbench.studio/#pricing"
CMD_RE = re.compile(r"^\s*/?agentcap\b(.*)$", re.IGNORECASE | re.DOTALL)

# Reader of the live account readings and the detached-process starter; tests replace them.
LIVE_READER: Callable[[], Mapping[str, Reading] | None] = codex_live
SPAWN: Callable[..., Any] = subprocess.Popen


# ----------------------------------------------------------------- paths
def _testing() -> bool:
    """Environment overrides are a test facility (a project's settings could set them)."""

    return os.environ.get("AGENTCAP_TESTING") == "1"


def state_dir() -> Path:
    base = _quota_state_root() if _testing() else Path.home() / ".local" / "state" / "agentcap" / "quota"
    return base / "codex"


def _safe(sid: str) -> str:
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in sid)[:120] or "unknown"


def session_path(sid: str) -> Path:
    return state_dir() / "sessions" / f"{_safe(sid)}.json"


def live_path(sid: str) -> Path:
    return state_dir() / "live" / f"{_safe(sid)}.json"


def defaults_path() -> Path:
    return state_dir() / "defaults.json"


def license_path() -> Path:
    env = os.environ.get("AGENTCAP_LICENSE_FILE")
    if _testing() and env:
        return Path(env).expanduser()
    return Path.home() / ".config" / "agentcap" / "license.json"


def is_pro() -> bool:
    data = _read_json(license_path())
    return bool(data and data.get("source") == "polar" and data.get("valid") is True
                and data.get("tier") == "pro" and isinstance(data.get("key"), str) and data.get("key"))


def load(sid: str) -> dict[str, Any]:
    return _read_json(session_path(sid)) or {"session_id": sid}


def save(sid: str, state: Mapping[str, Any]) -> None:
    _atomic_write(session_path(sid), state)


# ----------------------------------------------------------------- readings
def _readings_dict(readings: Mapping[str, Reading]) -> dict[str, Any]:
    return {w: {"used": r.used, "resets_at": r.resets_at} for w, r in readings.items()}


def _reading(block: Any) -> Reading | None:
    if not isinstance(block, Mapping) or block.get("used") is None:
        return None
    try:
        return Reading(float(block["used"]), float(block["resets_at"]) if block.get("resets_at") is not None else None)
    except (TypeError, ValueError):
        return None


def rollout_reading(path: Any, window: str) -> Reading | None:
    """Last rate_limits reading in the session's rollout (lags one response)."""

    if not isinstance(path, str) or not path:
        return None
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - ROLLOUT_TAIL_BYTES))
            lines = fh.read().decode("utf-8", "replace").splitlines()
    except OSError:
        return None
    for line in reversed(lines):
        if '"token_count"' not in line or '"rate_limits"' not in line:
            continue
        try:
            got = codex_readings(json.loads(line))
        except ValueError:
            continue
        if window in got:
            return got[window]
    return None


def read_live(sid: str, now: float) -> dict[str, Reading] | None:
    """Synchronous app-server read; stored for the hooks of this session."""

    try:
        got = LIVE_READER()
    except Exception:
        got = None
    if got:
        _atomic_write(live_path(sid), {"ts": now, "readings": _readings_dict(got)})
    return dict(got) if got else None


def maybe_probe(sid: str, now: float) -> bool:
    """Start a detached app-server read when the last one is LIVE_POLL_S old."""

    last = _read_json(live_path(sid)) or {}
    if now - float(last.get("ts") or 0) < LIVE_POLL_S:
        return False
    lock = live_path(sid).with_suffix(".probe")
    if now - float((_read_json(lock) or {}).get("ts") or 0) < PROBE_STALE_S:
        return False
    _atomic_write(lock, {"ts": now})
    try:
        SPAWN(
            [sys.executable, "-B", "-E", "-P", str(Path(__file__).resolve()), "probe", "--session", sid],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True, close_fds=True,
        )
    except OSError:
        return False
    return True


def cmd_probe(sid: str) -> int:
    lock = live_path(sid).with_suffix(".probe")
    try:
        read_live(sid, time.time())
    finally:
        try:
            lock.unlink()
        except OSError:
            pass
    return 0


def refresh(state: dict[str, Any], sid: str, payload: Mapping[str, Any], now: float) -> None:
    """Feed the newest readings (rollout + last live read) into the tracker."""

    window = state.get("window", "week")
    tracker = ShareTracker.from_dict(state.get("tracker"))
    if tracker.baseline is None:
        return
    candidates: list[Reading] = []
    got = rollout_reading(payload.get("transcript_path"), window)
    if got is not None:
        candidates.append(got)
    live = _read_json(live_path(sid)) or {}
    if float(live.get("ts") or 0) >= float(state.get("set_at") or 0):
        got = _reading((live.get("readings") or {}).get(window))
        if got is not None:
            candidates.append(got)
    for reading in candidates:
        if (reading.resets_at is not None and tracker.last_reset is not None
                and abs(reading.resets_at - tracker.last_reset) <= RESET_MATCH_S):
            reading = Reading(reading.used, tracker.last_reset)  # minute-rounded vs exact
        tracker.update(reading)
    state["tracker"] = tracker.to_dict()
    state["share"] = tracker.share
    state["checked_at"] = now


def start_quota(state: dict[str, Any], sid: str, quota: float, window: str, now: float,
                *, source: str) -> str:
    for key in ("tracker", "share", "warned", "nudged", "denies", "approvals", "receipt_share"):
        state.pop(key, None)
    state.update({"quota": quota, "window": window, "set_at": now, "source": source})
    live = read_live(sid, now)
    reading = (live or {}).get(window)
    if reading is None:
        state["tracker"] = ShareTracker().to_dict()
        return "baseline: not available yet (Codex did not answer); it is taken from the next reading"
    tracker = ShareTracker()
    tracker.seed(reading)
    state["tracker"] = tracker.to_dict()
    state["share"] = 0.0
    return f"baseline {reading.used:g}% (from codex app-server)"


def ensure_baseline(state: dict[str, Any], sid: str, payload: Mapping[str, Any]) -> None:
    """A limit set when Codex did not answer takes its baseline from the next reading."""

    tracker = ShareTracker.from_dict(state.get("tracker"))
    if state.get("quota") is None or tracker.baseline is not None:
        return
    window = state.get("window", "week")
    live = _read_json(live_path(sid)) or {}
    got = None
    if float(live.get("ts") or 0) >= float(state.get("set_at") or 0):
        got = _reading((live.get("readings") or {}).get(window))
    if got is None:
        got = rollout_reading(payload.get("transcript_path"), window)
    if got is not None:
        tracker.seed(got)
        state["tracker"] = tracker.to_dict()
        state["share"] = 0.0


def share_line(state: Mapping[str, Any]) -> str:
    quota = state.get("quota")
    if quota is None:
        return "agentcap: no limit for this session (type `agentcap 10% 5h`)"
    window = WINDOW_NAMES.get(state.get("window", "week"), "weekly")
    if ShareTracker.from_dict(state.get("tracker")).baseline is None:
        return f"agentcap: limit {float(quota):g}% of the {window} quota, waiting for the first reading"
    return f"agentcap: session {float(state.get('share') or 0):.1f}% / {float(quota):g}% ({window})"


# ----------------------------------------------------------------- commands (UserPromptSubmit)
HELP = (
    "agentcap commands (typed as a prompt in Codex; the model never sees them):\n"
    "  agentcap 10% 5h          this session may use 10% of the 5-hour quota (or `week`)\n"
    "  agentcap status          current share\n"
    "  agentcap off             clear this session's limit\n"
    "  agentcap approve         raise the limit by 50% after a stop (Pro, at most twice)\n"
    "  agentcap default 10% 5h  every new session, also `codex exec`, starts with this limit\n"
    "  agentcap default off     no default"
)


def _parse_limit(words: list[str]) -> tuple[float, str]:
    if not words:
        raise ValueError("give a limit, e.g. `agentcap 10% 5h`")
    quota = parse_quota(words[0])
    window = "week"
    for w in words[1:]:
        lw = w.lower()
        if lw in ("5h", "5-hour", "5hour", "session"):
            window = "5h"
        elif lw in ("week", "weekly", "7d"):
            window = "week"
        else:
            raise ValueError(f"unknown window {w!r}: use 5h or week")
    return quota, window


def run_command(text: str, sid: str, now: float) -> str:
    words = text.split()
    verb = words[0].lower() if words else "status"
    state = load(sid)
    if verb in ("help", "-h", "--help", "?"):
        return HELP
    if verb == "status":
        if state.get("quota") is not None:
            maybe_probe(sid, now)
        tier = "Pro (stops at the limit)" if is_pro() else "Free (watch only; Pro stops at the limit)"
        default = _read_json(defaults_path())
        extra = (f"; default for new sessions: {default['quota']:g}% {default['window']}"
                 if default and default.get("quota") is not None else "")
        loop = state.get("loop") or {}
        paused = f"; PAUSED: {loop['tripped']} (type `agentcap approve`)" if loop.get("tripped") else ""
        return f"{share_line(state)} — {tier}{extra}{paused}"
    if verb == "off":
        for key in ("quota", "window", "tracker", "share", "warned", "nudged", "set_at", "source"):
            state.pop(key, None)
        save(sid, state)
        return "agentcap: limit cleared for this session."
    if verb == "approve":
        if not is_pro():
            return f"agentcap: approve is Pro (Free never stops, so there is nothing to approve). {PRICING_URL}"
        loop = state.get("loop") or {}
        cleared = bool(loop.get("tripped"))
        state["loop"] = {"count": 0, "errors": 0}
        if state.get("quota") is None:
            save(sid, state)
            return "agentcap: breaker cleared; no limit set for this session." if cleared else \
                "agentcap: no limit set for this session."
        n = int(state.get("approvals") or 0)
        if n >= APPROVE_MAX:
            save(sid, state)
            return (f"agentcap: already approved twice (limit {float(state['quota']):g}%). "
                    "Use `agentcap <N%>` to set a new limit on purpose.")
        new = round(float(state["quota"]) * APPROVE_FACTOR, 4)
        state.update({"quota": new, "approvals": n + 1, "nudged": False, "warned": False})
        save(sid, state)
        share = float(state.get("share") or 0)
        note = (f" The session is already at {share:.1f}%, so tools stay denied; approve again or set a "
                "higher limit." if share >= new else " Tell Codex to continue.")
        return (f"agentcap: approved ({n + 1}/{APPROVE_MAX}): limit raised to {new:g}%"
                + ("; breaker cleared" if cleared else "") + "." + note)
    if verb == "default":
        rest = words[1:]
        if not rest:
            d = _read_json(defaults_path())
            return (f"agentcap: default for new sessions: {d['quota']:g}% of the "
                    f"{WINDOW_NAMES[d['window']]} quota." if d and d.get("quota") is not None
                    else "agentcap: no default limit. Set one with `agentcap default 10% 5h`.")
        if rest[0].lower() in ("off", "none", "clear"):
            try:
                defaults_path().unlink()
            except OSError:
                pass
            return "agentcap: default limit removed; new sessions start without one."
        quota, window = _parse_limit(rest)
        _atomic_write(defaults_path(), {"quota": quota, "window": window, "set_at": now})
        return (f"agentcap: every new Codex session (TUI and `codex exec`) now starts with a "
                f"{quota:g}% limit of the {WINDOW_NAMES[window]} quota. This session is unchanged; "
                f"`agentcap {quota:g}% {window}` sets it here too.")
    if verb in ("quota", "set", "limit"):
        words = words[1:]
    quota, window = _parse_limit(words)
    detail = start_quota(state, sid, quota, window, now, source="command")
    save(sid, state)
    tier = "" if is_pro() else (" Free: you get the meter and one notice at the limit; stopping is Pro "
                                f"({PRICING_URL}).")
    return (f"agentcap: this session may use {quota:g}% of the {WINDOW_NAMES[window]} quota; "
            f"{detail}. Tool calls are denied at the limit.{tier}")


# ----------------------------------------------------------------- self-guard
_STATE_RE = re.compile(r"state/agentcap\b|\.codex/plugins\S*agentcap|agentcap_codex\b")
_READ_HEADS = frozenset({"cat", "head", "tail", "less", "grep", "rg", "ls", "wc", "stat", "jq", "diff", "file"})


def self_guard(payload: Mapping[str, Any]) -> str | None:
    cmd = command_text(payload.get("tool_input"))
    if not cmd:
        return None
    if is_self_approve_command(cmd):
        return ("agentcap: the agent may not approve its own limit; the user types "
                "`agentcap approve` as a prompt.")
    if _STATE_RE.search(cmd):
        segments = [s.strip() for s in re.split(r"\|\||&&|[;|]", cmd) if s.strip()]
        if any(re.search(r">|\$\(|`", s) or s.split()[0] not in _READ_HEADS for s in segments):
            return ("agentcap: the agent may not change AgentCap's state or plugin files; "
                    "the limit belongs to the user.")
    return None


# ----------------------------------------------------------------- hooks
def _deny(reason: str) -> dict[str, Any]:
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                   "permissionDecisionReason": reason}}


def hook_session_start(payload: Mapping[str, Any], *, now: float) -> dict[str, Any] | None:
    sid = payload.get("session_id")
    if not isinstance(sid, str) or not sid:
        return None
    state = load(sid)
    if state.get("quota") is not None:
        return None  # resumed session keeps its own limit
    d = _read_json(defaults_path())
    if not d or d.get("quota") is None or d.get("window") not in WINDOW_NAMES:
        return None
    detail = start_quota(state, sid, float(d["quota"]), d["window"], now, source="default")
    save(sid, state)
    return {"systemMessage": (f"agentcap: default limit {float(d['quota']):g}% of the "
                              f"{WINDOW_NAMES[d['window']]} quota for this session; {detail}.")}


def hook_prompt(payload: Mapping[str, Any], *, now: float) -> dict[str, Any] | None:
    sid = payload.get("session_id")
    prompt = payload.get("prompt")
    if not isinstance(sid, str) or not sid or not isinstance(prompt, str):
        return None
    m = CMD_RE.match(prompt)
    if not m or "\n" in prompt.strip():
        return None
    try:
        text = run_command(m.group(1).strip(), sid, now)
    except ValueError as exc:
        text = f"agentcap: {exc}. {HELP}"
    return {"decision": "block", "reason": text}


def hook_pre(payload: Mapping[str, Any], *, now: float, pro: bool) -> dict[str, Any] | None:
    sid = payload.get("session_id")
    name = payload.get("tool_name")
    if not isinstance(sid, str) or not sid or not isinstance(name, str):
        return None
    guard = self_guard(payload)
    if guard:
        return _deny(guard)
    state = load(sid)
    loop = state.setdefault("loop", {})
    changed = False
    if loop.get("tripped"):
        if pro:
            save(sid, state)
            return _deny(f"agentcap: {loop['tripped']}. Stop now: do not call more tools; summarise "
                         "what is done and what is left. The user can type `agentcap approve`.")
    elif not is_wait_call(name, payload.get("tool_input")):
        d = _digest(name, payload.get("tool_input"))
        if loop.get("digest") != d:
            loop["nudged"] = False
        loop["count"] = int(loop.get("count", 0)) + 1 if loop.get("digest") == d else 1
        loop["digest"], loop["tool"] = d, name[:40]
        changed = True
        if loop["count"] >= LOOP_REPEATS:
            detail = f"same tool call {loop['count']} times in a row ({name[:40]})"
            if pro:
                loop["tripped"] = f"loop breaker: {detail}"
                save(sid, state)
                return _deny(f"agentcap {loop['tripped']}. Stop now: do not call more tools; "
                             "summarise what is done and what is left. The user can type `agentcap approve`.")
            if not loop.get("nudged"):
                loop["nudged"] = True
                save(sid, state)
                return {"systemMessage": f"agentcap: {detail}. AgentCap Pro would stop the agent here: "
                                         f"{PRICING_URL}"}
    if state.get("quota") is None:
        if changed:
            save(sid, state)
        return None
    ensure_baseline(state, sid, payload)
    maybe_probe(sid, now)
    refresh(state, sid, payload, now)
    quota = float(state["quota"])
    share = float(state.get("share") or 0)
    window = WINDOW_NAMES.get(state.get("window", "week"), "weekly")
    out: dict[str, Any] | None = None
    if ShareTracker.from_dict(state.get("tracker")).baseline is not None and share >= quota:
        if pro:
            state["denies"] = int(state.get("denies", 0)) + 1
            out = _deny(f"agentcap: this session used {share:.1f}% of the {window} quota (limit {quota:g}%). "
                        "Stop now: do not call more tools; summarise what is done and what is left. "
                        "The user can type `agentcap approve` (+50%) or `agentcap off`.")
        elif not state.get("nudged"):
            state["nudged"] = True
            out = {"systemMessage": f"agentcap: this session passed its {quota:g}% {window} limit "
                                    f"({share:.1f}%). AgentCap Pro would stop the agent here: {PRICING_URL}"}
    elif share >= quota * WARN_FRACTION and not state.get("warned"):
        state["warned"] = True
        out = {"systemMessage": f"agentcap: session at {share:.1f}% of its {quota:g}% {window} limit."}
    save(sid, state)
    return out


def hook_post(payload: Mapping[str, Any], *, now: float) -> dict[str, Any] | None:
    sid = payload.get("session_id")
    name = payload.get("tool_name")
    if not isinstance(sid, str) or not sid or not isinstance(name, str):
        return None
    if is_wait_call(name, payload.get("tool_input")):
        return None
    state = load(sid)
    loop = state.setdefault("loop", {})
    failed = tool_failed(payload.get("tool_response"))
    loop["errors"] = int(loop.get("errors", 0)) + 1 if failed else 0
    out: dict[str, Any] | None = None
    if loop["errors"] >= LOOP_ERRORS and not loop.get("tripped"):
        detail = f"{loop['errors']} failing tool calls in a row (last: {name[:40]})"
        if is_pro():
            loop["tripped"] = f"error breaker: {detail}"
        elif not loop.get("err_nudged"):
            loop["err_nudged"] = True
            out = {"systemMessage": f"agentcap: {detail}. AgentCap Pro would stop the agent here: {PRICING_URL}"}
    if not failed:
        loop["err_nudged"] = False
    save(sid, state)
    return out


def tool_failed(resp: Any) -> bool:
    """Failure of one Codex tool result (shell, apply_patch, MCP)."""

    if isinstance(resp, Mapping):
        if resp.get("isError") is True or resp.get("is_error") is True or resp.get("success") is False:
            return True
        for key in ("exit_code", "exitCode"):
            code = resp.get(key)
            if isinstance(code, int) and not isinstance(code, bool):
                return code != 0
        for key in ("output", "content", "stdout"):
            if key in resp:
                return _codex_error(resp[key])
        return False
    return _codex_error(resp)


def hook_stop(payload: Mapping[str, Any], *, now: float) -> dict[str, Any] | None:
    sid = payload.get("session_id")
    if not isinstance(sid, str) or not sid:
        return None
    state = load(sid)
    if state.get("quota") is None:
        return None
    ensure_baseline(state, sid, payload)
    if ShareTracker.from_dict(state.get("tracker")).baseline is not None:
        read_live(sid, now)  # the turn's last response is now on the server
        refresh(state, sid, payload, now)
    share = round(float(state.get("share") or 0), 1)
    line = share_line(state)
    if state.get("receipt_share") != share:
        state["receipt_share"] = share
        try:
            rec = {"ts": now, "agent": "codex", "session_id": sid, "window": state.get("window"),
                   "quota": state.get("quota"), "share": share, "denies": state.get("denies", 0),
                   "approvals": state.get("approvals", 0), "source": state.get("source"),
                   "measured_by": "app-server"}
            path = state_dir() / "receipts.jsonl"
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec) + "\n")
        except OSError:
            pass
    save(sid, state)
    return {"systemMessage": line}


# ----------------------------------------------------------------- entry
def _emit(stdout: TextIO, obj: Mapping[str, Any] | None) -> None:
    if obj:
        stdout.write(json.dumps(obj) + "\n")
        stdout.flush()


def main(argv: list[str] | None = None, stdin: TextIO = sys.stdin, stdout: TextIO = sys.stdout) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    now = time.time()
    if len(argv) >= 3 and argv[0] == "probe" and argv[1] == "--session":
        return cmd_probe(argv[2])
    if len(argv) < 2 or argv[0] != "hook":
        stdout.write(__doc__ or "")
        return 0
    try:
        raw = stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
        if not isinstance(payload, dict):
            return 0
        event = argv[1]
        if event == "SessionStart":
            _emit(stdout, hook_session_start(payload, now=now))
        elif event == "UserPromptSubmit":
            _emit(stdout, hook_prompt(payload, now=now))
        elif event == "PreToolUse":
            _emit(stdout, hook_pre(payload, now=now, pro=is_pro()))
        elif event == "PostToolUse":
            _emit(stdout, hook_post(payload, now=now))
        elif event == "Stop":
            _emit(stdout, hook_stop(payload, now=now))
    except Exception as exc:  # hooks fail open, loudly
        sys.stderr.write(f"agentcap: {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
