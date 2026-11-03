"""Harness adapters: one engine, per-harness log readers and stoppers.

The engine (``HarnessRunner``) runs a headless agent, reads its machine-readable
event stream, and enforces caps: token cap, USD cap from the harness's own cost
field, subscription quota % (only where the harness exposes it), the loop breaker
(same tool call 4x) and the error breaker (8 failing tool calls in a row).  The
stopper is SIGINT to the process group (headless runs end their turn on SIGINT).

Adapters:

* ``opencode`` -- ``opencode run --format json``: ``tool_use`` and
  ``step_finish`` (tokens + cost) events.  opencode does not expose
  subscription quota % (opencode Go's usage API needs the stored key, which
  AgentCap never reads), so quota mode is refused and ``--tokens``/
  ``--log-cap`` caps are offered instead.
* ``agy`` (Antigravity CLI) -- ``agy -p --output-format stream-json``:
  ``step_update`` tool/agent_response events.  Quota % comes from
  ``agy -p /quota --output-format json`` (``remaining_fraction`` per model
  group and window), which does not start an agent turn or spend quota.
"""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence, TextIO

from .procstop import reap_group, stop_tree
from .quota import LoopWatch, WARN_FRACTION, _atomic_write, state_root

EXIT_CAP_EXCEEDED = 3
EXIT_LOOP_DETECTED = 4


@dataclass(frozen=True)
class ToolCall:
    name: str
    args: Any
    error: bool = False


@dataclass(frozen=True)
class Usage:
    tokens: int
    cost: float | None = None


@dataclass(frozen=True)
class ApiError:
    kind: str


def _name(token: str) -> str:
    return os.path.basename(str(token)).lower()


def detect_harness(argv: Sequence[str]) -> str | None:
    if not argv:
        return None
    name = _name(argv[0])
    if name == "opencode":
        return "opencode"
    if name == "agy":
        return "agy"
    return None


def _num(v: Any) -> int:
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


class OpenCodeAdapter:
    name = "opencode"
    exposes_quota = False

    def argv(self, argv: Sequence[str]) -> list[str]:
        argv = list(argv)
        if len(argv) < 2 or argv[1] != "run":
            raise ValueError("AgentCap needs headless opencode: `opencode run ...`")
        if "--format" not in argv:
            argv[2:2] = ["--format", "json"]
        return argv

    def parse(self, obj: Any) -> list[Any]:
        if not isinstance(obj, dict):
            return []
        kind = obj.get("type")
        part = obj.get("part") if isinstance(obj.get("part"), dict) else {}
        if kind == "tool_use":
            st = part.get("state") if isinstance(part.get("state"), dict) else {}
            meta = (st.get("metadata") or {}).get("metadata") if isinstance(st.get("metadata"), dict) else None
            exit_code = meta.get("exit") if isinstance(meta, dict) else None
            error = st.get("status") == "error" or (isinstance(exit_code, int) and exit_code != 0)
            return [ToolCall(str(part.get("tool") or "tool"), st.get("input"), error=error)]
        if kind == "step_finish":
            t = part.get("tokens") if isinstance(part.get("tokens"), dict) else {}
            cache = t.get("cache") if isinstance(t.get("cache"), dict) else {}
            tokens = (_num(t.get("input")) + _num(t.get("output")) + _num(t.get("reasoning"))
                      + _num(cache.get("read")) + _num(cache.get("write")))
            cost = part.get("cost")
            return [Usage(tokens, float(cost) if isinstance(cost, (int, float)) else None)]
        if kind == "error":
            err = obj.get("error") if isinstance(obj.get("error"), dict) else {}
            return [ApiError(str(err.get("type") or "error"))]
        return []


_EXIT_RE = re.compile(r"exited with code (\d+)", re.I)


class AgyAdapter:
    name = "agy"
    exposes_quota = True

    def argv(self, argv: Sequence[str]) -> list[str]:
        argv = list(argv)
        if not any(a in ("-p", "--print", "--prompt") for a in argv[1:]):
            raise ValueError("AgentCap needs headless agy: `agy -p \"...\"`")
        if "--output-format" not in argv:
            argv[1:1] = ["--output-format", "stream-json"]
        return argv

    def parse(self, obj: Any) -> list[Any]:
        if not isinstance(obj, dict):
            return []
        if obj.get("event") == "result":
            res = obj.get("result") if isinstance(obj.get("result"), dict) else {}
            if res.get("status") not in (None, "SUCCESS"):
                return [ApiError(str(res.get("status")))]
            return []
        su = obj.get("step_update")
        if obj.get("event") != "step_update" or not isinstance(su, dict) or su.get("state") != "DONE":
            return []
        if su.get("step_type") == "tool":
            info = su.get("tool_info") if isinstance(su.get("tool_info"), dict) else {}
            out = str(info.get("output") or "")
            m = _EXIT_RE.search(out)
            error = bool(su.get("error")) or (m is not None and m.group(1) != "0")
            return [ToolCall(str(info.get("name") or su.get("tool_name") or "tool"),
                             info.get("parameters"), error=error)]
        usage = su.get("usage")
        if isinstance(usage, dict):
            return [Usage(_num(usage.get("total_tokens")), None)]
        return []


