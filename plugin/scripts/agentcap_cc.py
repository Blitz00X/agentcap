#!/usr/bin/env python3
"""AgentCap Claude Code plugin: per-session share of the Max/Pro subscription quota.

Stdlib only.  Reuses ``agentcap.quota`` from this repo (``<plugin root>/src``),
so the plugin needs no pip install.  Read-only by design: the quota sources are
the ``rate_limits`` object Claude Code hands to the statusLine command and
Claude Code's own ``claude -p /usage`` (a local command, no model call).  No
OAuth token, credential file or API traffic is ever touched.

Subcommands
  statusline              statusLine command (records readings, chains the user's line)
  hook pre|prompt|start|stop  hook entry points (JSON on stdin)
  quota <N%> [5h|week]    set this session's quota      (slash command)
  status | off            show / clear it                (slash commands)
  setup | unsetup         add / remove the statusLine in ~/.claude/settings.json
  usage-probe             background /usage read started by the PreToolUse hook
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Mapping, TextIO

ROOT = Path(__file__).resolve().parent.parent
sys.dont_write_bytecode = True  # keep the installed plugin package unchanged (no __pycache__)
sys.path.insert(0, str(ROOT / "src"))

from agentcap.quota import (  # noqa: E402
    STATE_ENV,
    Reading,
    _num,
    ShareTracker,
    _atomic_write,
    _normalized_shell_text,
    is_self_approve_command,
    _read_json,
    baseline_from_last,
    claude_readings,
    agent_set_refusal,
    approve_quota,
    parse_agent_set,
    parse_limit_words,
    parse_quota,
    resolve_until,
    stop_point,
    stop_text,
    state_root as _quota_state_root,
)
from agentcap.live_quota import (  # noqa: E402
    live_enabled as _live_enabled,
    live_readings as _live_readings,
)
from agentcap.private import write_private_text  # noqa: E402
from agentcap.waste import is_wait_call  # noqa: E402


def _testing() -> bool:
    """Environment overrides are a test facility, not a production input.

    A project-level Claude settings ``env`` block must not be able to redirect
    the plugin's quota state or licence cache (or turn live reads off).
    """

    return os.environ.get("AGENTCAP_TESTING") == "1"


def state_root() -> Path:
    """Plugin state root, pinned to the user's home unless explicitly testing."""

    if _testing():
        return _quota_state_root()
    return Path.home() / ".local" / "state" / "agentcap" / "quota"


def live_enabled() -> bool:
    if _testing():
        return _live_enabled()
    return True


def live_readings(agent: str) -> "dict[str, Any] | None":
    return _live_readings(agent, enabled=live_enabled(), state_dir=state_root())

WARN_FRACTION = 0.8
MARK = "agentcap-cc"  # tag inside our statusLine command (setup/unsetup detection)
WINDOW_NAMES = {"week": "weekly", "5h": "5-hour"}
# /usage is checked in the background at most once a minute while a quota is set:
# the statusline % lags /usage by 1-2 points and a stale first value made a 0% job
# look like 1% (live CLI tests C1-C3, 12-13 Nov 2026).
USAGE_POLL_S = 30.0   # headless `claude -p` has no statusline: /usage is the only source
PROBE_STALE_S = 120.0
RESET_MATCH_S = 120.0


# ----------------------------------------------------------------- paths
def cc_dir() -> Path:
    return state_root() / "cc"


def _safe(sid: str) -> str:
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in sid)[:120] or "unknown"


def session_path(sid: str) -> Path:
    return cc_dir() / "sessions" / f"{_safe(sid)}.json"


def cwd_key(cwd: str) -> str:
    return hashlib.sha256(os.path.realpath(cwd or ".").encode()).hexdigest()[:16]


def claude_settings_path() -> Path:
    base = os.environ.get("CLAUDE_CONFIG_DIR")
    return (Path(base).expanduser() if base else Path.home() / ".claude") / "settings.json"


# ----------------------------------------------------------------- licence
def license_path() -> Path:
    env = os.environ.get("AGENTCAP_LICENSE_FILE")
    if _testing() and env:
        return Path(env).expanduser()
    return Path.home() / ".config" / "agentcap" / "license.json"


def is_pro() -> bool:
    """Offline read of the agentcap CLI's validated licence cache (fail closed)."""

    data = _read_json(license_path())
    return bool(
        data
        and data.get("source") == "polar"
        and data.get("valid") is True
        and data.get("tier") == "pro"
        and isinstance(data.get("key"), str)
        and data.get("key")
    )


# ----------------------------------------------------------------- session state
def load(sid: str) -> dict[str, Any]:
    return _read_json(session_path(sid)) or {"session_id": sid}


def save(sid: str, state: Mapping[str, Any]) -> None:
    _atomic_write(session_path(sid), state)


def resolve_session(raw: str | None, cwd: str | None = None) -> str | None:
    """Session id from the slash command, else the newest session in this cwd."""

    if raw and not raw.startswith("$") and "{" not in raw:
        return raw
    pointer = _read_json(cc_dir() / "by_cwd" / f"{cwd_key(cwd or os.getcwd())}.json")
    sid = pointer.get("session_id") if pointer else None
    return sid if isinstance(sid, str) and sid else None


def usage_path(sid: str) -> Path:
    return cc_dir() / "usage" / f"{_safe(sid)}.json"


def usage_share(state: Mapping[str, Any], sid: str | None) -> float | None:
    """Share by /usage: this session's quota-time /usage read vs the newest one."""

    base = state.get("usage_base")
    window = state.get("window", "week")
    if not sid or not isinstance(base, Mapping):
        return None
    cur = ((_read_json(usage_path(sid)) or {}).get("readings") or {}).get(window)
    if not isinstance(cur, Mapping):
        return None
    try:
        b_used, c_used = float(base["used"]), float(cur["used"])
    except (KeyError, TypeError, ValueError):
        return None
    b_reset, c_reset = base.get("resets_at"), cur.get("resets_at")
    if b_reset is not None and c_reset is not None and abs(float(c_reset) - float(b_reset)) > RESET_MATCH_S:
        return None  # the window reset since: the statusline tracker carries it
    return max(0.0, round(c_used - b_used, 3))


def effective_share(state: Mapping[str, Any], sid: str | None) -> float:
    by_usage = usage_share(state, sid)
    return max(float(state.get("share") or 0), by_usage or 0.0)


