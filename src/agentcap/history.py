"""Local, read-only usage history: import agent logs into one SQLite file.

Sources (all read-only, never network, never credentials):

* AgentCap's own spend ledger (settled reservations)
* Claude Code session transcripts: ``~/.claude/projects/**/*.jsonl``
* Codex CLI rollouts: ``~/.codex/sessions/**/rollout-*.jsonl``

The transcript formats are undocumented.  Every line is parsed defensively:
unknown or malformed lines are skipped and counted, never fatal.  Only token
counts, model ids, timestamps, session ids, cwd and git branch are stored;
prompt and response text is never stored.
"""

from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable, Iterator

from .pricing import PricingError, cost, load_pricing

_FORBIDDEN = ("auth", "credential", "token", "oauth", "keychain")


def default_db_path() -> Path:
    base = os.environ.get("XDG_DATA_HOME", "").strip()
    root = Path(base).expanduser() if base else Path.home() / ".local" / "share"
    return root / "agentcap" / "usage.db"


@dataclass
class ImportStats:
    imported: int = 0
    duplicates: int = 0
    skipped: int = 0
    unpriced: int = 0
    files: int = 0

    def add(self, other: "ImportStats") -> "ImportStats":
        for f in ("imported", "duplicates", "skipped", "unpriced", "files"):
            setattr(self, f, getattr(self, f) + getattr(other, f))
        return self


_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    dedupe_key TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    ts REAL NOT NULL,
    session_id TEXT,
    project TEXT,
    branch TEXT,
    model TEXT,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens INTEGER NOT NULL DEFAULT 0,
    cache_write_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    usd TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);