ADAPTERS: dict[str, Callable[[], Any]] = {"opencode": OpenCodeAdapter, "agy": AgyAdapter}

_AGY_WINDOWS = {"week": "weekly", "5h": "5h"}


def _agy_group(model: str) -> str:
    m = model.lower()
    if "argon" in m:
        return "argon"
    if "claude" in m or "gpt" in m:
        return "claude and gpt"
    return "gemini models"


def agy_used_pct(payload: Mapping[str, Any], model: str, window: str) -> float | None:
    """Used % of the model's group bucket from ``agy -p /quota --output-format json``."""

    try:
        groups = payload["command"]["data"]["groups"]
    except (KeyError, TypeError):
        return None
    want = _agy_group(model)
    win = _AGY_WINDOWS.get(window, window)
    for g in groups if isinstance(groups, list) else []:
        if not isinstance(g, dict) or want not in str(g.get("name", "")).lower():
            continue
        for b in g.get("buckets") or []:
            if isinstance(b, dict) and b.get("window") == win:
                try:
                    return round((1.0 - float(b["remaining_fraction"])) * 100.0, 4)
                except (KeyError, TypeError, ValueError):
                    return None
    return None


def agy_model(argv: Sequence[str], home: Path | None = None) -> str:
    argv = list(argv)
    if "--model" in argv and argv.index("--model") + 1 < len(argv):
        return argv[argv.index("--model") + 1]
    path = (home or Path.home()) / ".gemini" / "antigravity-cli" / "settings.json"
    try:
        return str(json.loads(path.read_text(encoding="utf-8")).get("model") or "gemini")
    except (OSError, ValueError, AttributeError):
        return "gemini"


def agy_quota_payload(exe: str, timeout: float = 60.0) -> dict[str, Any] | None:
    try:
        out = subprocess.run([exe, "-p", "/quota", "--output-format", "json"], capture_output=True,
                             text=True, timeout=timeout, cwd=str(Path.home()))
        return json.loads(out.stdout)
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


@dataclass
class HarnessSummary:
    agent: str
    exit_code: int
    reason: str
    tokens: int = 0
    cost: float | None = None
    share: float | None = None
    tool_calls: int = 0
    tool_errors: int = 0
    last_tool: str | None = None
    notes: list[str] = field(default_factory=list)


