"""`agentcap waste` (Free): where the tokens went that you did not need to spend.

Reads Claude Code and Codex session logs read-only and reports:

* subagent / forked-thread share and Opus share of API-equivalent spend
* cache misses: a cache re-write after the prompt cache TTL expired (5 min, or 1 h
  when the cache was written with ``ephemeral_1h``)
* streaks of identical tool calls (hash of tool name + canonical input)
* streaks of failing tool calls (Claude ``is_error``, Codex non-zero exit code)

Every finding carries an API-equivalent $ estimate. Tool inputs are hashed and
never printed. These log formats are undocumented and change between CLI
versions; the numbers are estimates, not a bill.

Not visible in local logs at all: ``agent_summary`` / title background calls and
HTTP-level retries (those need OTel or the proxy).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterator

from .history import _Pricer, _int, _lines, _safe_file, _ts, codex_lineage

REPEAT_MIN = 3          # report streaks from this length
PRO_REPEAT_STOP = 4     # Pro loop breaker: same step 4x
PRO_ERROR_STOP = 8      # Pro loop breaker: 8 tool errors in a row
TTL_5M, TTL_1H = 300, 3600
CODEX_MISS_MIN_TOKENS = 4096  # Codex has no cache writes; only flag big uncached prompts


@dataclass
class CacheMiss:
    source: str
    session: str
    ts: float
    gap_s: float
    tokens: int
    usd: float


@dataclass
class Streak:
    kind: str           # repeat | error
    source: str
    session: str
    agent: str | None
    tool: str
    count: int
    usd: float          # API-equivalent $ of the calls in the streak
    run_usd_at_stop: float | None = None  # run spend when Pro would have stopped it
    stop_at_usd: float | None = None      # $ of the streak after that stop point


@dataclass
class WasteReport:
    sessions: int = 0
    total_usd: float = 0.0
    subagent_usd: float = 0.0
    opus_usd: float = 0.0
    subagent_threads: int = 0
    cache_misses: list[CacheMiss] = field(default_factory=list)
    repeat_streaks: list[Streak] = field(default_factory=list)
    error_streaks: list[Streak] = field(default_factory=list)

    @property
    def subagent_share(self) -> float:
        return self.subagent_usd / self.total_usd if self.total_usd else 0.0

    @property
    def opus_share(self) -> float:
        return self.opus_usd / self.total_usd if self.total_usd else 0.0

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["subagent_share"] = self.subagent_share
        d["opus_share"] = self.opus_share
        return d


@dataclass
class _Call:      # one tool call
    name: str
    digest: str
    ts: float
    cost: float = 0.0
    error: bool = False
    run_usd: float = 0.0  # cumulative stream spend after this call's model request


@dataclass
class _Stream:    # one session or one subagent/forked thread
    source: str
    session: str
    agent: str | None = None
    parent: str | None = None
    usd: float = 0.0
    calls: list[_Call] = field(default_factory=list)
    by_id: dict[str, _Call] = field(default_factory=dict)


def _digest(name: str, payload: Any) -> str:
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except ValueError:
            pass
    canon = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(f"{name}\0{canon}".encode()).hexdigest()[:16]


def _price(pricer: _Pricer, model: str):
    if pricer.store is None or not model:
        return None
    for cand in (model, f"openai/{model}", f"anthropic/{model}"):
        try:
            return pricer.store.get(cand)
        except Exception:  # noqa: BLE001 - PricingError or lookup issues
            continue
    return None


def _f(value: str | None) -> float:
    return float(value) if value is not None else 0.0


# --------------------------------------------------------------------- claude
def _claude_file(path: Path, pricer: _Pricer, since: float | None, rep: WasteReport,
                 streams: dict[tuple, _Stream]) -> None:
    file_agent = file_parent = None
    if path.parent.name == "subagents" and path.stem.startswith("agent-"):
        file_agent, file_parent = path.stem[len("agent-"):], path.parent.parent.name
    msgs: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    results: list[tuple[str, bool]] = []
    for obj in _lines(path):
        if obj is None:
            continue
        ts = _ts(obj.get("timestamp"))
        if ts is None or (since is not None and ts < since):
            continue
        msg = obj.get("message")
        if not isinstance(msg, dict):
            continue
        side = bool(obj.get("isSidechain")) or file_agent is not None
        agent = (obj.get("agentId") or file_agent or "sidechain") if side else None
        sess = str(obj.get("sessionId") or file_parent or path.stem)
        skey = ("claude", sess, agent)
        if skey not in streams:
            streams[skey] = _Stream("claude", sess, agent, sess if side else None)
        if obj.get("type") == "user" and isinstance(msg.get("content"), list):
            for block in msg["content"]:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    results.append((str(block.get("tool_use_id")), bool(block.get("is_error"))))
            continue
        if obj.get("type") != "assistant" or not isinstance(msg.get("usage"), dict):
            continue
        model = str(msg.get("model") or "")
        if model == "<synthetic>":
            continue
        key = f"{msg.get('id') or obj.get('uuid')}:{obj.get('requestId') or ''}"
        m = msgs.get(key)
        if m is None:  # one message can span several lines (one content block each)
            m = msgs[key] = {"ts": ts, "model": model, "skey": skey, "tools": [], "seen": set()}
            order.append(key)
        m["usage"] = msg["usage"]  # keep the final usage snapshot
        for block in msg.get("content") or []:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                tid = str(block.get("id"))
                if tid not in m["seen"]:
                    m["seen"].add(tid)
                    m["tools"].append((tid, str(block.get("name") or "?"), block.get("input")))
    last: dict[tuple, dict[str, Any]] = {}
    for key in order:
        m = msgs[key]
        st = streams[m["skey"]]
        u = m["usage"]
        inp, cr = _int(u.get("input_tokens")), _int(u.get("cache_read_input_tokens"))
        cw, out = _int(u.get("cache_creation_input_tokens")), _int(u.get("output_tokens"))
        usd = _f(pricer.usd(m["model"], inp, cr, cw, out))
        _account(rep, st, m["model"], usd)
        cc = u.get("cache_creation") if isinstance(u.get("cache_creation"), dict) else {}
        prev = last.get(m["skey"])
        if prev is not None and cw > 0 and cw > cr:
            gap = m["ts"] - prev["ts"]
            ttl = TTL_1H if prev["h1"] else TTL_5M
            if gap > ttl:
                price = _price(pricer, m["model"])
                extra = 0.0
                if price is not None:
                    wp = price.cache_write_per_mtok or price.input_per_mtok
                    extra = float((Decimal(cw) * (wp - price.cached_input_per_mtok)) / Decimal(1_000_000))
                rep.cache_misses.append(CacheMiss("claude", st.session, m["ts"], gap, cw, extra))
        last[m["skey"]] = {"ts": m["ts"],
                           "h1": _int(cc.get("ephemeral_1h_input_tokens")) > 0
                           or (prev is not None and prev["h1"] and m["ts"] - prev["ts"] <= TTL_1H)}
        tools = m["tools"]
        for tid, name, payload in tools:
            call = _Call(name, _digest(name, payload), m["ts"], usd / len(tools), run_usd=st.usd)
            st.calls.append(call)
            st.by_id[tid] = call
    for tid, err in results:
        for st in streams.values():
            if tid in st.by_id:
                st.by_id[tid].error = err
                break


# ---------------------------------------------------------------------- codex
def _codex_error(output: Any) -> bool:
    if isinstance(output, str):
        try:
            data = json.loads(output)
        except ValueError:
            first = output.lstrip().splitlines()[0] if output.strip() else ""
            if first.lower().startswith(("exit code:", "process exited with code")):
                code = first.rsplit(" ", 1)[-1].strip()
                return code.lstrip("-").isdigit() and int(code) != 0
            return False
        output = data
    if isinstance(output, dict):
        meta = output.get("metadata") if isinstance(output.get("metadata"), dict) else output
        code = meta.get("exit_code")
        if isinstance(code, int) and not isinstance(code, bool):
            return code != 0
        if output.get("success") is False:
            return True
    return False


def _codex_file(path: Path, pricer: _Pricer, since: float | None, rep: WasteReport,
                streams: dict[tuple, _Stream]) -> None:
    st: _Stream | None = None
    model = ""
    last_total = None
    last_ts: float | None = None
    for obj in _lines(path):
        if obj is None:
            continue
        payload = obj.get("payload")
        if not isinstance(payload, dict):
            continue
        kind = obj.get("type")
        if kind == "session_meta":
            agent, parent = codex_lineage(payload)
            sess = str(payload.get("id") or path.stem)
            st = streams.setdefault(("codex", sess, None),
                                    _Stream("codex", sess, agent if parent else None, parent))
            continue
        if kind == "turn_context":
            model = str(payload.get("model") or model)
            continue
        ts = _ts(obj.get("timestamp"))
        if st is None or ts is None or (since is not None and ts < since):
            continue
        ptype = payload.get("type")
        if kind == "response_item" and ptype in ("function_call", "custom_tool_call", "local_shell_call"):
            name = str(payload.get("name") or ptype)
            args = payload.get("arguments", payload.get("input", payload.get("action")))
            call = _Call(name, _digest(name, args), ts, run_usd=st.usd)
            st.calls.append(call)
            if payload.get("call_id"):
                st.by_id[str(payload["call_id"])] = call
            continue
        if kind == "response_item" and ptype in ("function_call_output", "custom_tool_call_output"):
            call = st.by_id.get(str(payload.get("call_id")))
            if call is not None:
                call.error = _codex_error(payload.get("output"))
            continue
        if kind != "event_msg" or ptype != "token_count":
            continue
        info = payload.get("info")
        if not isinstance(info, dict) or not isinstance(info.get("last_token_usage"), dict):
            continue
        marker = json.dumps(info.get("total_token_usage"), sort_keys=True)
        if marker == last_total:
            continue
        last_total = marker
        u = info["last_token_usage"]
        inp = _int(u.get("input_tokens"))
        cr = min(_int(u.get("cached_input_tokens")), inp)
        cw = min(_int(u.get("cache_write_input_tokens")), inp - cr)
        usd = _f(pricer.usd(model, inp - cr - cw, cr, cw, _int(u.get("output_tokens"))))
        _account(rep, st, model, usd)
        if st.calls and st.calls[-1].cost == 0.0 and st.calls[-1].ts <= ts:
            pending = [c for c in st.calls if c.cost == 0.0 and c.run_usd == st.calls[-1].run_usd]
            for c in pending:  # the request that produced these calls
                c.cost = usd / len(pending)
                c.run_usd = st.usd
        uncached = inp - cr
        if last_ts is not None and ts - last_ts > TTL_5M and cr == 0 and uncached >= CODEX_MISS_MIN_TOKENS:
            price = _price(pricer, model)
            extra = 0.0
            if price is not None:
                extra = float(Decimal(uncached) * (price.input_per_mtok - price.cached_input_per_mtok)
                              / Decimal(1_000_000))
            rep.cache_misses.append(CacheMiss("codex", st.session, ts, ts - last_ts, uncached, extra))
        last_ts = ts


def _account(rep: WasteReport, st: _Stream, model: str, usd: float) -> None:
    st.usd += usd
    rep.total_usd += usd
    if st.parent:
        rep.subagent_usd += usd
    if "opus" in model.lower():
        rep.opus_usd += usd


# -------------------------------------------------------------------- streaks
def _streaks(st: _Stream) -> Iterator[Streak]:
    calls = st.calls

    def runs(same) -> Iterator[list[_Call]]:
        i = 0
        while i < len(calls):
            j = i + 1
            while j < len(calls) and same(calls[i], calls[j]):
                j += 1
            yield calls[i:j]
            i = j

    for run in runs(lambda a, b: a.digest == b.digest):
        if len(run) >= REPEAT_MIN:
            s = Streak("repeat", st.source, st.session, st.agent, run[0].name, len(run),
                       sum(c.cost for c in run))
            if len(run) > PRO_REPEAT_STOP:
                s.run_usd_at_stop = run[PRO_REPEAT_STOP - 1].run_usd
                s.stop_at_usd = sum(c.cost for c in run[PRO_REPEAT_STOP:])
            elif len(run) == PRO_REPEAT_STOP:
                s.run_usd_at_stop, s.stop_at_usd = run[-1].run_usd, 0.0
            yield s
    for run in runs(lambda a, b: a.error and b.error):
        if run[0].error and len(run) >= REPEAT_MIN:
            names = sorted({c.name for c in run})
            s = Streak("error", st.source, st.session, st.agent, "/".join(names)[:40], len(run),
                       sum(c.cost for c in run))
            if len(run) >= PRO_ERROR_STOP:
                s.run_usd_at_stop = run[PRO_ERROR_STOP - 1].run_usd
                s.stop_at_usd = sum(c.cost for c in run[PRO_ERROR_STOP:])
            yield s


def scan(home: Path | None = None, since: float | None = None,
         claude_dir: Path | None = None, codex_dir: Path | None = None) -> WasteReport:
    home = Path(home) if home is not None else Path.home()
    claude_dir = claude_dir or home / ".claude" / "projects"
    codex_dir = codex_dir or home / ".codex" / "sessions"
    pricer = _Pricer()
    rep = WasteReport()
    streams: dict[tuple, _Stream] = {}
    if claude_dir.is_dir():
        for path in sorted(claude_dir.rglob("*.jsonl")):
            if _safe_file(path):
                _claude_file(path, pricer, since, rep, streams)
    if codex_dir.is_dir():
        for path in sorted(codex_dir.rglob("rollout-*.jsonl")):
            if _safe_file(path):
                _codex_file(path, pricer, since, rep, streams)
    live = [s for s in streams.values() if s.usd > 0 or s.calls]
    rep.sessions = len({(s.source, s.parent or s.session) for s in live})
    rep.subagent_threads = sum(1 for s in live if s.parent)
    for st in live:
        for s in _streaks(st):
            (rep.repeat_streaks if s.kind == "repeat" else rep.error_streaks).append(s)
    rep.repeat_streaks.sort(key=lambda s: (-s.count, -s.usd))
    rep.error_streaks.sort(key=lambda s: (-s.count, -s.usd))
    rep.cache_misses.sort(key=lambda m: -m.usd)
    return rep


# --------------------------------------------------------------------- render
def _m(x: float) -> str:
    return f"${x:,.2f}"


def _sid(s: str) -> str:
    return s[:8]


def _who(s: Streak) -> str:
    return f"{_sid(s.session)}{'/' + s.agent[:12] if s.agent else ''}"


def render(rep: WasteReport, period: str, top: int = 3) -> str:
    out = [f"agentcap waste - {period} - {rep.sessions} sessions - ${rep.total_usd:,.2f} API-equivalent",
           "(local Claude Code / Codex logs, read-only; $ are estimates at API list prices)", ""]
    out.append(f"SUBAGENTS            {_m(rep.subagent_usd):>10}  {rep.subagent_share * 100:4.0f}% of spend"
               f"  in {rep.subagent_threads} subagent/forked threads")
    out.append(f"OPUS                 {_m(rep.opus_usd):>10}  {rep.opus_share * 100:4.0f}% of spend")
    cm = sum(m.usd for m in rep.cache_misses)
    out.append(f"CACHE MISSES         {_m(cm):>10}  {len(rep.cache_misses)} cache re-writes after the TTL ran out"
               " (5 min, or 1 h)")
    for m in rep.cache_misses[:top]:
        out.append(f"   {m.source:<6} {_sid(m.session):<8}  idle {m.gap_s / 60:5.0f} min  "
                   f"{m.tokens / 1000:7.1f}k tok rewritten  ${m.usd:.2f}")
    rp = sum(s.usd for s in rep.repeat_streaks)
    out.append(f"REPEATED TOOL CALLS  {_m(rp):>10}  {len(rep.repeat_streaks)} streaks of >= {REPEAT_MIN}"
               " identical calls in a row")
    for s in rep.repeat_streaks[:top]:
        out.append(f"   {s.source:<6} {_who(s):<21} {s.tool[:18]:<18} x{s.count:<4} ${s.usd:.2f}")
    er = sum(s.usd for s in rep.error_streaks)
    out.append(f"TOOL ERROR STREAKS   {_m(er):>10}  {len(rep.error_streaks)} streaks of >= {REPEAT_MIN}"
               " failing calls in a row")
    for s in rep.error_streaks[:top]:
        out.append(f"   {s.source:<6} {_who(s):<21} {s.tool[:18]:<18} x{s.count:<4} ${s.usd:.2f}")
    out.append("")
    out.append("Not in local logs: agent_summary/title background calls, HTTP retries (need OTel or the proxy).")
    out.append(pro_nudge(rep))
    return "\n".join(out)


def pro_nudge(rep: WasteReport) -> str:
    stoppable = [s for s in rep.repeat_streaks + rep.error_streaks if s.run_usd_at_stop is not None]
    if stoppable:
        s = max(stoppable, key=lambda s: (s.stop_at_usd or 0.0, s.count))
        n = PRO_REPEAT_STOP if s.kind == "repeat" else PRO_ERROR_STOP
        what = "repeats" if s.kind == "repeat" else "failed calls in a row"
        return (f"Pro would have stopped this run at ${s.run_usd_at_stop:.2f} / at {n} {what} "
                f"({s.tool[:18]} x{s.count} in {_who(s)}, ${s.stop_at_usd or 0:.2f} after that).")
    return (f"Pro stops a run at {PRO_REPEAT_STOP} identical repeats or {PRO_ERROR_STOP} tool errors"
            " in a row - none of these runs got that far.")


def render_json(rep: WasteReport) -> str:
    return json.dumps(rep.to_dict(), indent=2)