CREATE TABLE IF NOT EXISTS quota_samples (
    source TEXT NOT NULL,
    ts REAL NOT NULL,
    window TEXT NOT NULL,
    used_percent REAL NOT NULL,
    resets_at REAL,
    session_id TEXT,
    PRIMARY KEY (source, ts, window)
);
CREATE TABLE IF NOT EXISTS file_offsets (
    path TEXT PRIMARY KEY,
    size INTEGER NOT NULL,
    mtime REAL NOT NULL
);
"""


class HistoryStore:
    def __init__(self, path: str | os.PathLike[str] | None = None) -> None:
        self.path = Path(path).expanduser() if path is not None else default_db_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as conn:
            conn.executescript(_SCHEMA)
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.path), timeout=30.0)
        conn.row_factory = sqlite3.Row
        return conn

    def add_events(self, rows: Iterable[dict[str, Any]]) -> tuple[int, int]:
        new = dup = 0
        with self._conn() as conn:
            for row in rows:
                cur = conn.execute(
                    "INSERT OR IGNORE INTO events (dedupe_key, source, ts, session_id, project,"
                    " branch, model, input_tokens, cache_read_tokens, cache_write_tokens,"
                    " output_tokens, usd) VALUES (:dedupe_key, :source, :ts, :session_id,"
                    " :project, :branch, :model, :input_tokens, :cache_read_tokens,"
                    " :cache_write_tokens, :output_tokens, :usd)",
                    row,
                )
                if cur.rowcount:
                    new += 1
                else:
                    dup += 1
        return new, dup

    def add_quota_samples(self, rows: Iterable[dict[str, Any]]) -> None:
        with self._conn() as conn:
            conn.executemany(
                "INSERT OR IGNORE INTO quota_samples (source, ts, window, used_percent,"
                " resets_at, session_id) VALUES (:source, :ts, :window, :used_percent,"
                " :resets_at, :session_id)",
                list(rows),
            )

    def events(self, since: float | None = None) -> list[dict[str, Any]]:
        with self._conn() as conn:
            if since is None:
                cur = conn.execute("SELECT * FROM events ORDER BY ts")
            else:
                cur = conn.execute("SELECT * FROM events WHERE ts >= ? ORDER BY ts", (since,))
            return [dict(r) for r in cur.fetchall()]

    def quota_samples(self, source: str | None = None) -> list[dict[str, Any]]:
        with self._conn() as conn:
            if source:
                cur = conn.execute(
                    "SELECT * FROM quota_samples WHERE source=? ORDER BY ts", (source,)
                )
            else:
                cur = conn.execute("SELECT * FROM quota_samples ORDER BY ts")
            return [dict(r) for r in cur.fetchall()]


# --------------------------------------------------------------------- helpers
def _ts(value: Any) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _int(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    return max(int(value), 0)


def _safe_file(path: Path) -> bool:
    name = path.name.lower()
    return path.suffix == ".jsonl" and not any(word in name for word in _FORBIDDEN)


def _lines(path: Path) -> Iterator[Any]:
    """Yield parsed JSON objects; ``None`` for a line that is not a JSON object."""

    with path.open("r", encoding="utf-8", errors="replace") as fh:
        for raw in fh:
            raw = raw.strip()
            if not raw:
                continue
            try:
                obj = json.loads(raw)
            except ValueError:
                yield None
                continue
            yield obj if isinstance(obj, dict) else None


class _Pricer:
    def __init__(self) -> None:
        try:
            self.store = load_pricing()
        except PricingError:
            self.store = None

    def usd(self, model: str, inp: int, cread: int, cwrite: int, out: int) -> str | None:
        if self.store is None or not model:
            return None
        price = None
        for candidate in (model, f"openai/{model}", f"anthropic/{model}"):
            try:
                price = self.store.get(candidate)
                break
            except PricingError:
                continue
        if price is None:
            return None
        try:
            if price.provider == "anthropic":
                value = cost(
                    price, inp + cread + cwrite, cread, out, cache_write_tokens=cwrite
                )
            else:  # OpenAI: cache writes bill as ordinary input
                value = cost(price, inp + cread + cwrite, cread, out)
        except (PricingError, ValueError):
            return None
        return str(value.quantize(Decimal("0.000001")))


# ---------------------------------------------------------------------- claude
def import_claude(store: HistoryStore, root: Path, pricer: _Pricer | None = None) -> ImportStats:
    stats = ImportStats()
    root = Path(root)
    if not root.is_dir():
        return stats
    pricer = pricer or _Pricer()
    for path in sorted(root.rglob("*.jsonl")):
        if not _safe_file(path):
            continue
        stats.files += 1
        rows: list[dict[str, Any]] = []
        seen: set[str] = set()
        for obj in _lines(path):
            if obj is None:
                stats.skipped += 1
                continue
            if obj.get("type") != "assistant":
                continue
            msg = obj.get("message")
            usage = msg.get("usage") if isinstance(msg, dict) else None
            ts = _ts(obj.get("timestamp"))
            if not isinstance(usage, dict) or ts is None:
                stats.skipped += 1
                continue
            model = str(msg.get("model") or "")
            if model == "<synthetic>":
                continue
            key = f"claude:{msg.get('id') or obj.get('uuid')}:{obj.get('requestId') or ''}"
            if key in seen:
                continue
            seen.add(key)
            inp = _int(usage.get("input_tokens"))
            cread = _int(usage.get("cache_read_input_tokens"))
            cwrite = _int(usage.get("cache_creation_input_tokens"))
            out = _int(usage.get("output_tokens"))
            usd = pricer.usd(model, inp, cread, cwrite, out)
            if usd is None:
                stats.unpriced += 1
            rows.append(
                {
                    "dedupe_key": key, "source": "claude", "ts": ts,
                    "session_id": obj.get("sessionId"), "project": obj.get("cwd"),
                    "branch": obj.get("gitBranch") or None, "model": model,
                    "input_tokens": inp, "cache_read_tokens": cread,
                    "cache_write_tokens": cwrite, "output_tokens": out, "usd": usd,
                }
            )
        new, dup = store.add_events(rows)
        stats.imported += new
        stats.duplicates += dup
    return stats


# ----------------------------------------------------------------------- codex
_WINDOWS = {300: "5h", 10080: "week"}


def import_codex(store: HistoryStore, root: Path, pricer: _Pricer | None = None) -> ImportStats:
    stats = ImportStats()
    root = Path(root)
    if not root.is_dir():
        return stats
    pricer = pricer or _Pricer()
    for path in sorted(root.rglob("rollout-*.jsonl")):
        if not _safe_file(path):
            continue
        stats.files += 1
        session = cwd = branch = None
        model = ""
        last_total: Any = None
        rows: list[dict[str, Any]] = []
        samples: list[dict[str, Any]] = []
        for obj in _lines(path):
            if obj is None:
                stats.skipped += 1
                continue
            payload = obj.get("payload")
            if not isinstance(payload, dict):
                continue
            kind = obj.get("type")
            if kind == "session_meta":
                session = payload.get("id")
                cwd = payload.get("cwd")
                git = payload.get("git")
                if isinstance(git, dict):
                    branch = git.get("branch")
                continue
            if kind == "turn_context":
                model = str(payload.get("model") or model)
                cwd = payload.get("cwd") or cwd
                continue
            if kind != "event_msg" or payload.get("type") != "token_count":
                continue
            ts = _ts(obj.get("timestamp"))
            if ts is None:
                stats.skipped += 1
                continue
            limits = payload.get("rate_limits")
            if isinstance(limits, dict):
                for slot in ("primary", "secondary"):
                    win = limits.get(slot)
                    if not isinstance(win, dict):
                        continue
                    name = _WINDOWS.get(_int(win.get("window_minutes")))
                    pct = win.get("used_percent")
                    if name and isinstance(pct, (int, float)) and not isinstance(pct, bool):
                        samples.append(
                            {"source": "codex", "ts": ts, "window": name,
                             "used_percent": float(pct), "resets_at": _ts(win.get("resets_at")),
                             "session_id": session}
                        )
            info = payload.get("info")
            if info is None:
                continue
            if not isinstance(info, dict):
                stats.skipped += 1
                continue
            total = info.get("total_token_usage")
            last = info.get("last_token_usage")
            if not isinstance(total, dict) or not isinstance(last, dict):
                stats.skipped += 1
                continue
            marker = json.dumps(total, sort_keys=True)
            if marker == last_total:
                continue  # Codex repeats the same count after some events
            last_total = marker
            inp_all = _int(last.get("input_tokens"))
            cread = min(_int(last.get("cached_input_tokens")), inp_all)
            # newer Codex logs carry cache_write_input_tokens (part of input_tokens)
            cwrite = min(_int(last.get("cache_write_input_tokens")), inp_all - cread)
            out = _int(last.get("output_tokens"))
            usd = pricer.usd(model, inp_all - cread - cwrite, cread, cwrite, out)
            if usd is None:
                stats.unpriced += 1
            rows.append(
                {
                    "dedupe_key": f"codex:{session or path.name}:{_int(total.get('total_tokens'))}:{ts}",
                    "source": "codex", "ts": ts, "session_id": session, "project": cwd,
                    "branch": branch, "model": model, "input_tokens": inp_all - cread - cwrite,
                    "cache_read_tokens": cread, "cache_write_tokens": cwrite,
                    "output_tokens": out, "usd": usd,
                }
            )
        new, dup = store.add_events(rows)
        store.add_quota_samples(samples)
        stats.imported += new
        stats.duplicates += dup
    return stats


# ------------------------------------------------------- claude quota samples
def import_claude_quota(store: HistoryStore, state_dir: Path) -> ImportStats:
    """Read AgentCap's own statusline sample log (written during quota mode)."""

    stats = ImportStats()
    path = Path(state_dir) / "claude_samples.jsonl"
    if not path.is_file():
        return stats
    stats.files = 1
    rows = []
    for obj in _lines(path):
        ts = _ts(obj.get("ts")) if obj else None
        readings = obj.get("readings") if obj else None
        if ts is None or not isinstance(readings, dict):
            stats.skipped += 1
            continue
        for window, block in readings.items():
            if window not in ("5h", "week") or not isinstance(block, dict):
                continue
            used = block.get("used")
            if not isinstance(used, (int, float)) or isinstance(used, bool):
                continue
            rows.append({"source": "claude", "ts": ts, "window": window, "used_percent": float(used),
                         "resets_at": _ts(block.get("resets_at")), "session_id": None})
    store.add_quota_samples(rows)
    stats.imported = len(rows)
    return stats