class HarnessRunner:
    def __init__(
        self,
        argv: Sequence[str],
        *,
        tokens: int | None = None,
        usd: float | None = None,
        quota: float | None = None,
        window: str = "week",
        enforce: bool = True,
        alerts: Callable[[Mapping[str, Any]], None] | None = None,
        stderr: TextIO | None = None,
        stdout: TextIO | None = None,
        poll_s: float = 0.2,
        quota_poll_s: float = 20.0,
        receipt_dir: Path | None = None,
        home: Path | None = None,
        quiet: bool = False,
        quota_fetch: Callable[[], Mapping[str, Any] | None] | None = None,
        breakers_only: bool = False,
        int_timeout: float = 5.0,
        term_timeout: float = 3.0,
    ) -> None:
        kind = detect_harness(argv)
        if kind is None:
            raise ValueError("harness mode supports `opencode` and `agy`")
        self.kind = kind
        self.adapter = ADAPTERS[kind]()
        if quota is not None and not self.adapter.exposes_quota:
            raise ValueError(
                f"{kind} does not expose subscription quota %; use --tokens N (token cap from its "
                "own logs) or --log-cap USD (its own cost field) instead"
            )
        if tokens is None and usd is None and quota is None and not breakers_only:
            raise ValueError("give --tokens, --log-cap or --quota")
        self.argv = self.adapter.argv(argv)
        self.tokens_cap, self.usd_cap, self.quota = tokens, usd, quota
        self.window = window
        self.enforce = enforce
        self.alerts = alerts or (lambda e: None)
        self.stderr = stderr or sys.stderr
        self.stdout = stdout or sys.stdout
        self.poll_s, self.quota_poll_s = poll_s, quota_poll_s
        self.receipt_dir = receipt_dir
        self.home = home
        self.quiet = quiet
        self.int_timeout = int_timeout
        self.term_timeout = term_timeout
        self.model = agy_model(self.argv, home) if kind == "agy" else ""
        exe = self.argv[0]
        self.quota_fetch = quota_fetch or (lambda: agy_quota_payload(exe))
        self.job_id = f"h{int(time.time())}-{os.getpid()}"
        self.child: subprocess.Popen | None = None
        self._pgid: int | None = None
        self.summary: HarnessSummary | None = None
        self._lock = threading.Lock()
        self._lines: list[str] = []

    # ------------------------------------------------------------ helpers
    def _say(self, msg: str) -> None:
        if not self.quiet:
            print(msg, file=self.stderr, flush=True)

    def _event(self, kind: str, **extra: Any) -> None:
        if not self.enforce:
            return
        try:
            self.alerts({"type": kind, "run_id": self.job_id, "agent": self.kind, **extra})
        except Exception:
            pass

    def _signal(self, sig: int) -> None:
        c = self.child
        if c is None or c.poll() is not None:
            return
        try:
            os.killpg(os.getpgid(c.pid), sig)
        except OSError:
            try:
                c.send_signal(sig)
            except OSError:
                pass

    def _stop(self) -> None:
        if self.child is not None:
            stop_tree(self.child, self._pgid, int_timeout=self.int_timeout,
                      term_timeout=self.term_timeout, kill_timeout=2.0)

    def _reader(self) -> None:
        assert self.child is not None and self.child.stdout is not None
        for line in self.child.stdout:
            try:
                self.stdout.write(line)
                self.stdout.flush()
            except (OSError, ValueError):
                pass
            with self._lock:
                self._lines.append(line)

    def _share(self, baseline: float | None) -> float | None:
        payload = self.quota_fetch()
        used = agy_used_pct(payload, self.model, self.window) if payload else None
        if used is None or baseline is None:
            return None
        return max(0.0, used - baseline)

    # ------------------------------------------------------------------ run
    def run(self) -> int:
        baseline = None
        if self.quota is not None:
            payload = self.quota_fetch()
            baseline = agy_used_pct(payload, self.model, self.window) if payload else None
            if baseline is None:
                self._say("agentcap: agy reported no quota for this model/window; quota cap is "
                          "not active (token/USD caps and breakers still are).")
        self.child = subprocess.Popen(self.argv, stdout=subprocess.PIPE, text=True, bufsize=1,
                                      start_new_session=True)
        try:
            self._pgid = os.getpgid(self.child.pid)
        except OSError:
            self._pgid = None
        reader = threading.Thread(target=self._reader, daemon=True)
        reader.start()
        loop = LoopWatch()
        tokens, cost, share = 0, None, None
        calls = errors = 0
        last_tool: str | None = None
        reason, hit, warned = "exited", None, False
        next_quota = time.time() + self.quota_poll_s
        try:
            while True:
                code = self.child.poll()
                with self._lock:
                    lines, self._lines = self._lines, []
                for line in lines:
                    try:
                        obj = json.loads(line)
                    except ValueError:
                        continue
                    for ev in self.adapter.parse(obj):
                        if isinstance(ev, ToolCall):
                            calls += 1
                            errors += int(ev.error)
                            last_tool = ev.name
                            loop.call(ev.name, ev.args)
                            loop.result(ev.error)
                        elif isinstance(ev, Usage):
                            tokens += ev.tokens
                            if ev.cost is not None:
                                cost = (cost or 0.0) + ev.cost
                        elif isinstance(ev, ApiError):
                            if hit is not None or loop.tripped:
                                continue  # the agent reporting our own SIGINT
                            self._say(f"agentcap: {self.kind} reported an API error ({ev.kind}).")
                if self.quota is not None and baseline is not None and (
                        time.time() >= next_quota or code is not None):
                    s = self._share(baseline)
                    if s is not None:
                        share = s if share is None else max(share, s)
                    next_quota = time.time() + self.quota_poll_s
                frac, what = self._usage_fraction(tokens, cost, share)
                if hit is None and not warned and frac >= WARN_FRACTION:
                    warned = True
                    self._say(f"agentcap: WARNING: this job reached 80% of its {what}.")
                    self._event("cap_threshold", threshold=80, what=what)
                if hit is None and frac >= 1.0:
                    hit = what
                    reason = "quota_exceeded" if what.startswith("quota") else (
                        "token_cap" if what.startswith("token") else "usd_cap")
                    if self.enforce:
                        self._say(f"agentcap: {self._cap_text(what)} reached. Stopping the agent.")
                        self._event("quota_exceeded" if reason == "quota_exceeded" else "cap_exceeded",
                                    what=what, tokens=tokens, cost_usd=cost,
                                    share_pct=round(share, 2) if share is not None else None,
                                    quota_pct=self.quota, window=self.window)
                        self._stop()
                if loop.tripped and reason == "exited":
                    reason = "loop_detected"
                    if self.enforce:
                        self._say(f"agentcap: loop breaker: {loop.tripped}. Stopping the agent.")
                        self._event("loop_detected", detail=loop.tripped)
                        self._stop()
                if code is not None:
                    break
                time.sleep(self.poll_s)
        except KeyboardInterrupt:
            self._stop()
            reason = "interrupted"
        code = self.child.wait()
        if reap_group(self._pgid):
            self._say('agentcap: killed leftover processes from the agent process group.')
        reader.join(timeout=2)
        if self.enforce and reason in ("token_cap", "usd_cap", "quota_exceeded"):
            code = EXIT_CAP_EXCEEDED
        elif self.enforce and reason == "loop_detected":
            code = EXIT_LOOP_DETECTED
        self.summary = HarnessSummary(self.kind, code, reason, tokens, cost, share, calls, errors, last_tool)
        self._report(loop.tripped)
        return code

    def _usage_fraction(self, tokens: int, cost: float | None, share: float | None) -> tuple[float, str]:
        best = (0.0, "")
        if self.tokens_cap:
            best = max(best, (tokens / self.tokens_cap, "token cap"))
        if self.usd_cap and cost is not None:
            best = max(best, (cost / self.usd_cap, "USD cap"))
        if self.quota and share is not None:
            best = max(best, (share / self.quota, "quota limit"))
        return best

    def _cap_text(self, what: str) -> str:
        if what == "token cap":
            return f"token cap ({self.tokens_cap:,} tokens)"
        if what == "USD cap":
            return f"USD cap (${self.usd_cap:g}, from {self.kind}'s own cost field)"
        return f"quota limit ({self.quota:g}% of the {self._label()} quota)"

    def _label(self) -> str:
        return "weekly" if self.window == "week" else "5-hour"

    def _report(self, loop_detail: str | None) -> None:
        s = self.summary
        assert s is not None
        parts = [f"{s.tokens:,} tokens"]
        if s.cost is not None:
            parts.append(f"${s.cost:.4f} ({self.kind}'s own cost estimate)")
        if s.share is not None:
            parts.append(f"{s.share:.2f}% of the {self._label()} quota")
        parts.append(f"{s.tool_calls} tool calls, {s.tool_errors} failed")
        self._say(f"agentcap: {self.kind} job used " + ", ".join(parts) + ".")
        if s.last_tool:
            self._say(f"agentcap: last tool call: {s.last_tool[:40]}")
        if not self.enforce:
            if s.reason in ("token_cap", "usd_cap", "quota_exceeded"):
                what = {"token_cap": "token cap", "usd_cap": "USD cap", "quota_exceeded": "quota limit"}[s.reason]
                self._say(f"agentcap: Pro would have stopped this job at its {what}. "
                          "https://agentcap.launchbench.studio/#pricing")
            if loop_detail:
                self._say(f"agentcap: Pro's loop breaker would have stopped this job ({loop_detail}).")
            return
        self._event("harness_receipt", reason=s.reason, exit_code=s.exit_code, tokens=s.tokens)
        if self.receipt_dir is not None:
            try:
                _atomic_write(self.receipt_dir / f"{self.job_id}.json", {
                    "type": "agentcap_harness_receipt", "agent": s.agent, "reason": s.reason,
                    "exit_code": s.exit_code, "tokens": s.tokens, "cost_usd": s.cost,
                    "share_pct": s.share, "window": self.window if self.quota else None,
                    "tool_calls": s.tool_calls, "tool_errors": s.tool_errors, "last_tool": s.last_tool,
                    "caps": {"tokens": self.tokens_cap, "usd": self.usd_cap, "quota_pct": self.quota},
                })
            except OSError:
                pass


__all__ = ["HarnessRunner", "OpenCodeAdapter", "AgyAdapter", "detect_harness", "agy_used_pct",
           "agy_model", "ToolCall", "Usage", "ApiError", "EXIT_CAP_EXCEEDED", "EXIT_LOOP_DETECTED",
           "state_root"]
