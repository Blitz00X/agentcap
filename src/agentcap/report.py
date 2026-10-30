"""Free usage report: group imported history by day/session/model/project."""

from __future__ import annotations

import csv
import io
import json
import time
from datetime import datetime
from decimal import Decimal
from typing import Any, Iterable

GROUPS = ("day", "session", "model", "project", "branch", "source", "repo", "job", "agent")
FIELDS = ("requests", "input_tokens", "output_tokens", "cache_read_tokens",
          "cache_write_tokens", "usd", "unpriced")


def parse_since(value: str | None, now: float | None = None) -> float | None:
    """``all`` / ``7d`` / ``24h`` / ``2026-10-01`` -> epoch seconds (or None)."""

    if value is None:
        value = "30d"
    value = value.strip().lower()
    if value in ("all", ""):
        return None
    now = time.time() if now is None else now
    if value[-1] in "dh" and value[:-1].isdigit():
        n = int(value[:-1])
        return now - n * (86400 if value[-1] == "d" else 3600)
    try:
        return datetime.fromisoformat(value).timestamp()
    except ValueError as exc:
        raise ValueError(f"bad --since value: {value!r} (use all, 7d, 24h or YYYY-MM-DD)") from exc


def _key(row: dict[str, Any], by: str) -> str:
    if by == "day":
        return datetime.fromtimestamp(row["ts"]).strftime("%Y-%m-%d")
    if by in ("session", "job"):
        return str(row.get("session_id") or "-")
    if by == "agent":
        return str(row.get("agent_id") or "main")
    if by in ("project", "repo"):
        return str(row.get("project") or "-")
    return str(row.get(by) or "-")


def aggregate(rows: Iterable[dict[str, Any]], by: str) -> list[dict[str, Any]]:
    if by not in GROUPS:
        raise ValueError(f"unknown group {by!r}")
    out: dict[str, dict[str, Any]] = {}
    for row in rows:
        k = _key(row, by)
        agg = out.setdefault(k, {"key": k, **{f: 0 for f in FIELDS}, "usd": Decimal(0)})
        agg["requests"] += 1
        for f in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens"):
            agg[f] += int(row.get(f) or 0)
        if row.get("usd") is None:
            agg["unpriced"] += 1
        else:
            agg["usd"] += Decimal(row["usd"])
    ordered = sorted(out.values(), key=lambda a: a["key"] if by == "day" else -a["usd"])
    return ordered


def _fmt_tokens(n: int) -> str:
    for unit, size in (("B", 10**9), ("M", 10**6), ("k", 10**3)):
        if n >= size:
            return f"{n / size:.1f}{unit}"
    return str(n)


def render_text(rows: list[dict[str, Any]], by: str) -> str:
    head = [by.upper(), "REQ", "INPUT", "OUTPUT", "CACHE READ", "CACHE WRITE", "API $"]
    body = [
        [r["key"][:48], str(r["requests"]), _fmt_tokens(r["input_tokens"]),
         _fmt_tokens(r["output_tokens"]), _fmt_tokens(r["cache_read_tokens"]),
         _fmt_tokens(r["cache_write_tokens"]),
         f"{r['usd']:.2f}" + ("*" if r["unpriced"] else "")]
        for r in rows
    ]
    total = aggregate_total(rows)
    body.append(["TOTAL", str(total["requests"]), _fmt_tokens(total["input_tokens"]),
                 _fmt_tokens(total["output_tokens"]), _fmt_tokens(total["cache_read_tokens"]),
                 _fmt_tokens(total["cache_write_tokens"]), f"{total['usd']:.2f}"])
    widths = [max(len(x[i]) for x in [head] + body) for i in range(len(head))]
    lines = ["  ".join(c.ljust(w) if i == 0 else c.rjust(w) for i, (c, w) in enumerate(zip(r, widths)))
             for r in [head] + body]
    lines.insert(1, "-" * len(lines[0]))
    lines.insert(len(lines) - 1, "-" * len(lines[0]))
    lines.append("$ is API-equivalent list price (what these tokens would cost on the API),"
                 " not your subscription bill.")
    if total["unpriced"]:
        lines.append(f"* {total['unpriced']} request(s) use a model with no bundled price.")
    return "\n".join(lines)


def aggregate_total(rows: list[dict[str, Any]]) -> dict[str, Any]:
    t: dict[str, Any] = {f: 0 for f in FIELDS}
    t["usd"] = Decimal(0)
    for r in rows:
        for f in FIELDS:
            t[f] += r[f]
    return t