def current_used(state: Mapping[str, Any], sid: str | None) -> float | None:
    """The window's % now: the newer of the statusline and /usage (the share uses the same pair)."""

    used = _num(state.get("used"))
    if usage_share(state, sid) is not None:
        cur = ((_read_json(usage_path(sid)) or {}).get("readings") or {}).get(state.get("window", "week"))
        c_used = _num(cur.get("used")) if isinstance(cur, Mapping) else None
        if c_used is not None and (used is None or c_used > used):
            used = c_used
    return used


def share_line(state: Mapping[str, Any], sid: str | None = None) -> str:
    quota = state.get("quota")
    window = state.get("window", "week")
    if quota is None:
        return ""
    name = WINDOW_NAMES.get(window, window)
    if state.get("used") is None:
        return f"agentcap: {limit_sentence(state)}"
    share = effective_share(state, sid or state.get("session_id"))
    return (f"agentcap: {stop_text(float(quota), share, current_used(state, sid or state.get('session_id')), name)} "
            f"(this session used {share:.1f} of {float(quota):g} points)")


def limit_sentence(state: Mapping[str, Any]) -> str:
    window = WINDOW_NAMES.get(state.get("window", "week"), "weekly")
    quota = float(state.get("quota") or 0)
    until = state.get("until")
    used = _num(state.get("used"))
    if used is None or ShareTracker.from_dict(state.get("tracker")).baseline is None:
        what = (f"this session stops when the {window} quota reaches {float(until):g}%" if until is not None
                else f"this session may use {quota:g} more points of the {window} quota")
        return f"{what}; the stop point is shown after the first reading"
    if until is not None and quota <= 0:
        return (f"the {window} quota is already at {used:g}%, at or above your target {float(until):g}%, "
                "so tool calls are denied now; set a higher target or /agentcap:off")
    head = (f"this session stops at {float(until):g}% of the {window} quota" if until is not None
            else f"this session may use {quota:g} more points of the {window} quota")
    return f"{head}: {stop_text(quota, float(state.get('share') or 0), used, window)}"


def maybe_probe_usage(sid: str, state: Mapping[str, Any], now: float, *, force: bool = False) -> bool:
    """Start a background `claude -p /usage` read when the last one is a minute old.

    ``force``: a limit without a baseline yet (a default applied at SessionStart)."""

    if not (state.get("usage_base") or force) or not live_enabled():
        return False
    last = _read_json(usage_path(sid)) or {}
    if now - float(last.get("ts") or 0) < USAGE_POLL_S:
        return False
    lock = usage_path(sid).with_suffix(".probe")
    if now - float((_read_json(lock) or {}).get("ts") or 0) < PROBE_STALE_S:
        return False  # one already running
    _atomic_write(lock, {"ts": now})
    try:
        subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), "usage-probe", "--session", sid],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True, close_fds=True,
        )
    except OSError:
        return False
    return True


def adopt_usage_baseline(state: dict[str, Any], sid: str) -> bool:
    """A limit set without a reading (default at SessionStart) takes its baseline from
    the first `/usage` read made after it was set."""

    if state.get("quota") is None or state.get("usage_base") or state.get("used") is not None:
        return False
    cur = _read_json(usage_path(sid)) or {}
    if float(cur.get("ts") or 0) < float(state.get("set_at") or 0) - 5:
        return False
    block = (cur.get("readings") or {}).get(state.get("window", "week"))
    if not isinstance(block, Mapping) or _num(block.get("used")) is None:
        return False
    seed = Reading(float(block["used"]), _num(block.get("resets_at")))
    tracker = ShareTracker()
    tracker.seed(seed)
    state.update({"usage_base": {"used": seed.used, "resets_at": seed.resets_at, "ts": cur.get("ts")},
                  "tracker": tracker.to_dict(), "share": 0.0, "used": seed.used, "resets_at": seed.resets_at})
    resolve_until(state)
    return True


def defaults_path() -> Path:
    return cc_dir() / "defaults.json"


def _is_probe_session(cwd: str) -> bool:
    """AgentCap's own `claude -p /usage` probe runs in state_root()/probe; its hooks
    must not apply a default (or start another probe)."""

    try:
        return os.path.realpath(cwd) == os.path.realpath(str(state_root() / "probe"))
    except (OSError, ValueError):
        return False


def cmd_default(args: list[str], *, now: float) -> str:
    if not args:
        d = _read_json(defaults_path())
        return (f"agentcap: new sessions start with {_default_text(d)}."
                if d and d.get("quota") is not None else
                "agentcap: no default limit. Set one with /agentcap:default 10% 5h.")
    if args[0].lower() in ("off", "none", "clear"):
        try:
            defaults_path().unlink()
        except OSError:
            pass
        return "agentcap: default limit removed; new sessions start without one."
    try:
        quota, window, until = parse_limit_words(args)
    except ValueError as exc:
        return f"agentcap: {exc}"
    d = {"quota": quota, "window": window, "until": until, "set_at": now}
    _atomic_write(defaults_path(), d)
    return (f"agentcap: every new Claude Code session (interactive and `claude -p`) now starts with "
            f"{_default_text(d)}. "
            f"This session is unchanged; /agentcap:quota {'until ' if until else ''}{quota:g}% {window} "
            "sets it here too.")


def _default_text(d: Mapping[str, Any]) -> str:
    window = WINDOW_NAMES.get(d.get("window", "week"), "weekly")
    if d.get("until"):
        return f"a stop at {float(d['quota']):g}% of the {window} quota"
    return f"a limit of {float(d['quota']):g} more points of the {window} quota"


def cmd_usage_probe(sid: str | None, *, now: float) -> str:
    if not sid:
        return ""
    lock = usage_path(sid).with_suffix(".probe")
    try:
        got = live_readings("claude")
        if got:
            _atomic_write(usage_path(sid), {"ts": time.time(), "readings": {
                w: {"used": r.used, "resets_at": r.resets_at} for w, r in got.items()}})
    finally:
        try:
            lock.unlink()
        except OSError:
            pass
    return ""


def apply_readings(state: dict[str, Any], readings: Mapping[str, Any], now: float) -> None:
    state["last_readings"] = {w: {"used": r.used, "resets_at": r.resets_at} for w, r in readings.items()}
    state["readings_ts"] = now
    quota = state.get("quota")
    window = state.get("window", "week")
    if quota is None or window not in readings:
        return
    reading = readings[window]
    tracker = ShareTracker.from_dict(state.get("tracker"))
    tracker.update(reading)
    state["tracker"] = tracker.to_dict()
    state["share"] = tracker.share
    state["used"] = reading.used
    state["resets_at"] = reading.resets_at
    resolve_until(state)


