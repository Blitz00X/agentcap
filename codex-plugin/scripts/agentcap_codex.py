#!/usr/bin/env python3
"""AgentCap Codex plugin: a per-session share of the ChatGPT plan quota, inside Codex.

Stdlib only.  Reuses ``agentcap`` modules from ``<plugin root>/src``, so the plugin
needs no pip install.  Read-only by design: the quota comes from Codex's own
``codex app-server`` (``account/rateLimits/read``) and from the rollout file Codex
writes; no token, login file or model traffic is touched.

Everything happens inside Codex (TUI or ``codex exec``):

  agentcap 10% 5h          this session may use 10 more points of the 5-hour quota (``week`` too)
  agentcap until 70% 5h    stop when the 5-hour quota reaches 70%
  agentcap status          share, limit, where the numbers come from
  agentcap off             clear this session's limit
  agentcap approve         +50% after a stop (Pro, at most twice)
  agentcap default 10% 5h  every new session (also ``codex exec``) starts with this limit
  agentcap default off     no default

Those prompts are handled by the ``UserPromptSubmit`` hook and never reach the model
(no quota spent; the model cannot type a user prompt, so ``approve`` is the user's).
Any other prompt is blocked while a Pro session is at/over its stop point, so a
limit reached in ``PreToolUse`` cannot be bypassed with a new prompt.
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
    agent_set_refusal,
    approve_quota,
    is_self_approve_command,
    parse_agent_set,
    parse_limit_words,
    resolve_until,
    stop_point,
    stop_text,
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
                *, source: str, until: bool = False, seed: Reading | None = None, live: bool = True) -> str:
    """Set the limit and return the sentence that says where this session stops.

    ``live=False`` (inside a short hook) uses ``seed`` instead of an app-server read."""

    for key in ("tracker", "share", "warned", "nudged", "denies", "approvals", "receipt_share",
                "until", "until_done"):
        state.pop(key, None)
    state.update({"quota": quota, "window": window, "set_at": now, "source": source})
    if until:
        state["until"] = quota
    reading = (read_live(sid, now) or {}).get(window) if live else seed
    if reading is None:
        state["tracker"] = ShareTracker().to_dict()
    else:
        tracker = ShareTracker()
        tracker.seed(reading)
        state["tracker"] = tracker.to_dict()
        state["share"] = 0.0
        resolve_until(state)
    return limit_sentence(state)


def limit_sentence(state: Mapping[str, Any]) -> str:
    window = WINDOW_NAMES.get(state.get("window", "week"), "weekly")
    tracker = ShareTracker.from_dict(state.get("tracker"))
    quota = float(state.get("quota") or 0)
    until = state.get("until")
    if tracker.baseline is None:
        what = (f"this session stops when the {window} quota reaches {float(until):g}%" if until is not None
                else f"this session may use {quota:g} more points of the {window} quota")
        return f"{what}; Codex has not reported the current % yet, the stop point is shown at the next reading"
    if until is not None and quota <= 0:
        return (f"the {window} quota is already at {float(tracker.baseline):g}%, at or above your target "
                f"{float(until):g}%, so tool calls are denied now; set a higher target or `agentcap off`")
    head = (f"this session stops at {float(until):g}% of the {window} quota" if until is not None
            else f"this session may use {quota:g} more points of the {window} quota")
    return f"{head}: {stop_text(quota, float(state.get('share') or 0), tracker.last_used, window)}"


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
        resolve_until(state)


def latest_reading(sid: str, payload: Mapping[str, Any], window: str, now: float) -> Reading | None:
    """Newest reading without a network call: a live read under a minute old, else the rollout."""

    live = _read_json(live_path(sid)) or {}
    if now - float(live.get("ts") or 0) <= 60:
        got = _reading((live.get("readings") or {}).get(window))
        if got is not None:
            return got
    return rollout_reading(payload.get("transcript_path"), window)


def share_line(state: Mapping[str, Any]) -> str:
    quota = state.get("quota")
    if quota is None:
        return "agentcap: no limit for this session (type `agentcap 10% 5h`)"
    window = WINDOW_NAMES.get(state.get("window", "week"), "weekly")
    tracker = ShareTracker.from_dict(state.get("tracker"))
    if tracker.baseline is None:
        return f"agentcap: {limit_sentence(state)}"
    share = float(state.get("share") or 0)
    return (f"agentcap: {stop_text(float(quota), share, tracker.last_used, window)} "
            f"(this session used {share:.1f} of {float(quota):g} points)")


# ----------------------------------------------------------------- commands (UserPromptSubmit)
HELP = (
    "agentcap commands (typed as a prompt in Codex; the model never sees them):\n"
    "  agentcap 10% 5h          this session may use 10 more points of the 5-hour quota (or `week`)\n"
    "  agentcap until 70% 5h    stop when the 5-hour quota reaches 70%\n"
    "  agentcap status          where the quota is now and where this session stops\n"
    "  agentcap off             clear this session's limit\n"
    "  agentcap approve         raise the limit by 50% after a stop (Pro, at most twice)\n"
    "  agentcap default 10% 5h  every new session, also `codex exec`, starts with this limit (`until` too)\n"
    "  agentcap default off     no default\n"
    "Or ask the agent in plain words (\"use at most half of the 5-hour quota\"): it proposes the\n"
    "limit and you approve it with one button. The agent can only set or tighten a limit."
)


def _parse_limit(words: list[str]) -> tuple[float, str, bool]:
    if not words:
        raise ValueError("give a limit, e.g. `agentcap 10% 5h` or `agentcap until 70% 5h`")
    return parse_limit_words(words)


def _default_text(d: Mapping[str, Any]) -> str:
    window = WINDOW_NAMES.get(d.get("window", "week"), "weekly")
    if d.get("until"):
        return f"a stop at {float(d['quota']):g}% of the {window} quota"
    return f"a limit of {float(d['quota']):g} more points of the {window} quota"


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
        extra = (f"; new sessions start with {_default_text(default)}"
                 if default and default.get("quota") is not None else "")
        loop = state.get("loop") or {}
        paused = f"; PAUSED: {loop['tripped']} (type `agentcap approve`)" if loop.get("tripped") else ""
        return f"{share_line(state)} — {tier}{extra}{paused}"
    if verb == "off":
        for key in ("quota", "window", "tracker", "share", "warned", "nudged", "set_at", "source",
                    "until", "until_done"):
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
                    "Set a new limit with `agentcap <N%>`, or remove it with `agentcap off`.")
        new = approve_quota(float(state["quota"]), APPROVE_FACTOR)
        state.update({"quota": new, "approvals": n + 1, "nudged": False, "warned": False})
        state.pop("until", None)  # from here on the limit is points; the stop point is shown below
        save(sid, state)
        share = float(state.get("share") or 0)
        tracker = ShareTracker.from_dict(state.get("tracker"))
        window = WINDOW_NAMES.get(state.get("window", "week"), "weekly")
        where = f" {stop_text(new, share, tracker.last_used, window)}." if tracker.baseline is not None else ""
        note = (f" The session already used {share:.1f} points, so tools stay denied; approve again or set "
                "a higher limit." if share >= new else " Tell Codex to continue.")
        return (f"agentcap: approved ({n + 1}/{APPROVE_MAX}): limit raised to {new:g} points"
                + ("; breaker cleared" if cleared else "") + "." + where + note)
    if verb == "default":
        rest = words[1:]
        if not rest:
            d = _read_json(defaults_path())
            return (f"agentcap: new sessions start with {_default_text(d)}." if d and d.get("quota") is not None
                    else "agentcap: no default limit. Set one with `agentcap default 10% 5h`.")
        if rest[0].lower() in ("off", "none", "clear"):
            try:
                defaults_path().unlink()
            except OSError:
                pass
            return "agentcap: default limit removed; new sessions start without one."
        quota, window, until = _parse_limit(rest)
        d = {"quota": quota, "window": window, "until": until, "set_at": now}
        _atomic_write(defaults_path(), d)
        here = f"agentcap {'until ' if until else ''}{quota:g}% {window}"
        return (f"agentcap: every new Codex session (TUI and `codex exec`) now starts with {_default_text(d)}. "
                f"This session is unchanged; `{here}` sets it here too.")
    if verb in ("quota", "set", "limit"):
        words = words[1:]
    quota, window, until = _parse_limit(words)
    detail = start_quota(state, sid, quota, window, now, source="command", until=until)
    save(sid, state)
    tier = "" if is_pro() else (" Free: you get the meter and one notice at the limit; stopping is Pro "
                                f"({PRICING_URL}).")
    return f"agentcap: {detail}. Tool calls are denied at the limit.{tier}"


# ----------------------------------------------------------------- agent-set limit
# The user asks in plain words ("use at most half of the 5-hour quota"); the agent runs
# `echo agentcap set 50% 5h` with sandbox_permissions "require_escalated" (the skill says so),
# Codex shows its approval prompt (the user's one button), and PostToolUse sets the limit after
# the command ran (a declined command never runs).
# The agent may only set or tighten a limit: raising, clearing and approving stay the user's.
AGENT_SET_HELP = ("the command is `echo agentcap set 50% 5h` (50 more points of the 5-hour quota) or "
                  "`echo agentcap set until 70% 5h` (stop when the 5-hour quota reaches 70%)")


def _agent_set(payload: Mapping[str, Any]) -> tuple[float, str, bool] | None:
    """The agent-set command in this tool call (ValueError: it is one, with bad arguments)."""

    if payload.get("tool_name") not in _SHELL_TOOLS:
        return None
    return parse_agent_set(command_text(payload.get("tool_input")))


def _current(state: dict[str, Any], sid: str, payload: Mapping[str, Any], now: float) -> tuple[float | None, float]:
    """(window % now, share) of the limit in force, after the newest readings."""

    if state.get("quota") is None:
        return None, 0.0
    ensure_baseline(state, sid, payload)
    refresh(state, sid, payload, now)
    resolve_until(state)
    tracker = ShareTracker.from_dict(state.get("tracker"))
    used = tracker.last_used if tracker.baseline is not None else None
    return used, float(state.get("share") or 0)


def preview_text(value: float, window: str, until: bool, reading: Reading | None) -> str:
    """Where the proposed limit would stop, as the user sees it in the approval prompt."""

    name = WINDOW_NAMES.get(window, window)
    what = (f"stop this session when the {name} quota reaches {value:g}%" if until
            else f"let this session use {value:g} more points of the {name} quota")
    if reading is None:
        return what
    if until and value <= reading.used:
        return f"{what} (it is at {reading.used:g}% already, so tools would stop right away)"
    quota = max(value - reading.used, 0.0) if until else value
    return f"{what}: {stop_text(quota, 0.0, reading.used, name)}"


def agent_set_pre(payload: Mapping[str, Any], sid: str, now: float) -> tuple[bool, dict[str, Any] | None]:
    """(is the agent-set command, deny output or None to let Codex ask the user)."""

    try:
        got = _agent_set(payload)
    except ValueError as exc:
        return True, _deny(f"agentcap: {exc}; {AGENT_SET_HELP}.")
    if got is None:
        return False, None
    value, window, until = got
    state = load(sid)
    used, share = _current(state, sid, payload, now)
    why = agent_set_refusal(state, used, share, value, window, until)
    if why:
        return True, _deny(f"agentcap guard (not a quota stop): {why}. Tell the user they can type "
                           f"`agentcap {'until ' if until else ''}{value:g}% {window}` themselves.")
    # Codex hands hooks only {"command": ...}, so the escalation the skill asks for (which makes
    # Codex show its approval prompt) cannot be checked here; a call without it only sets a
    # tighter limit, and the user sees it in the message after it ran.
    return True, None


def agent_set_post(payload: Mapping[str, Any], sid: str, now: float, failed: bool) -> Any:
    """Set the limit once the approved command ran.  False: not the agent-set command."""

    try:
        got = _agent_set(payload)
    except ValueError:
        return None
    if got is None:
        return False
    if failed:
        return None
    value, window, until = got
    state = load(sid)
    used, share = _current(state, sid, payload, now)
    why = agent_set_refusal(state, used, share, value, window, until)
    if why:
        save(sid, state)
        return {"systemMessage": f"agentcap: limit not changed: {why}."}
    detail = start_quota(state, sid, value, window, now, source="agent", until=until, live=False,
                         seed=latest_reading(sid, payload, window, now))
    save(sid, state)
    tier = ("" if is_pro() else
            f" Free: you get the meter and one notice at the limit; stopping is Pro ({PRICING_URL}).")
    return {"systemMessage": f"agentcap: limit set (you approved the agent's command): {detail}. "
                             "Only you can raise it (`agentcap approve`, `agentcap <N%>`) or clear it "
                             f"(`agentcap off`).{tier}"}


# ----------------------------------------------------------------- self-guard
# Only the installed plugin and AgentCap's state dir are protected (not a repo checkout of
# AgentCap: developing it inside Codex must stay possible).
_STATE_RE = re.compile(r"state/agentcap\b|\.codex/plugins\S*agentcap")
_READ_HEADS = frozenset({
    "cat", "head", "tail", "less", "more", "grep", "egrep", "rg", "ls", "wc", "stat", "jq", "diff", "file",
    "tree", "du", "nl", "cut", "sort", "uniq", "od", "xxd", "sha256sum", "md5sum", "realpath", "readlink",
    "basename", "dirname", "echo", "pwd", "cd", "true", "test", "[", "find", "sed",
})
# Shell tools only.  A patch or file write that merely *contains* the words of a command
# (live 14 Nov 2026: a README patch quoting the approve command was denied) is not running it.
_SHELL_TOOLS = frozenset({"Bash", "exec_command", "shell", "local_shell", "exec", "unified_exec"})


def _segment_reads(seg: str) -> bool:
    words = seg.split()
    if not words or re.search(r">|\$\(|`|<\(", seg):
        return False
    head = words[0]
    if head == "sed":
        return not any(w.startswith("-i") or w == "--in-place" for w in words[1:])
    if head == "find":
        return not any(w in ("-delete", "-exec", "-execdir", "-ok", "-okdir", "-fprint", "-fprintf")
                       for w in words[1:])
    if head == "git":
        rest = words[1:]
        while rest and rest[0] in ("-C", "--no-pager", "-P"):   # never `-c` (config can run commands)
            rest = rest[2:] if rest[0] == "-C" else rest[1:]
        return (bool(rest) and rest[0] in ("log", "show", "diff", "status", "blame", "ls-files", "rev-parse")
                and not any(w.startswith("--output") or w == "--ext-diff" for w in rest))
    return head in _READ_HEADS


_PATCH_PATH_RE = re.compile(r"^\*\*\* (?:Add File|Update File|Delete File|Move to): (.+)$", re.MULTILINE)


def _patch_targets(tool_input: Any) -> list[str]:
    text = tool_input if isinstance(tool_input, str) else json.dumps(tool_input)
    return [m.strip() for m in _PATCH_PATH_RE.findall(text.replace("\\n", "\n"))]


def self_guard(payload: Mapping[str, Any]) -> str | None:
    tool = payload.get("tool_name")
    if tool == "apply_patch":
        # judge a patch by the files it writes, never by the text it contains
        if any(_STATE_RE.search(os.path.expanduser(p)) for p in _patch_targets(payload.get("tool_input"))):
            return ("agentcap guard (not a quota stop): the agent may not change AgentCap's state or "
                    "installed plugin files. Continue the task without it.")
        return None
    if tool not in _SHELL_TOOLS:
        return None
    cmd = command_text(payload.get("tool_input"))
    if not cmd:
        return None
    if is_self_approve_command(cmd):
        return ("agentcap guard (not a quota stop): the agent may not approve its own limit; the user "
                "types the approve command as a prompt. Continue the task without it.")
    if _STATE_RE.search(cmd):
        segments = [s.strip() for s in re.split(r"\|\||&&|[;|]", cmd) if s.strip()]
        if not all(_segment_reads(s) for s in segments):
            return ("agentcap guard (not a quota stop): the agent may not change AgentCap's state or "
                    "installed plugin files (reading them is fine). Continue the task without it.")
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
    detail = start_quota(state, sid, float(d["quota"]), d["window"], now, source="default",
                         until=bool(d.get("until")))
    save(sid, state)
    return {"systemMessage": f"agentcap (default limit): {detail}."}


def limit_reading(state: dict[str, Any], sid: str, payload: Mapping[str, Any],
                  now: float) -> tuple[float, float, float | None] | None:
    """(share, quota, window % now) after the reading updates both prompt hooks use.

    PreToolUse and UserPromptSubmit share this one computation: the same live and
    rollout readings, the same baseline adoption and the same share decide both;
    neither adds a read of its own.
    """

    if state.get("quota") is None:
        return None
    ensure_baseline(state, sid, payload)
    maybe_probe(sid, now)
    refresh(state, sid, payload, now)
    resolve_until(state)
    tracker = ShareTracker.from_dict(state.get("tracker"))
    if tracker.baseline is None:
        return None
    return float(state.get("share") or 0), float(state["quota"]), tracker.last_used


def hook_prompt(payload: Mapping[str, Any], *, now: float, pro: bool) -> dict[str, Any] | None:
    sid = payload.get("session_id")
    prompt = payload.get("prompt")
    if not isinstance(sid, str) or not sid or not isinstance(prompt, str):
        return None
    m = CMD_RE.match(prompt)
    if m and "\n" not in prompt.strip():
        try:
            text = run_command(m.group(1).strip(), sid, now)
        except ValueError as exc:
            text = f"agentcap: {exc}. {HELP}"
        return {"decision": "block", "reason": text}
    # Any other prompt is a model call: block it while a Pro session is at/over its
    # stop point (the PreToolUse deny only ends the turn, it does not close the door).
    if not pro:
        return None
    state = load(sid)
    if state.get("quota") is None:
        return None
    reading = limit_reading(state, sid, payload, now)
    save(sid, state)
    if reading is None:
        return None
    share, quota, used_now = reading
    if share < quota:
        return None
    window = WINDOW_NAMES.get(state.get("window", "week"), "weekly")
    stop = stop_point(quota, share, used_now)
    at = "unknown" if used_now is None else f"{used_now:g}"
    point = "unknown" if stop is None else f"{stop:g}"
    return {"decision": "block", "reason": (
        f"agentcap: limit reached — {window} quota at {at}%, stop point {point}%. "
        "Run `agentcap approve` (+50%), `agentcap <N%> 5h` or `agentcap off`."
    )}


def hook_pre(payload: Mapping[str, Any], *, now: float, pro: bool) -> dict[str, Any] | None:
    sid = payload.get("session_id")
    name = payload.get("tool_name")
    if not isinstance(sid, str) or not sid or not isinstance(name, str):
        return None
    guard = self_guard(payload)
    if guard:
        return _deny(guard)
    is_set, out = agent_set_pre(payload, sid, now)
    if is_set:
        return out
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
    reading = limit_reading(state, sid, payload, now)
    if reading is None:
        save(sid, state)
        return None
    share, quota, used_now = reading
    window = WINDOW_NAMES.get(state.get("window", "week"), "weekly")
    out: dict[str, Any] | None = None
    if share >= quota:
        if pro:
            state["denies"] = int(state.get("denies", 0)) + 1
            at = f"the {window} quota is at {float(used_now):g}%; " if used_now is not None else ""
            out = _deny(f"agentcap: {at}this session used {share:.1f} of its {quota:g} points and reached its "
                        "stop point. Stop now: do not call more tools; summarise what is done and what is left. "
                        "The user can type `agentcap approve` (+50%) or `agentcap off`.")
        elif not state.get("nudged"):
            state["nudged"] = True
            out = {"systemMessage": f"agentcap: this session passed its stop point ({share:.1f} of {quota:g} "
                                    f"points of the {window} quota). AgentCap Pro would stop the agent here: "
                                    f"{PRICING_URL}"}
    elif share >= quota * WARN_FRACTION and not state.get("warned"):
        state["warned"] = True
        out = {"systemMessage": f"{share_line(state)}."}
    save(sid, state)
    return out


def hook_post(payload: Mapping[str, Any], *, now: float) -> dict[str, Any] | None:
    sid = payload.get("session_id")
    name = payload.get("tool_name")
    if not isinstance(sid, str) or not sid or not isinstance(name, str):
        return None
    if is_wait_call(name, payload.get("tool_input")):
        return None
    failed = tool_failed(payload.get("tool_response"))
    applied = agent_set_post(payload, sid, now, failed)
    if applied is not False:
        return applied
    state = load(sid)
    loop = state.setdefault("loop", {})
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
        resolve_until(state)
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
            _emit(stdout, hook_prompt(payload, now=now, pro=is_pro()))
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
