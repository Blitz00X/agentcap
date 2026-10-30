"""Pro usage analytics over the imported history (usage.db).

All numbers are local estimates from the user's own logs.  Dollar figures are
API-equivalent list prices, not a subscription bill.
"""

from __future__ import annotations

import csv
import io
import json
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable

from .history import HistoryStore, _Pricer
from .plans import load_plans

RESET_TOLERANCE_S = 600.0
FORECAST_LOOKBACK_S = {"5h": 2 * 3600.0, "week": 24 * 3600.0}


def _same_period(a: dict[str, Any], b: dict[str, Any]) -> bool:
    ra, rb = a.get("resets_at"), b.get("resets_at")
    if ra is None or rb is None:
        return b["used_percent"] >= a["used_percent"]
    return abs(ra - rb) <= RESET_TOLERANCE_S


PERIOD_TOLERANCE_S = {"5h": 1800.0, "week": 6 * 3600.0}
MIN_TREND_PCT = 5.0


def _periods(samples: list[dict[str, Any]], window: str) -> list[list[dict[str, Any]]]:
    """Split samples into quota periods (one per reset).

    ``resets_at`` jitters by minutes between readings, so a new period starts
    only when it moves by more than the tolerance (or when no reset time is
    known and the percentage drops sharply).
    """

    tol = PERIOD_TOLERANCE_S.get(window, RESET_TOLERANCE_S)
    out: list[list[dict[str, Any]]] = []
    anchor: float | None = None
    peak = 0.0
    for s in samples:
        r = s.get("resets_at")
        new = not out
        if not new and r is not None and anchor is not None and abs(r - anchor) > tol:
            new = True
        if not new and r is None and s["used_percent"] < peak - 20:
            new = True
        if new:
            out.append([])
            peak = 0.0
            anchor = r
        elif anchor is None:
            anchor = r
        out[-1].append(s)
        peak = max(peak, s["used_percent"])
    return out


def _rises(period: list[dict[str, Any]]) -> list[tuple[float, float, float]]:
    """(from_ts, to_ts, delta) for every new running maximum.

    Parallel sessions report the same account with lagging values; only a new
    high-water mark is real progress, so stale lower readings are ignored.
    """

    rises = []
    peak = period[0]["used_percent"]
    peak_ts = period[0]["ts"]
    for s in period[1:]:
        if s["used_percent"] > peak:
            rises.append((peak_ts, s["ts"], s["used_percent"] - peak))
            peak, peak_ts = s["used_percent"], s["ts"]
    return rises


def _samples(store: HistoryStore, source: str, window: str) -> list[dict[str, Any]]:
    return [s for s in store.quota_samples(source) if s["window"] == window]


