import csv
import io
import json
from decimal import Decimal

import pytest

import agentcap.cli as cli
from agentcap import analytics, history, plans
from test_cli import write_pro_cache

H = 3600.0
T0 = 1_800_000_000.0  # fixed epoch


def ev(store, key, ts, *, source="codex", model="gpt-5.4", inp=0, out=0, cr=0, cw=0,
       usd="0.10", session="s1", project="/r/api", branch=None):
    store.add_events([{
        "dedupe_key": key, "source": source, "ts": ts, "session_id": session,
        "project": project, "branch": branch, "model": model, "input_tokens": inp,
        "cache_read_tokens": cr, "cache_write_tokens": cw, "output_tokens": out, "usd": usd,
    }])


def qs(store, ts, pct, window="week", source="codex", resets=T0 + 7 * 24 * H):
    store.add_quota_samples([{"source": source, "ts": ts, "window": window,
                              "used_percent": pct, "resets_at": resets, "session_id": None}])


@pytest.fixture
def store(tmp_path):
    return history.HistoryStore(tmp_path / "u.db")


# ------------------------------------------------------------ (a) forecast
def test_forecast_projects_limit_time(store):
    qs(store, T0, 40.0)
    qs(store, T0 + H, 50.0)  # 10%/h -> 100% in 5h
    f = analytics.forecast(store, "codex", "week", now=T0 + H)
    assert f.used_percent == 50.0
    assert f.hits_at == pytest.approx(T0 + 6 * H, abs=60)
    assert f.before_reset is True


def test_forecast_no_hit_before_reset(store):
    resets = T0 + 2 * H
    qs(store, T0, 10.0, resets=resets)
    qs(store, T0 + H, 11.0, resets=resets)
    f = analytics.forecast(store, "codex", "week", now=T0 + H)
    assert f.before_reset is False


def test_forecast_ignores_samples_from_previous_reset(store):
    qs(store, T0, 90.0, resets=T0 + 10)  # old window
    qs(store, T0 + H, 5.0)
    qs(store, T0 + 2 * H, 10.0)
    f = analytics.forecast(store, "codex", "week", now=T0 + 2 * H)
    assert f.rate_per_hour == pytest.approx(5.0)


def test_forecast_none_without_data(store):
    assert analytics.forecast(store, "codex", "5h", now=T0) is None


# --------------------------------------------------------- (b) calibration
def test_calibration_tokens_per_percent(store):
    qs(store, T0, 10.0)
    ev(store, "a", T0 + 10, inp=1000, out=500, cr=8500, usd="0.20")
    ev(store, "b", T0 + 20, inp=0, out=0, cr=0, usd="0.20")
    qs(store, T0 + 30, 12.0)  # 2% used for 10k tokens, $0.40
    rows = analytics.calibration(store, "codex", "week")
    assert len(rows) == 1
    r = rows[0]
    assert r.tokens_per_pct == pytest.approx(5000)
    assert r.usd_per_pct == pytest.approx(Decimal("0.20"))


def test_calibration_trend_shows_shrinking_limit(store):
    week = 7 * 24 * H
    for i, tok in enumerate((10000, 5000)):
        base = T0 + i * week
        r = base + week
        qs(store, base, 10.0, resets=r)
        ev(store, f"e{i}", base + 5, inp=tok, usd="0.10")
        qs(store, base + 10, 11.0, resets=r)
    rows = analytics.calibration(store, "codex", "week")
    assert [round(x.tokens_per_pct) for x in rows] == [10000, 5000]
    text = analytics.render_calibration(rows, "codex", "week")
    assert "1% of week quota" in text
    assert "-50%" in text


def test_calibration_skips_pairs_across_reset_or_without_tokens(store):
    qs(store, T0, 90.0, resets=T0 + 5)
    qs(store, T0 + 10, 1.0)
    qs(store, T0 + 20, 2.0)  # no events in between
    assert analytics.calibration(store, "codex", "week") == []


# ------------------------------------------------------------ (c) savings
def test_cache_waste_counts_writes_never_read(store):
    m = "claude-sonnet-4-5-20250929"
    ev(store, "w1", T0, source="claude", model=m, cw=1_000_000, session="dead", usd="3.75")
    ev(store, "w2", T0, source="claude", model=m, cw=1_000_000, session="live", usd="3.75")
    ev(store, "r2", T0 + 1, source="claude", model=m, cr=5_000_000, session="live", usd="1.5")
    w = analytics.cache_waste(store.events())
    assert w.wasted_tokens == 1_000_000
    assert w.wasted_usd == pytest.approx(Decimal("3.75"), rel=Decimal("0.01"))