# ----------------------------------------------------------------- statusline
def statusline_main(stdin: TextIO, stdout: TextIO, *, now: float | None = None) -> int:
    raw = stdin.read()
    now = time.time() if now is None else now
    line = ""
    try:
        payload = json.loads(raw) if raw.strip() else {}
        readings = claude_readings(payload)
        sid = payload.get("session_id") if isinstance(payload, Mapping) else None
        if readings:
            _atomic_write(
                state_root() / "claude_last.json",
                {"ts": now, "readings": {w: {"used": r.used, "resets_at": r.resets_at} for w, r in readings.items()}},
            )
        if isinstance(sid, str) and sid:
            state = load(sid)
            apply_readings(state, readings, now)
            save(sid, state)
            line = share_line(state, sid)
    except Exception:
        line = "agentcap: quota unavailable"
    prev = (_read_json(cc_dir() / "statusline.json") or {}).get("previous")
    if isinstance(prev, Mapping) and isinstance(prev.get("command"), str) and prev["command"].strip():
        try:
            res = subprocess.run(prev["command"], shell=True, input=raw, capture_output=True, text=True, timeout=8)
            if res.stdout.strip():
                stdout.write(res.stdout.rstrip("\n") + "\n")
        except Exception:
            pass
    if line:
        stdout.write(line + "\n")
    stdout.flush()
    return 0


# ----------------------------------------------------------------- hooks
def _emit(stdout: TextIO, obj: Mapping[str, Any]) -> None:
    json.dump(obj, stdout)
    stdout.write("\n")


LOOP_STATE_TTL_S = 7 * 86400
SESSION_STATE_TTL_S = 30 * 86400   # sessions/, usage/, by_cwd/: untouched for 30 days
RECEIPT_TTL_S = 90 * 86400         # receipts.jsonl lines older than 90 days


def _prune_dir(path: Path, patterns: tuple[str, ...], ttl: float, now: float) -> None:
    try:
        for pattern in patterns:
            for f in path.glob(pattern):
                try:
                    if f.is_file() and not f.is_symlink() and now - f.stat().st_mtime > ttl:
                        f.unlink()
                except OSError:
                    pass
    except OSError:
        pass


def prune_loop_state(now: float) -> None:
    """Delete per-session loop counters older than 7 days (only our *.json files)."""

    _prune_dir(cc_dir() / "loops", ("*.json",), LOOP_STATE_TTL_S, now)