# ---------------------------------------------------------------------- ledger
def import_ledger(store: HistoryStore, ledger_path: Path) -> ImportStats:
    stats = ImportStats()
    ledger_path = Path(ledger_path)
    if not ledger_path.is_file():
        return stats
    stats.files = 1
    uri = f"file:{ledger_path}?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=10.0)
        try:
            cur = conn.execute(
                "SELECT id, run_id, model, actual_usd, COALESCE(settled_at, created_at)"
                " FROM reservations WHERE status='settled'"
            )
            data = cur.fetchall()
        finally:
            conn.close()
    except sqlite3.Error:
        stats.skipped += 1
        return stats
    rows = [
        {"dedupe_key": f"ledger:{rid}", "source": "agentcap", "ts": float(ts),
         "session_id": run, "project": None, "branch": None, "model": model,
         "input_tokens": 0, "cache_read_tokens": 0, "cache_write_tokens": 0,
         "output_tokens": 0, "usd": usd}
        for rid, run, model, usd, ts in data
    ]
    new, dup = store.add_events(rows)
    stats.imported, stats.duplicates = new, dup
    return stats


def import_all(store: HistoryStore, home: Path | None = None) -> ImportStats:
    from .ledger import default_ledger_path

    home = Path(home) if home is not None else Path.home()
    pricer = _Pricer()
    stats = ImportStats()
    stats.add(import_claude(store, home / ".claude" / "projects", pricer))
    stats.add(import_codex(store, home / ".codex" / "sessions", pricer))
    ledger = (
        default_ledger_path() if home == Path.home()
        else home / ".local" / "state" / "agentcap" / "ledger.sqlite3"
    )
    stats.add(import_ledger(store, ledger))
    if home == Path.home():
        from .quota import state_root

        quota_dir = state_root()
    else:
        quota_dir = home / ".local" / "state" / "agentcap" / "quota"
    samples = import_claude_quota(store, quota_dir)
    stats.skipped += samples.skipped
    return stats