def _when(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%a %d %b %H:%M")


def _tok(n: float) -> str:
    for unit, size in (("B", 1e9), ("M", 1e6), ("k", 1e3)):
        if n >= size:
            return f"{n / size:.1f}{unit}"
    return f"{n:.0f}"


# ---------------------------------------------------------------- forecast
@dataclass
class Forecast:
    source: str
    window: str
    used_percent: float
    rate_per_hour: float
    hits_at: float | None
    resets_at: float | None
    before_reset: bool
    seen_at: float


def forecast(store: HistoryStore, source: str, window: str, *, now: float) -> Forecast | None:
    samples = _samples(store, source, window)
    if not samples:
        return None
    period = _periods(samples, window)[-1]
    last = period[-1]
    lookback = FORECAST_LOOKBACK_S.get(window, 24 * 3600.0)
    recent = [s for s in period if last["ts"] - s["ts"] <= lookback]
    first = recent[0]
    top = max(recent, key=lambda s: (s["used_percent"], -s["ts"]))
    used = top["used_percent"]
    hours = (top["ts"] - first["ts"]) / 3600.0
    rate = (used - first["used_percent"]) / hours if hours > 0 else 0.0
    hits = None
    if rate > 0 and used < 100:
        hits = top["ts"] + (100.0 - used) / rate * 3600.0
    resets = max((s["resets_at"] for s in period if s.get("resets_at")), default=None)
    before = hits is not None and (resets is None or hits < resets)
    last = {**last, "used_percent": used}
    return Forecast(source, window, last["used_percent"], max(rate, 0.0), hits, resets, before, last["ts"])


def render_forecast(items: Iterable[Forecast], *, now: float, warn_hours: float = 2.0) -> str:
    lines = []
    for f in items:
        head = f"{f.source} {f.window:>4}: {f.used_percent:5.1f}% used, +{f.rate_per_hour:.1f}%/h"
        if f.used_percent >= 100:
            reset = f", resets {_when(f.resets_at)}" if f.resets_at else ""
            lines.append(f"{head} -> limit reached{reset}  !! BLOCKED")
        elif f.before_reset and f.hits_at is not None:
            left_h = (f.hits_at - now) / 3600.0
            mark = "  !! EARLY WARNING" if left_h <= warn_hours else ""
            lines.append(f"{head} -> limit hits at {_when(f.hits_at)} (~{max(left_h, 0):.1f}h){mark}")
        elif f.hits_at is not None and f.resets_at is not None:
            lines.append(f"{head} -> resets {_when(f.resets_at)} before the limit; OK")
        else:
            lines.append(f"{head} -> no burn trend yet")
    if not lines:
        return "No quota samples yet. Codex: just use codex; Claude: run `agentcap run --quota` once."
    lines.append("Forecast = straight-line burn over the recent window; account-level, all sessions.")
    return "\n".join(lines)


# ------------------------------------------------------------- calibration
@dataclass
class Calibration:
    period_start: float
    resets_at: float | None
    pct: float
    tokens: int
    usd: Decimal
    unpriced: int = 0

    @property
    def tokens_per_pct(self) -> float:
        return self.tokens / self.pct

    @property
    def usd_per_pct(self) -> Decimal:
        return self.usd / Decimal(str(self.pct))


def calibration(store: HistoryStore, source: str, window: str) -> list[Calibration]:
    samples = _samples(store, source, window)
    events = [e for e in store.events() if e["source"] == source]
    out: list[Calibration] = []
    for period in _periods(samples, window):
        cal = None
        for t0, t1, delta in _rises(period):
            inside = [e for e in events if t0 < e["ts"] <= t1]
            tokens = sum(e["input_tokens"] + e["output_tokens"] + e["cache_read_tokens"]
                         + e["cache_write_tokens"] for e in inside)
            if tokens <= 0:
                continue  # quota moved by usage we cannot see (web, other machine)
            if cal is None:
                cal = Calibration(t0, period[-1].get("resets_at"), 0.0, 0, Decimal(0))
            cal.pct += delta
            cal.tokens += tokens
            cal.usd += sum((Decimal(e["usd"]) for e in inside if e["usd"] is not None), Decimal(0))
            cal.unpriced += sum(1 for e in inside if e["usd"] is None)
        if cal is not None:
            out.append(cal)
    return out


def render_calibration(rows: list[Calibration], source: str, window: str) -> str:
    if not rows:
        return (f"{source} {window}: not enough paired samples yet (need quota % to move while"
                " tokens are logged). Keep using the agent and re-run `agentcap report calibration`.")
    solid = [r for r in rows if r.pct >= MIN_TREND_PCT] or rows
    cur = solid[-1]
    out = [
        f"  {source.upper()} - this {window}:  1% of {window} quota = {_tok(cur.tokens_per_pct)} tokens"
        f"  ~ ${cur.usd_per_pct:.2f} API-equivalent",
        "",
        f"  {'period from':<18}{'% seen':>8}{'tokens/1%':>12}{'$/1%':>9}{'vs first':>10}",
    ]
    base = solid[0].tokens_per_pct
    for r in rows:
        if r.pct < MIN_TREND_PCT:
            change = "      n/a"
        else:
            change = f"{(r.tokens_per_pct / base - 1) * 100:>+9.0f}%"
        out.append(
            f"  {_when(r.period_start):<18}{r.pct:>7.1f}%{_tok(r.tokens_per_pct):>12}"
            f"{'$' + format(r.usd_per_pct, '.2f'):>9}{change:>10}"
        )
    if len(solid) > 1 and solid[-1].tokens_per_pct < base * 0.85:
        out.append("  ! your quota buys fewer tokens than before: the limit may have shrunk.")
    out.append(f"  trend uses periods with >= {MIN_TREND_PCT:g}% observed; quota used outside these logs"
               " (web app, other machines) makes tokens/1% look smaller.")
    out.append("  tokens include cache reads; $ uses list API prices. Small % samples are noisy.")
    return "\n".join(out)


# ----------------------------------------------------------------- savings
@dataclass
class CacheWaste:
    wasted_tokens: int = 0
    wasted_usd: Decimal = Decimal(0)
    write_tokens: int = 0
    sessions: int = 0
    by_project: dict[str, Decimal] = field(default_factory=dict)


def cache_waste(events: Iterable[dict[str, Any]], pricer: _Pricer | None = None) -> CacheWaste:
    """Cache writes not followed by at least as many cache-read tokens in the same session."""

    pricer = pricer or _Pricer()
    groups: dict[tuple, list[int]] = {}
    projects: dict[tuple, str] = {}
    for e in events:
        k = (e["source"], e.get("session_id"), e.get("model"))
        g = groups.setdefault(k, [0, 0])
        g[0] += int(e["cache_write_tokens"] or 0)
        g[1] += int(e["cache_read_tokens"] or 0)
        projects[k] = e.get("project") or "-"
    w = CacheWaste()
    for k, (written, read) in groups.items():
        w.write_tokens += written
        waste = max(written - read, 0)
        if not waste:
            continue
        w.sessions += 1
        w.wasted_tokens += waste
        usd = Decimal(pricer.usd(k[2] or "", 0, 0, waste, 0) or 0)
        w.wasted_usd += usd
        w.by_project[projects[k]] = w.by_project.get(projects[k], Decimal(0)) + usd
    return w


@dataclass
class Breakeven:
    monthly_api_usd: Decimal
    rows: list[dict[str, Any]]
    recommended: str


def breakeven(api_usd: Decimal, *, days: float, vendor: str) -> Breakeven:
    monthly = (api_usd * Decimal(30) / Decimal(str(max(days, 1)))).quantize(Decimal("0.01"))
    plans = sorted((p for p in load_plans() if p.vendor == vendor), key=lambda p: p.usd_per_month)
    rows = [{"plan": p.name, "usd_per_month": p.usd_per_month, "saves_usd": monthly - p.usd_per_month,
             "source": p.source} for p in plans]
    fit = [p for p in plans if p.usd_per_month <= monthly]
    rec = fit[-1].name if fit else "API (pay as you go)"
    return Breakeven(monthly, rows, rec)


def render_savings(events: list[dict[str, Any]], *, days: float) -> str:
    w = cache_waste(events)
    out = [f"  CACHE WASTE: ${w.wasted_usd:.2f} on {_tok(w.wasted_tokens)} cache-write tokens never read"
           f" ({w.sessions} session(s))"]
    for proj, usd in sorted(w.by_project.items(), key=lambda kv: -kv[1])[:5]:
        out.append(f"     {proj[:50]:<50} ${usd:.2f}")
    out.append("")
    for vendor, label in (("claude", "Claude Code"), ("codex", "Codex")):
        usd = sum((Decimal(e["usd"]) for e in events if e["source"] == vendor and e["usd"]), Decimal(0))
        if not usd:
            continue
        b = breakeven(usd, days=days, vendor=vendor)
        out.append(f"  {label}: ${b.monthly_api_usd:.2f}/month API-equivalent (breakeven vs plans)")
        for r in b.rows:
            verdict = "plan cheaper" if r["saves_usd"] > 0 else "API cheaper"
            out.append(f"     {r['plan']:<16} ${r['usd_per_month']:>6.2f}/mo  {verdict} by ${abs(r['saves_usd']):.2f}")
        out.append(f"     -> recommended: {b.recommended}")
    out.append("  $ = list API price for the same tokens; plan prices: agentcap/plans.py (editable).")
    return "\n".join(out)


# ---------------------------------------------------------------- receipts
_BY = {"job": "session_id", "session": "session_id", "repo": "project", "branch": "branch"}


def receipts_csv(events: Iterable[dict[str, Any]], by: str) -> str:
    col = _BY.get(by, "session_id")
    agg: dict[str, dict[str, Any]] = {}
    for e in events:
        k = str(e.get(col) or "-")
        a = agg.setdefault(k, {by: k, "source": e["source"], "first_seen": e["ts"], "last_seen": e["ts"],
                                "requests": 0, "input_tokens": 0, "output_tokens": 0,
                                "cache_read_tokens": 0, "cache_write_tokens": 0,
                                "api_equivalent_usd": Decimal(0)})
        a["first_seen"] = min(a["first_seen"], e["ts"])
        a["last_seen"] = max(a["last_seen"], e["ts"])
        a["requests"] += 1
        for f in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens"):
            a[f] += int(e[f] or 0)
        if e["usd"] is not None:
            a["api_equivalent_usd"] += Decimal(e["usd"])
    buf = io.StringIO()
    cols = [by, "source", "first_seen", "last_seen", "requests", "input_tokens", "output_tokens",
            "cache_read_tokens", "cache_write_tokens", "api_equivalent_usd"]
    w = csv.DictWriter(buf, fieldnames=cols, lineterminator="\n")
    w.writeheader()
    for a in sorted(agg.values(), key=lambda a: -a["api_equivalent_usd"]):
        a = dict(a)
        a["first_seen"] = datetime.fromtimestamp(a["first_seen"]).isoformat(timespec="seconds")
        a["last_seen"] = datetime.fromtimestamp(a["last_seen"]).isoformat(timespec="seconds")
        a["api_equivalent_usd"] = str(a["api_equivalent_usd"].quantize(Decimal("0.000001")))
        w.writerow(a)
    return buf.getvalue()


def job_receipts(directory: Path) -> list[dict[str, Any]]:
    rows = []
    if not Path(directory).is_dir():
        return rows
    for path in sorted(Path(directory).glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict) or data.get("type") not in ("agentcap_receipt", "agentcap_quota_receipt"):
            continue
        rows.append({"job": data.get("run_id") or data.get("job_id"), "command": data.get("command"),
                     "started_at": data.get("started_at"), "requests": data.get("requests"),
                     "spent_usd": data.get("spent_usd"), "cap_usd": data.get("cap_usd"),
                     "reason": data.get("reason")})
    return rows