def prune_receipts(now: float) -> None:
    """Keep receipts.jsonl to the last 90 days (rewritten only when a line is dropped)."""

    path = cc_dir() / "receipts.jsonl"
    try:
        if path.is_symlink() or not path.is_file():
            return
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return
    keep = []
    for line in lines:
        try:
            ts = float(json.loads(line).get("ts") or 0)
        except (ValueError, TypeError, AttributeError):
            ts = now  # an unreadable line is kept, not guessed old
        if now - ts <= RECEIPT_TTL_S:
            keep.append(line)
    if len(keep) == len(lines):
        return
    tmp = path.with_suffix(".jsonl.tmp")
    try:
        tmp.write_text("".join(f"{line}\n" for line in keep), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        pass


def prune_state(now: float) -> None:
    """Local retention: loop counters 7 days, session files 30 days, receipts 90 days."""

    prune_loop_state(now)
    for sub in ("sessions", "usage", "by_cwd"):
        _prune_dir(cc_dir() / sub, ("*.json", "*.probe"), SESSION_STATE_TTL_S, now)
    prune_receipts(now)


def hook_start(payload: Mapping[str, Any], stdout: TextIO, *, now: float) -> int:
    sid = payload.get("session_id")
    if not isinstance(sid, str) or not sid:
        return 0
    cwd = payload.get("cwd") if isinstance(payload.get("cwd"), str) else os.getcwd()
    if _is_probe_session(cwd):
        return 0
    _atomic_write(cc_dir() / "by_cwd" / f"{cwd_key(cwd)}.json", {"session_id": sid, "ts": now})
    _atomic_write(cc_dir() / "plugin_root.json", {"root": str(ROOT), "ts": now})
    prune_state(now)
    messages = []
    d = _read_json(defaults_path())
    state = load(sid)
    if d and d.get("quota") is not None and state.get("quota") is None:
        state.update({"quota": float(d["quota"]), "window": d.get("window", "week"), "set_at": now,
                      "source": "default", "tracker": ShareTracker().to_dict()})
        if d.get("until"):
            state["until"] = float(d["quota"])
        save(sid, state)
        maybe_probe_usage(sid, state, now, force=True)
        messages.append(f"agentcap: this session starts with the default limit, {_default_text(d)}.")
    if not (_read_json(cc_dir() / "statusline.json") or {}).get("installed") and not d:
        messages.append("agentcap: run /agentcap:setup once so it can read your subscription quota (statusLine).")
    if messages:
        _emit(stdout, {"systemMessage": " ".join(messages)})
    return 0


# ------------------------------------------------------------ loop breaker
LOOP_REPEATS = 4   # same tool call (name + input) 4x in a row, like the CLI/proxy breaker
LOOP_ERRORS = 8    # 8 failing tool calls in a row
POLL_TOOLS = frozenset({"wait", "sleep", "write_stdin", "list_agents", "TaskOutput", "BashOutput"})
PRO_HINT = "see the Pro section of the plugin README"   # no URL in hook code (directory policy)


def _digest(name: str, payload: Any) -> str:
    canon = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(f"{name}\0{canon}".encode()).hexdigest()[:16]


def _loop_path(sid: str) -> Path:
    return cc_dir() / "loops" / f"{_safe(sid)}.json"


def _loop_state(payload: Mapping[str, Any]) -> tuple[str, dict[str, Any], dict[str, Any]] | None:
    sid = payload.get("session_id")
    if not isinstance(sid, str) or not sid:
        return None
    allst = _read_json(_loop_path(sid)) or {}
    stream = str(payload.get("agent_id") or "main")  # subagents get their own streak
    return sid, allst, allst.setdefault(stream, {})


def _stop(stdout: TextIO, event: str, reason: str, *, deny: bool) -> None:
    out: dict[str, Any] = {"continue": False, "stopReason": reason, "systemMessage": reason}
    if deny:
        out["hookSpecificOutput"] = {"hookEventName": event, "permissionDecision": "deny",
                                     "permissionDecisionReason": reason}
    _emit(stdout, out)


def loop_pre(payload: Mapping[str, Any], stdout: TextIO, *, now: float, pro: bool) -> bool:
    """Track identical tool calls; True when this call was denied (Pro)."""

    name = payload.get("tool_name")
    got = _loop_state(payload)
    if not isinstance(name, str) or got is None or is_wait_call(name, payload.get("tool_input")):
        return False
    sid, allst, st = got
    d = _digest(name, payload.get("tool_input"))
    st["count"] = int(st.get("count", 0)) + 1 if st.get("digest") == d else 1
    if st.get("digest") != d:
        st["nudged"] = False
    st["digest"], st["tool"], st["ts"] = d, name[:40], now
    allst["last_tool"] = name[:40]
    hit = st["count"] >= LOOP_REPEATS
    if hit and pro:
        st["stops"] = int(st.get("stops", 0)) + 1
    nudge = hit and not pro and not st.get("nudged")
    if nudge:
        st["nudged"] = True
    _atomic_write(_loop_path(sid), allst)
    detail = f"same tool call {st['count']} times in a row ({name[:40]})"
    if hit and pro:
        _stop(stdout, "PreToolUse", f"agentcap loop breaker: {detail}. Stop now: do not call more "
              "tools; summarise what is done and what is left.", deny=True)
        return True
    if nudge:
        _emit(stdout, {"systemMessage": f"agentcap: {detail}. AgentCap Pro would stop the agent here; {PRO_HINT}."})
        return True
    return False


def hook_post(payload: Mapping[str, Any], stdout: TextIO, *, now: float, pro: bool, failed: bool) -> int:
    got = _loop_state(payload)
    if got is None:
        return 0
    sid, allst, st = got
    if agent_set_post(payload, stdout, now=now, failed=failed, pro=pro):
        return 0
    if is_wait_call(str(payload.get("tool_name") or ""), payload.get("tool_input")):
        return 0  # a wait (sleep ...) that has not seen its file yet
    if failed and "agentcap" in json.dumps(payload.get("error") or payload.get("tool_response") or "").lower():
        return 0  # a pause by AgentCap itself, not a failing call (live C3, 12 Nov 2026)
    st["errors"] = int(st.get("errors", 0)) + 1 if failed else 0
    hit = st["errors"] >= LOOP_ERRORS
    nudge = hit and not pro and not st.get("err_nudged")
    if not failed:
        st["err_nudged"] = False
    if nudge:
        st["err_nudged"] = True
    _atomic_write(_loop_path(sid), allst)
    if not hit:
        return 0
    name = str(payload.get("tool_name") or "tool")[:40]
    detail = f"{st['errors']} failing tool calls in a row (last: {name})"
    if pro:
        _stop(stdout, "PostToolUseFailure", f"agentcap loop breaker: {detail}. Stop now and summarise "
              "what is done and what is left.", deny=False)
    elif nudge:
        _emit(stdout, {"systemMessage": f"agentcap: {detail}. AgentCap Pro would stop the agent here; {PRO_HINT}."})
    return 0


# ------------------------------------------------------------ self guard
# Claude Code (measured on 2.1.318) runs a slash command's `!` line WITHOUT
# PreToolUse hooks, while every tool call the model makes goes through them.
# So the user's /agentcap:approve|quota|off keep working, and any model-made
# tool call that would run this script or touch its state can be denied here.
SELF_GUARD_REASON = (
    "agentcap: the agent may not run AgentCap's plugin script or change its state files; "
    "approve, quota and off belong to the user. Stop and ask the user to run "
    "/agentcap:approve, /agentcap:quota or /agentcap:off themselves."
)
# the script itself (any path, `python -m agentcap_cc`, `import agentcap_cc`) or a glob for it
_SCRIPT_RE = re.compile(
    r"agentcap_cc\b|agentcap[^/\s]*[*?]|CLAUDE_PLUGIN_ROOT|\.claude/plugins\S*agentcap"
)
_STATE_RE = re.compile(r"state/agentcap\b|" + re.escape(STATE_ENV))
_SCRIPT_GLOB_RE = re.compile(r"agentcap[^/\s]*[*?]")
# only plain reads of those paths may pass; anything that can execute or write is denied
_EXEC_OR_WRITE_RE = re.compile(r"\$\(|`|>|<\(")
_SEGMENT_SPLIT_RE = re.compile(r"\|\||&&|[;&|()]")
READ_ONLY_HEADS = frozenset({
    "cat", "head", "tail", "less", "more", "grep", "egrep", "fgrep", "rg", "wc", "nl", "file", "stat",
    "ls", "tree", "du", "diff", "cmp", "sha256sum", "sha1sum", "md5sum", "realpath", "readlink",
    "basename", "dirname", "cd", "pwd", "echo", "true", "jq",
})
_GIT_RISKY_RE = re.compile(
    r"(?:^|\s)-c\b|--output|--open-files-in-pager|--exec-path|--config-env|"
    r"config|alias|!|exec|upload-pack|receive-pack|ssh|checkout|restore|reset|apply|\bam\b"
)
READ_TOOLS = frozenset({"Read", "Grep", "Glob", "LS", "NotebookRead"})
PATH_KEYS = ("file_path", "notebook_path", "path")


def _real(path: str | Path) -> str:
    return os.path.realpath(os.path.expanduser(str(path)))


def _inside(path: str, root: str) -> bool:
    try:
        return os.path.commonpath([path, root]) == root
    except ValueError:
        return False


def _path_forms(path: Path) -> set[str]:
    """How a command line may spell ``path``: absolute, real, ``~/``, ``$HOME/``."""

    forms = {str(path).rstrip("/"), _real(path).rstrip("/")}
    home = str(Path.home()).rstrip("/")
    for p in list(forms):
        if home and p.startswith(home + "/"):
            rel = p[len(home) + 1:]
            forms.update({f"~/{rel}", f"$HOME/{rel}", f"${{HOME}}/{rel}"})
    return {f for f in forms if len(f) > 1}


def _protected_roots(cwd: str) -> list[Path]:
    """State dir always; the plugin's own code unless the session works inside its checkout."""

    roots = [state_root()]
    if not _inside(_real(cwd or "/"), _real(ROOT)):
        roots.append(ROOT)
    return roots


def _read_only(command: str) -> bool:
    """Every command in ``command`` is a plain read (the normaliser joins lines, so split first)."""

    segments = [seg for line in command.splitlines()
                for seg in _SEGMENT_SPLIT_RE.split(_normalized_shell_text(line))]
    if any(_EXEC_OR_WRITE_RE.search(seg) for seg in segments):
        return False
    for seg in segments:
        words = seg.split()
        if not words:
            continue
        head = os.path.basename(words[0])
        if head == "git" and not _GIT_RISKY_RE.search(seg):
            continue
        if head not in READ_ONLY_HEADS:
            return False
    return True


def command_touches_plugin(command: str, cwd: str) -> bool:
    """True when a model-made shell command would run the plugin script or alter its state."""

    text = _normalized_shell_text(command)
    hit = bool(_SCRIPT_RE.search(text) or _STATE_RE.search(text))
    if not hit:
        hit = any(form in text for root in _protected_roots(cwd) for form in _path_forms(root))
    # A glob that can match the installed script is denied even when the
    # command is otherwise a plain read (e.g. `cat .../agentcap*`).
    glob_hit = bool(_SCRIPT_GLOB_RE.search(text))
    return hit and (glob_hit or not _read_only(command))


def path_touches_plugin(raw: str, cwd: str) -> bool:
    base = cwd if cwd and os.path.isabs(cwd) else os.getcwd()
    target = _real(os.path.join(base, os.path.expanduser(raw)))
    return any(_inside(target, _real(root)) for root in _protected_roots(cwd))


def self_guard(payload: Mapping[str, Any]) -> bool:
    """Deny a model tool call that would approve/raise/clear its own limit (True if denied)."""

    tool = payload.get("tool_name")
    tool_input = payload.get("tool_input")
    if not isinstance(tool, str) or not isinstance(tool_input, Mapping):
        return False
    cwd = payload.get("cwd") if isinstance(payload.get("cwd"), str) else ""
    command = tool_input.get("command")
    if isinstance(command, str) and command_touches_plugin(command, cwd):
        return True
    # `agentcap approve` from the model (live C6, 13 Nov 2026: only the CLI's env-marker
    # check stopped it, and `env -u CLAUDECODE agentcap approve` would pass that)
    if isinstance(command, str) and is_self_approve_command(command):
        return True
    if tool in READ_TOOLS:
        return False
    return any(isinstance(tool_input.get(k), str) and tool_input[k] and path_touches_plugin(tool_input[k], cwd)
               for k in PATH_KEYS)


def quota_reading(sid: str, state: dict[str, Any], now: float) -> tuple[float, float, float | None] | None:
    """(share, quota, window % now) after the reading updates both prompt hooks use.

    PreToolUse and UserPromptSubmit share this one computation: the same baseline
    adoption, the same background /usage cadence and the same effective share
    decide both, and neither creates a network call of its own.
    """

    quota = state.get("quota")
    if quota is not None and state.get("used") is None:
        if adopt_usage_baseline(state, sid):
            save(sid, state)
        else:
            maybe_probe_usage(sid, state, now, force=True)
    if resolve_until(state):
        save(sid, state)
    quota = state.get("quota")
    if quota is None or state.get("used") is None:
        return None
    maybe_probe_usage(sid, state, now)
    return effective_share(state, sid), float(quota), current_used(state, sid)


def hook_pre(payload: Mapping[str, Any], stdout: TextIO, *, now: float, pro: bool) -> int:
    if self_guard(payload):  # every tier: these settings are the user's, never the agent's
        _emit(stdout, {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                              "permissionDecisionReason": SELF_GUARD_REASON}})
        return 0
    if agent_set_pre(payload, stdout, now=now):
        return 0
    if loop_pre(payload, stdout, now=now, pro=pro):
        return 0
    sid = payload.get("session_id")
    if not isinstance(sid, str) or not sid or not session_path(sid).exists():
        return 0
    state = load(sid)
    reading = quota_reading(sid, state, now)
    if reading is None:
        return 0
    share, quota, used_now = reading
    window = WINDOW_NAMES.get(state.get("window", "week"), "weekly")
    if share >= quota:
        if pro:
            at = f"the {window} quota is at {used_now:g}%; " if used_now is not None else ""
            reason = (
                f"agentcap: {at}this session used {share:.1f} of its {float(quota):g} points and reached "
                "its stop point. Stop now: do not call more tools; summarise what is done "
                "and what is left. The user can raise it with /agentcap:approve (+50%), "
                "/agentcap:quota or clear it with /agentcap:off."
            )
            state["denies"] = int(state.get("denies", 0)) + 1
            if state["denies"] == 1:
                # headless `claude -p` ends here with no output and no Stop hook: the
                # receipt is the only record of why (live 14 Nov 2026)
                _write_receipt(state, sid, now, share, reason="quota_exceeded")
            save(sid, state)
            _emit(stdout, {  # deny + end the turn; the session itself stays open
                "continue": False,
                "stopReason": reason,
                "systemMessage": reason,
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": reason,
                },
            })
            return 0
        if not state.get("nudged"):
            state["nudged"] = True
            save(sid, state)
            _emit(stdout, {"systemMessage": (
                f"agentcap: this session passed its stop point ({share:.1f} of {float(quota):g} points of "
                f"the {window} quota). AgentCap Pro would stop the agent here; {PRO_HINT}."
            )})
        return 0
    if share >= quota * WARN_FRACTION and not state.get("warned"):
        state["warned"] = True
        save(sid, state)
        msg = f"{share_line(state, sid)}."
        out: dict[str, Any] = {"systemMessage": msg}
        if pro:
            out["hookSpecificOutput"] = {
                "hookEventName": "PreToolUse",
                "additionalContext": msg + " Wrap up soon; tool calls will be denied at the limit.",
            }
        _emit(stdout, out)
    return 0