def render_json(rows: list[dict[str, Any]], by: str) -> str:
    def conv(r: dict[str, Any]) -> dict[str, Any]:
        return {**r, "usd": str(r["usd"])}
    return json.dumps({"by": by, "usd_basis": "api_equivalent",
                       "rows": [conv(r) for r in rows],
                       "total": conv(aggregate_total(rows))}, indent=2)


def render_csv(rows: list[dict[str, Any]]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(["key", *FIELDS])
    for r in rows:
        w.writerow([r["key"], *[str(r[f]) for f in FIELDS]])
    return buf.getvalue()


# ------------------------------------------------------------- subagent tree
def _usd(row: dict[str, Any]) -> Decimal:
    return Decimal(row["usd"]) if row.get("usd") is not None else Decimal(0)


def is_opus(model: Any) -> bool:
    return "opus" in str(model or "").lower()


def agent_tree(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Group events into root sessions with their subagents / forks as children.

    Claude subagents share the parent's ``sessionId``; Codex children are their own
    threads pointing at ``parent_session``. Both end up under the root session.
    """

    roots: dict[str, dict[str, Any]] = {}

    def root(sid: str) -> dict[str, Any]:
        return roots.setdefault(sid, {"session": sid, "source": None, "own_usd": Decimal(0),
                                      "total_usd": Decimal(0), "opus_usd": Decimal(0),
                                      "requests": 0, "children": {}})

    for row in rows:
        parent = row.get("parent_session")
        agent = row.get("agent_id")
        sid = str(parent or row.get("session_id") or "-")
        node = root(sid)
        node["source"] = node["source"] or row.get("source")
        usd = _usd(row)
        node["total_usd"] += usd
        node["requests"] += 1
        if is_opus(row.get("model")):
            node["opus_usd"] += usd
        if parent:
            key = f"{agent or 'subagent'}" + (
                "" if row.get("source") == "claude" else f" ({row.get('session_id')})")
            kid = node["children"].setdefault(key, {"agent": str(agent or "subagent"),
                                                    "thread": row.get("session_id"),
                                                    "usd": Decimal(0), "requests": 0,
                                                    "models": set()})
            kid["usd"] += usd
            kid["requests"] += 1
            kid["models"].add(str(row.get("model") or "?"))
        else:
            node["own_usd"] += usd
    out = []
    for node in roots.values():
        total = node["total_usd"]
        node["opus_share"] = float(node["opus_usd"] / total) if total else 0.0
        node["sub_share"] = float((total - node["own_usd"]) / total) if total else 0.0
        kids = sorted(node["children"].values(), key=lambda k: -k["usd"])
        for k in kids:
            k["models"] = sorted(k["models"])
        node["children"] = kids
        out.append(node)
    return sorted(out, key=lambda n: -n["total_usd"])


def render_agent_tree(tree: list[dict[str, Any]], limit: int = 15) -> str:
    total = sum((n["total_usd"] for n in tree), Decimal(0))
    sub = sum((n["total_usd"] - n["own_usd"] for n in tree), Decimal(0))
    opus = sum((n["opus_usd"] for n in tree), Decimal(0))
    pct = (lambda a: f"{float(a / total) * 100:.0f}%" if total else "0%")
    lines = [f"Sessions by agent (API-equivalent $)   total ${total:.2f} | "
             f"subagents {pct(sub)} | Opus {pct(opus)}", ""]
    for node in tree[:limit]:
        lines.append(f"{node['session'][:36]:<36} {node['source'] or '':<6} {'$' + format(node['total_usd'], ',.2f'):>10}"
                     f"  main ${node['own_usd']:.2f}  subagents {node['sub_share'] * 100:.0f}%"
                     f"  Opus {node['opus_share'] * 100:.0f}%")
        for i, k in enumerate(node["children"]):
            branch = "└─" if i == len(node["children"]) - 1 else "├─"
            lines.append(f"  {branch} {k['agent'][:30]:<30} {'$' + format(k['usd'], ',.2f'):>10}  {k['requests']:>4} calls  "
                         f"{', '.join(k['models'])}")
    if len(tree) > limit:
        lines.append(f"... {len(tree) - limit} more sessions (--json for all)")
    return "\n".join(lines)


def render_agent_json(tree: list[dict[str, Any]]) -> str:
    return json.dumps(tree, default=lambda o: str(o) if isinstance(o, Decimal) else list(o), indent=2)
