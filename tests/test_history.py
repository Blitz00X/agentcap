import json
import shutil
import sqlite3
from decimal import Decimal
from pathlib import Path

import pytest

from agentcap import history

FIX = Path(__file__).parent / "fixtures" / "history"


@pytest.fixture
def home(tmp_path):
    shutil.copytree(FIX / "claude", tmp_path / ".claude")
    shutil.copytree(FIX / "codex", tmp_path / ".codex")
    return tmp_path


@pytest.fixture
def store(tmp_path):
    return history.HistoryStore(tmp_path / "data" / "usage.db")


def test_default_db_respects_xdg(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "x"))
    assert history.default_db_path() == tmp_path / "x" / "agentcap" / "usage.db"


def test_default_db_falls_back_to_local_share(monkeypatch, tmp_path):
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    assert history.default_db_path() == tmp_path / ".local" / "share" / "agentcap" / "usage.db"


def test_claude_import_dedupes_streamed_message_and_skips_bad_lines(store, home):
    stats = history.import_claude(store, home / ".claude" / "projects")
    assert stats.imported == 2
    assert stats.skipped >= 2  # non-json + usage-less assistant
    rows = store.events()
    a1 = next(r for r in rows if r["model"].startswith("claude-sonnet"))
    assert a1["source"] == "claude"
    assert a1["session_id"] == "sess-a"
    assert a1["project"] == "/home/dev/shop"
    assert a1["branch"] == "main"
    assert a1["input_tokens"] == 10
    assert a1["cache_write_tokens"] == 2000
    assert a1["cache_read_tokens"] == 0
    assert a1["output_tokens"] == 100
    assert Decimal(a1["usd"]) > 0


def test_synthetic_zero_rows_are_ignored(store, home):
    history.import_claude(store, home / ".claude" / "projects")
    assert all(r["model"] != "<synthetic>" for r in store.events())


def test_import_is_idempotent(store, home):
    history.import_all(store, home=home)
    first = len(store.events())
    stats = history.import_all(store, home=home)
    assert len(store.events()) == first
    assert stats.imported == 0


def test_codex_import_uses_last_usage_and_dedupes_repeats(store, home):
    stats = history.import_codex(store, home / ".codex" / "sessions")
    assert stats.imported == 2
    rows = sorted(store.events(), key=lambda r: r["ts"])
    assert rows[0]["source"] == "codex"
    assert rows[0]["model"] == "gpt-5.4"
    assert rows[0]["project"] == "/home/dev/api"
    assert rows[0]["branch"] == "feat-x"
    assert rows[0]["session_id"] == "0199aaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    # OpenAI input includes cached; we store uncached input separately
    assert rows[0]["input_tokens"] == 2000
    assert rows[0]["cache_read_tokens"] == 6000
    assert rows[1]["output_tokens"] == 1000
    assert stats.skipped >= 1


def test_codex_rate_limit_samples_stored(store, home):
    history.import_codex(store, home / ".codex" / "sessions")
    samples = store.quota_samples()
    weekly = [s for s in samples if s["window"] == "week"]
    assert {s["used_percent"] for s in weekly} == {30.0, 30.5}
    assert all(s["source"] == "codex" for s in samples)


def test_never_opens_credential_files(store, home, monkeypatch):
    opened = []
    real_open = Path.open

    def spy(self, *a, **k):
        opened.append(self.name)
        return real_open(self, *a, **k)

    monkeypatch.setattr(Path, "open", spy)
    history.import_all(store, home=home)
    assert not any("auth" in n or "credential" in n for n in opened)
    assert opened  # importer really read jsonl via Path.open


def test_unpriced_model_kept_without_usd(store, tmp_path):
    p = tmp_path / "projects" / "x"
    p.mkdir(parents=True)
    (p / "s.jsonl").write_text(
        '{"type":"assistant","sessionId":"s","cwd":"/r","timestamp":"2026-10-29T00:00:00Z",'
        '"requestId":"r","message":{"id":"m","model":"mystery-1","usage":{"input_tokens":5,"output_tokens":5}}}\n'
    )
    stats = history.import_claude(store, tmp_path / "projects")
    assert stats.imported == 1 and stats.unpriced == 1
    assert store.events()[0]["usd"] is None