def hook_prompt(payload: Mapping[str, Any], stdout: TextIO, *, now: float, pro: bool) -> int:
    """UserPromptSubmit: block a new prompt while a Pro session is at/over its stop point.

    PreToolUse only ends the turn; without this the user could send another prompt
    and the model would answer in text, spending quota.  ``/agentcap:`` commands
    always pass: they are how the user raises or clears the limit (0.5.2).
    """

    prompt = payload.get("prompt")
    if not isinstance(prompt, str) or prompt.lstrip().startswith("/agentcap:"):
        return 0
    sid = payload.get("session_id")
    if not pro or not isinstance(sid, str) or not sid or not session_path(sid).exists():
        return 0
    state = load(sid)
    reading = quota_reading(sid, state, now)
    if reading is None:
        return 0
    share, quota, used_now = reading
    if share < quota:
        return 0
    window = WINDOW_NAMES.get(state.get("window", "week"), "weekly")
    stop = stop_point(quota, share, used_now)
    at = "unknown" if used_now is None else f"{used_now:g}"
    point = "unknown" if stop is None else f"{stop:g}"
    _emit(stdout, {"decision": "block", "reason": (
        f"agentcap: limit reached — {window} quota at {at}%, stop point {point}%. "
        "Run /agentcap:approve (+50%), /agentcap:quota <N%> 5h or /agentcap:off."
    )})
    return 0