def test_breakeven_and_recommendation():
    rec = analytics.breakeven(Decimal("150"), days=30, vendor="claude")
    names = [r["plan"] for r in rec.rows]
    assert "Claude Max 5x" in names
    assert rec.recommended == "Claude Max 5x"
    cheap = analytics.breakeven(Decimal("8"), days=30, vendor="claude")
    assert cheap.recommended == "API (pay as you go)"


def test_plan_table_has_sources():
    for p in plans.PLANS:
        assert p.source.startswith("http") and p.usd_per_month > 0


def test_plan_table_override(tmp_path, monkeypatch):
    f = tmp_path / "cfg" / "agentcap" / "plans.json"
    f.parent.mkdir(parents=True)
    f.write_text(json.dumps([{"name": "Claude Pro", "vendor": "claude", "usd_per_month": "17",
                              "source": "https://example.test"}]))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    loaded = {p.name: p for p in plans.load_plans()}
    assert loaded["Claude Pro"].usd_per_month == Decimal("17")


# ------------------------------------------------------------ (d) receipts
def test_receipts_csv_by_repo(store):
    ev(store, "1", T0, project="/r/api", usd="1.00")
    ev(store, "2", T0, project="/r/web", usd="2.00")
    out = analytics.receipts_csv(store.events(), "repo")
    rows = list(csv.DictReader(io.StringIO(out)))
    assert {r["repo"] for r in rows} == {"/r/api", "/r/web"}
    assert "api_equivalent_usd" in rows[0]


def test_job_receipts_read_agentcap_receipt_files(tmp_path):
    d = tmp_path / "receipts"
    d.mkdir()
    (d / "r1.json").write_text(json.dumps({"type": "agentcap_receipt", "run_id": "r1",
        "command": "codex exec", "started_at": "2026-10-29T10:00:00+00:00",
        "spent_usd": "0.42", "cap_usd": "1", "requests": 3, "reason": "exited", "exit_code": 0}))
    (d / "junk.json").write_text("{")
    rows = analytics.job_receipts(d)
    assert rows == [{"job": "r1", "command": "codex exec", "started_at": "2026-10-29T10:00:00+00:00",
                     "requests": 3, "spent_usd": "0.42", "cap_usd": "1", "reason": "exited"}]


# ------------------------------------------------------------ CLI gating
@pytest.fixture
def cli_env(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("AGENTCAP_LICENSE_FILE", str(tmp_path / "license.json"))
    s = history.HistoryStore(tmp_path / "data" / "agentcap" / "usage.db")
    return tmp_path, s


@pytest.mark.parametrize("view", ["forecast", "calibration", "savings", "receipts"])
def test_pro_views_need_pro(cli_env, capsys, view):
    assert cli.main(["report", view]) == 2
    assert "Pro" in capsys.readouterr().err


def test_pro_calibration_output_is_screenshot_friendly(cli_env, capsys):
    tmp, s = cli_env
    write_pro_cache(tmp / "license.json")
    qs(s, T0, 10.0)
    ev(s, "a", T0 + 1, inp=40000, usd="0.30")
    qs(s, T0 + 2, 11.0)
    assert cli.main(["report", "calibration", "--no-import"]) == 0
    out = capsys.readouterr().out
    assert "1% of week quota = 40.0k tokens" in out


def test_pro_savings_output(cli_env, capsys):
    tmp, s = cli_env
    write_pro_cache(tmp / "license.json")
    ev(s, "w", T0, source="claude", model="claude-sonnet-4-5-20250929", cw=1_000_000, usd="3.75")
    assert cli.main(["report", "savings", "--no-import", "--since", "all"]) == 0
    out = capsys.readouterr().out
    assert "cache waste" in out.lower() and "$3.75" in out
    assert "breakeven" in out.lower()


def test_pro_receipts_csv(cli_env, capsys):
    tmp, s = cli_env
    write_pro_cache(tmp / "license.json")
    ev(s, "1", T0, project="/r/api", usd="1.00")
    assert cli.main(["report", "receipts", "--by", "repo", "--csv", "--no-import", "--since", "all"]) == 0
    assert capsys.readouterr().out.startswith("repo,")
