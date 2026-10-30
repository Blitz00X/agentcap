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