def hook_stop(payload: Mapping[str, Any], stdout: TextIO, *, now: float, pro: bool) -> int:
    sid = payload.get("session_id")
    if not isinstance(sid, str) or not sid or not session_path(sid).exists():
        return 0
    state = load(sid)
    if state.get("quota") is None or state.get("used") is None:
        return 0
    by_usage = usage_share(state, sid)
    share = round(effective_share(state, sid), 1)
    if state.get("receipt_share") == share:
        return 0
    state["receipt_share"] = share
    save(sid, state)
    if pro:
        _write_receipt(state, sid, now, share, reason="turn_end", by_usage=by_usage)
    _emit(stdout, {"systemMessage": share_line(state, sid)})
    return 0


def _write_receipt(state: Mapping[str, Any], sid: str, now: float, share: float, *, reason: str,
                   by_usage: float | None = None) -> None:
    if by_usage is None:
        by_usage = usage_share(state, sid)
    rec = {"ts": now, "session_id": sid, "window": state.get("window"), "quota": state.get("quota"),
           "share": round(share, 1), "used_now": state.get("used"), "denies": state.get("denies", 0),
           "measured_by": "usage" if by_usage is not None else "statusline",
           "approvals": state.get("approvals", 0), "reason": reason, "source": state.get("source"),
           "last_tool": (_read_json(_loop_path(sid)) or {}).get("last_tool")}
    try:
        cc_dir().mkdir(parents=True, exist_ok=True)
        with open(cc_dir() / "receipts.jsonl", "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec) + "\n")
    except OSError:
        pass


# ----------------------------------------------------------------- commands
def cmd_quota(args: list[str], sid: str | None, *, now: float) -> str:
    if not sid:
        return "agentcap: no session id yet; send one message first, then run /agentcap:quota again."
    if not args:
        return "usage: /agentcap:quota 5% [week|5h]  or  /agentcap:quota until 70% [week|5h]"
    try:
        quota, window, until = parse_limit_words(args)
    except ValueError as exc:
        return f"agentcap: {exc}"
    state = load(sid)
    seed, source = set_limit(state, sid, quota, window, until, now, origin="command", live=True)
    lines = [f"agentcap: {limit_sentence(state)}."]
    if seed:
        lines.append(f"(baseline {seed.used:g}%, exact, from {source})")
    if not is_pro():
        lines.append("Free tier: you get the live meter and a notice at the limit. Enforcement (deny tool calls) is Pro.")
    if not (_read_json(cc_dir() / "statusline.json") or {}).get("installed"):
        lines.append("Active: measured with /usage (checked every 30 s). Optional: /agentcap:setup adds a live "
                     "meter to the statusline." if source == "/usage" else
                     "Not active yet: /usage did not answer; run /agentcap:setup (statusline) or try again.")
    return "\n".join(lines)


def set_limit(state: dict[str, Any], sid: str, quota: float, window: str, until: bool, now: float,
              *, origin: str, live: bool) -> tuple[Reading | None, str]:
    """Start a limit in ``state`` (saved).  ``live=False`` (inside a hook) uses only readings
    already on disk and starts a background /usage read when there is none."""

    for k in ("tracker", "share", "used", "resets_at", "warned", "nudged", "denies", "receipt_share",
              "usage_base", "until", "until_done", "approvals"):
        state.pop(k, None)
    state.update({"quota": quota, "window": window, "set_at": now, "source": origin})
    if until:
        state["until"] = quota
    tracker = ShareTracker()
    source = "statusline"
    got = live_readings("claude") if live and live_enabled() else None
    seed = got.get(window) if got else None
    if seed is not None:
        source = "/usage"
        state["usage_base"] = {"used": seed.used, "resets_at": seed.resets_at, "ts": now}
        _atomic_write(usage_path(sid), {"ts": now, "readings": {
            w: {"used": r.used, "resets_at": r.resets_at} for w, r in got.items()}})
    else:
        # only this session's own reading: another session's (claude_last.json) can be
        # minutes old while the account kept spending (live C1, 12 Nov 2026)
        seed = baseline_from_last(
            {"ts": state.get("readings_ts"), "readings": state.get("last_readings")}
            if state.get("last_readings") else None, window, now=now)
        if seed is None and not live:
            cur = _read_json(usage_path(sid)) or {}
            block = (cur.get("readings") or {}).get(window)
            if now - float(cur.get("ts") or 0) <= USAGE_POLL_S * 2 and isinstance(block, Mapping) \
                    and _num(block.get("used")) is not None:
                seed = Reading(float(block["used"]), _num(block.get("resets_at")))
                source = "/usage"
                state["usage_base"] = {"used": seed.used, "resets_at": seed.resets_at, "ts": cur.get("ts")}
    if seed is not None:
        tracker.seed(seed)
        state.update({"share": 0.0, "used": seed.used, "resets_at": seed.resets_at})
    state["tracker"] = tracker.to_dict()
    resolve_until(state)
    save(sid, state)
    if seed is None and not live:
        maybe_probe_usage(sid, state, now, force=True)   # adopt_usage_baseline takes it from there
    return seed, source


# ------------------------------------------------------------ agent-set limit
# The user asks in plain words ("use at most half of the 5-hour quota"); the agent runs
# `echo agentcap set 50% 5h`; PreToolUse answers "ask", so Claude Code shows its permission
# prompt (the user's one button) with the stop point; PostToolUse sets the limit after it ran.
# The agent may only set or tighten a limit: raising, clearing and approving stay the user's.
AGENT_SET_HELP = ("the command is `echo agentcap set 50% 5h` (50 more points of the 5-hour quota) or "
                  "`echo agentcap set until 70% 5h` (stop when the 5-hour quota reaches 70%)")


def _agent_set(payload: Mapping[str, Any]) -> tuple[float, str, bool] | None:
    tool_input = payload.get("tool_input")
    if payload.get("tool_name") != "Bash" or not isinstance(tool_input, Mapping):
        return None
    return parse_agent_set(tool_input.get("command"))


def _current(state: Mapping[str, Any], sid: str) -> tuple[float | None, float]:
    if state.get("quota") is None or state.get("used") is None:
        return None, 0.0
    return current_used(state, sid), effective_share(state, sid)


def _known_used(state: Mapping[str, Any], sid: str, window: str, now: float) -> float | None:
    """The window % from readings already on disk (for the approval prompt), else None."""

    seed = baseline_from_last({"ts": state.get("readings_ts"), "readings": state.get("last_readings")}
                              if state.get("last_readings") else None, window, now=now)
    if seed is not None:
        return seed.used
    cur = _read_json(usage_path(sid)) or {}
    block = (cur.get("readings") or {}).get(window)
    if now - float(cur.get("ts") or 0) <= 300 and isinstance(block, Mapping):
        return _num(block.get("used"))
    return None


def preview_text(value: float, window: str, until: bool, used: float | None) -> str:
    name = WINDOW_NAMES.get(window, window)
    what = (f"stop this session when the {name} quota reaches {value:g}%" if until
            else f"let this session use {value:g} more points of the {name} quota")
    if used is None:
        return what
    if until and value <= used:
        return f"{what} (it is at {used:g}% already, so tools would stop right away)"
    return f"{what}: {stop_text(max(value - used, 0.0) if until else value, 0.0, used, name)}"


def agent_set_pre(payload: Mapping[str, Any], stdout: TextIO, *, now: float) -> bool:
    """The agent proposes a limit: deny it if it would loosen the current one, else make Claude
    Code ask the user (True when this tool call was the agent-set command)."""

    def decide(decision: str, reason: str) -> bool:
        _emit(stdout, {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": decision,
                                              "permissionDecisionReason": reason}})
        return True

    try:
        got = _agent_set(payload)
    except ValueError as exc:
        return decide("deny", f"agentcap: {exc}; {AGENT_SET_HELP}.")
    if got is None:
        return False
    sid = payload.get("session_id")
    if not isinstance(sid, str) or not sid:
        return decide("deny", "agentcap: no session id; ask the user to run /agentcap:quota.")
    value, window, until = got
    state = load(sid)
    used, share = _current(state, sid)
    why = agent_set_refusal(state, used, share, value, window, until)
    if why:
        return decide("deny", f"agentcap: {why}. Tell the user they can run "
                              f"/agentcap:quota {'until ' if until else ''}{value:g}% {window} themselves.")
    preview = preview_text(value, window, until, _known_used(state, sid, window, now))
    return decide("ask", f"AgentCap: the agent asks to {preview}. Approve to set this limit; "
                         "only you can raise or remove it later (/agentcap:approve, /agentcap:off).")