def test_ledger_import_reads_settled_reservations(store, tmp_path):
    from agentcap.ledger import Ledger

    led = Ledger(tmp_path / "ledger.sqlite3")
    rid = led.reserve("run1", "openai", "openai/gpt-5.4", "0.50")
    led.settle(rid, "0.12")
    stats = history.import_ledger(store, tmp_path / "ledger.sqlite3")
    assert stats.imported == 1
    row = store.events()[0]
    assert row["source"] == "agentcap" and row["session_id"] == "run1"
    assert Decimal(row["usd"]) == Decimal("0.12")
    assert history.import_ledger(store, tmp_path / "ledger.sqlite3").imported == 0


def test_missing_sources_are_fine(store, tmp_path):
    stats = history.import_all(store, home=tmp_path)
    assert stats.imported == 0


def test_db_is_private(store):
    assert (store.path.stat().st_mode & 0o777) == 0o600


def test_claude_quota_samples_imported_from_statusline_log(store, tmp_path):
    log = tmp_path / "state" / "claude_samples.jsonl"
    log.parent.mkdir()
    log.write_text(
        '{"ts": 1800000000, "readings": {"5h": {"used": 12.0, "resets_at": 1800010000},'
        ' "week": {"used": 40.0, "resets_at": 1800500000}}}\n'
        'garbage\n'
        '{"ts": 1800000600, "readings": {"week": {"used": 41.0, "resets_at": 1800500000}}}\n'
    )
    stats = history.import_claude_quota(store, log.parent)
    samples = store.quota_samples("claude")
    assert len(samples) == 3 and stats.skipped == 1
    history.import_claude_quota(store, log.parent)
    assert len(store.quota_samples("claude")) == 3


def test_statusline_appends_sample_only_on_change(tmp_path, monkeypatch):
    import io
    import json as _json

    from agentcap import quota

    monkeypatch.setenv(quota.STATE_ENV, str(tmp_path / "st"))
    job = tmp_path / "job"
    job.mkdir()
    (job / "job.json").write_text('{"window": "week", "quota": 5}')
    payload = {"session_id": "s", "rate_limits": {"seven_day": {"used_percentage": 40, "resets_at": 1800500000}}}
    for now in (1.0, 2.0):
        quota.statusline_main(job, io.StringIO(_json.dumps(payload)), io.StringIO(), now=now)
    payload["rate_limits"]["seven_day"]["used_percentage"] = 41
    quota.statusline_main(job, io.StringIO(_json.dumps(payload)), io.StringIO(), now=3.0)
    lines = (tmp_path / "st" / "claude_samples.jsonl").read_text().splitlines()
    assert len(lines) == 2


def test_codex_cache_write_and_token_usage_record_lines(store, tmp_path):
    d = tmp_path / "sessions" / "2026" / "10" / "30"
    d.mkdir(parents=True)
    usage = {"input_tokens": 10000, "cached_input_tokens": 6000, "cache_write_input_tokens": 1000,
             "output_tokens": 300, "reasoning_output_tokens": 100, "total_tokens": 10300}
    lines = [
        {"timestamp": "2026-10-30T10:00:00Z", "type": "session_meta",
         "payload": {"id": "sess-r", "cwd": "/w", "history_mode": "x", "base_instructions": {"text": "REDACTED"}}},
        {"timestamp": "2026-10-30T10:00:01Z", "type": "turn_context", "payload": {"model": "gpt-5.4"}},
        {"timestamp": "2026-10-30T10:00:02Z", "ordinal": 1, "type": "token_usage_record",
         "payload": {"session_id": "sess-r", "usage": usage, "turn_token_usage": usage, "thread_token_usage": usage}},
        {"timestamp": "2026-10-30T10:00:03Z", "type": "event_msg", "payload": {"type": "token_count",
         "info": {"last_token_usage": usage, "total_token_usage": usage, "model_context_window": 1,
                  "last_usage_includes_reasoning": True},
         "rate_limits": {"limit_id": "codex", "plan_type": "plus", "credits": None,
                         "primary": {"used_percent": 5.0, "window_minutes": 300, "resets_at": 1793376000},
                         "secondary": {"used_percent": 1.0, "window_minutes": 10080, "resets_at": 1793900000}}}},
    ]
    (d / "rollout-2026-10-30T10-00-00-sess-r.jsonl").write_text("\n".join(json.dumps(x) for x in lines) + "\n")
    stats = history.import_codex(store, tmp_path / "sessions")
    assert stats.imported == 1  # token_usage_record is not double counted
    row = store.events()[0]
    assert row["cache_read_tokens"] == 6000
    assert row["cache_write_tokens"] == 1000
    assert row["input_tokens"] == 3000
    assert row["output_tokens"] == 300
