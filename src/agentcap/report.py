"""Free usage report: group imported history by day/session/model/project."""

from __future__ import annotations

import csv
import io
import json
import time
from datetime import datetime
from decimal import Decimal
from typing import Any, Iterable

GROUPS = ("day", "session", "model", "project", "branch", "source")
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
    if by == "session":
        return str(row.get("session_id") or "-")
    if by == "project":
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