def agent_set_post(payload: Mapping[str, Any], stdout: TextIO, *, now: float, failed: bool, pro: bool) -> bool:
    """Set the limit once the approved command ran (True when it was the agent-set command)."""

    try:
        got = _agent_set(payload)
    except ValueError:
        return True
    if got is None:
        return False
    sid = payload.get("session_id")
    if failed or not isinstance(sid, str) or not sid:
        return True
    value, window, until = got
    state = load(sid)
    used, share = _current(state, sid)
    why = agent_set_refusal(state, used, share, value, window, until)
    if why:
        msg = f"agentcap: limit not changed: {why}."
    else:
        set_limit(state, sid, value, window, until, now, origin="agent", live=False)
        tier = "" if pro else " Free: you get the meter and a notice at the limit; stopping is Pro."
        msg = (f"agentcap: limit set (you approved the agent's command): {limit_sentence(state)}. "
               f"Only the user can raise it (/agentcap:approve, /agentcap:quota) or clear it (/agentcap:off).{tier}")
    _emit(stdout, {"systemMessage": msg,
                   "hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": msg}})
    return True


APPROVE_FACTOR, APPROVE_MAX = 1.5, 2


def cmd_approve(sid: str | None) -> str:
    """Pro: raise this session's quota by 50% (max twice) and clear a loop-breaker stop."""

    if not sid:
        return "agentcap: no session found."
    if not is_pro():
        return f"agentcap: approve is a Pro feature (Free never blocks, so there is nothing to approve); {PRO_HINT}."
    state = load(sid)
    loop = _loop_path(sid)
    loop_cleared = loop.exists()
    if loop.exists():
        allst = _read_json(loop) or {}
        for k, v in list(allst.items()):
            if isinstance(v, dict):
                v.update({"count": 0, "errors": 0})
        _atomic_write(loop, allst)
    if state.get("quota") is None:
        return "agentcap: loop streak cleared; no quota set for this session."
    n = int(state.get("approvals", 0))
    if n >= APPROVE_MAX:
        return (f"agentcap: already approved twice (limit {float(state['quota']):g}%). "
                "Set a new limit with /agentcap:quota, or remove it with /agentcap:off.")
    new = approve_quota(float(state["quota"]), APPROVE_FACTOR)
    state.update({"quota": new, "approvals": n + 1, "nudged": False})
    state.pop("until", None)  # from here on the limit is points; the stop point is shown below
    save(sid, state)
    if new >= 100:
        head = (f"agentcap: approved ({n + 1}/{APPROVE_MAX}): limit is now {new:g} points "
                "(above 100), so the quota no longer stops this session")
    else:
        head = f"agentcap: approved ({n + 1}/{APPROVE_MAX}): limit raised to {new:g} points"
        if state.get("used") is not None:
            name = WINDOW_NAMES.get(state.get("window", "week"), "weekly")
            head += f" ({stop_text(new, effective_share(state, sid), current_used(state, sid), name)})"
    if loop_cleared:
        head += "; loop breaker cleared"
    return head + "; tell the agent to continue."


def cmd_status(sid: str | None) -> str:
    if not sid:
        return "agentcap: no session found."
    state = load(sid)
    if state.get("quota") is None:
        return "agentcap: no quota set for this session. Use /agentcap:quota 5%."
    tier = "Pro (enforcing)" if is_pro() else "Free (watch only)"
    checked = (_read_json(usage_path(sid)) or {}).get("ts")
    seen = (f"; /usage checked {int(time.time() - float(checked))}s ago"
            if state.get("usage_base") and checked else "")
    return f"{share_line(state, sid)} — {tier}{seen}"


def cmd_off(sid: str | None) -> str:
    if not sid:
        return "agentcap: no session found."
    state = load(sid)
    for k in ("quota", "window", "tracker", "share", "used", "resets_at", "warned", "nudged", "usage_base",
              "until", "until_done"):
        state.pop(k, None)
    save(sid, state)
    return "agentcap: quota cleared for this session."


def launcher_path() -> Path:
    return cc_dir() / "statusline_launcher.py"


LAUNCHER = '''#!/usr/bin/env python3
# agentcap-cc statusLine launcher: finds the current plugin version (it moves on update).
import json, os, subprocess, sys
here = os.path.dirname(os.path.abspath(__file__))
try:
    root = json.load(open(os.path.join(here, "plugin_root.json")))["root"]
    script = os.path.join(root, "scripts", "agentcap_cc.py")
    data = sys.stdin.buffer.read()
    r = subprocess.run([sys.executable, script, "statusline"], input=data, capture_output=True, timeout=9)
    sys.stdout.buffer.write(r.stdout)
except Exception:
    print("agentcap: plugin not found (reinstall or run /agentcap:unsetup)")
'''


def cmd_setup(*, now: float) -> str:
    path = claude_settings_path()
    data = _read_json(path) if path.exists() else {}
    if data is None:
        return f"agentcap: {path} is not valid JSON; not touching it."
    current = data.get("statusLine")
    info = _read_json(cc_dir() / "statusline.json") or {}
    if isinstance(current, Mapping) and MARK in str(current.get("command", "")):
        return "agentcap: statusLine already set up."
    cc_dir().mkdir(parents=True, exist_ok=True)
    _atomic_write(cc_dir() / "plugin_root.json", {"root": str(ROOT), "ts": now})
    launcher_path().write_text(LAUNCHER, encoding="utf-8")
    _atomic_write(cc_dir() / "statusline.json", {"installed": True, "previous": current, "ts": now, "settings": str(path)})
    backup = path.with_name(path.name + ".agentcap-backup")
    if path.exists() and not backup.exists():
        # Settings can contain env API keys/apiKeyHelper.  Write the backup
        # owner-only and never through a pre-existing symlink.
        write_private_text(backup, path.read_text(encoding="utf-8"))
    cmd = f"python3 {shlex.quote(str(launcher_path()))} # {MARK}"
    data["statusLine"] = {"type": "command", "command": cmd, "refreshInterval": 5}
    path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(path, data)
    prev = "your previous statusLine is still shown above it" if current else "no previous statusLine"
    return (f"agentcap: statusLine set in {path} ({prev}). Backup: {backup.name} (owner-only; "
            "/agentcap:unsetup restores your statusLine and deletes it). "
            "It only reads the rate_limits % Claude Code passes to statusLine.")


def cmd_unsetup() -> str:
    path = claude_settings_path()
    data = _read_json(path)
    info = _read_json(cc_dir() / "statusline.json") or {}
    if data is None:
        return "agentcap: no settings file; nothing to undo."
    current = data.get("statusLine")
    if not (isinstance(current, Mapping) and MARK in str(current.get("command", ""))):
        _atomic_write(cc_dir() / "statusline.json", {"installed": False})
        return "agentcap: statusLine is not AgentCap's; left unchanged."
    prev = info.get("previous")
    if prev:
        data["statusLine"] = prev
    else:
        data.pop("statusLine", None)
    _atomic_write(path, data)
    _atomic_write(cc_dir() / "statusline.json", {"installed": False})
    # The backup is a full copy of settings.json (it can hold env API keys): once the
    # statusLine is restored it has no job left, so it does not outlive the setup.
    backup = path.with_name(path.name + ".agentcap-backup")
    removed = False
    try:
        if backup.is_file() and not backup.is_symlink():
            backup.unlink()
            removed = True
    except OSError:
        pass
    tail = f" Deleted {backup.name}." if removed else ""
    return ("agentcap: statusLine restored." if prev else "agentcap: statusLine removed.") + tail


# ----------------------------------------------------------------- main
def main(argv: list[str] | None = None, stdin: TextIO = sys.stdin, stdout: TextIO = sys.stdout) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    now = time.time()
    if not argv:
        stdout.write(__doc__ or "")
        return 0
    cmd, rest = argv[0], argv[1:]
    try:
        if cmd == "statusline":
            return statusline_main(stdin, stdout, now=now)
        if cmd == "hook":
            raw = stdin.read()
            payload = json.loads(raw) if raw.strip() else {}
            if not isinstance(payload, dict):
                return 0
            event = rest[0] if rest else ""
            if event == "start":
                return hook_start(payload, stdout, now=now)
            if event == "prompt":
                return hook_prompt(payload, stdout, now=now, pro=is_pro())
            if event == "pre":
                return hook_pre(payload, stdout, now=now, pro=is_pro())
            if event in ("post", "postfail"):
                return hook_post(payload, stdout, now=now, pro=is_pro(), failed=event == "postfail")
            if event == "stop":
                return hook_stop(payload, stdout, now=now, pro=is_pro())
            return 0
    except Exception as exc:  # hooks fail open
        sys.stderr.write(f"agentcap: {exc}\n")
        return 0
    sid_raw = None
    if "--session" in rest:
        i = rest.index("--session")
        sid_raw = rest[i + 1] if i + 1 < len(rest) else None
        rest = rest[:i] + rest[i + 2:]
    rest = [a for x in rest for a in x.split()]
    sid = resolve_session(sid_raw)
    if cmd == "quota":
        out = cmd_quota(rest, sid, now=now)
    elif cmd == "approve":
        out = cmd_approve(sid)
    elif cmd == "status":
        out = cmd_status(sid)
    elif cmd == "off":
        out = cmd_off(sid)
    elif cmd == "setup":
        out = cmd_setup(now=now)
    elif cmd == "unsetup":
        out = cmd_unsetup()
    elif cmd == "default":
        out = cmd_default(rest, now=now)
    elif cmd == "usage-probe":
        cmd_usage_probe(sid, now=now)
        return 0
    else:
        out = f"agentcap: unknown command {cmd!r}"
    stdout.write(out + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
