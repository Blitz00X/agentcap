"""Live quota reads through the agents' own clients (codex app-server, claude -p /usage)."""

from __future__ import annotations

import datetime as dt
import json
import os
import stat
import sys
import textwrap
import time
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from agentcap import live_quota as lq
from agentcap import quota as q

IST = ZoneInfo("Europe/Istanbul")
# 12 Nov 2026 12:00 Istanbul
NOW = dt.datetime(2026, 11, 12, 12, 0, tzinfo=IST).timestamp()

# verbatim `claude -p "/usage"` output, 12 Nov 2026 (Claude Code 2.1.322)
USAGE_TEXT = """\
You are currently using your subscription to power your Claude Code usage

Current session: 10% used · resets Nov 12, 5:29pm (Europe/Istanbul)
Current week (all models): 70% used · resets Nov 12, 11:59pm (Europe/Istanbul)
Current week (Fable): 5% used · resets Nov 12, 11:59pm (Europe/Istanbul)

What's contributing to your usage?
Approximate · sessions on this machine only · shares overlap

Last 24h · 226 requests · 3 sessions
  94%  Stop sessions you no longer need · some ran 8 hours or more
"""


def _ts(*args: int, tz=IST) -> float:
    return dt.datetime(*args, tzinfo=tz).timestamp()


def test_parse_claude_usage_real_output():
    got = lq.parse_claude_usage(USAGE_TEXT, NOW)
    assert got == {
        "5h": q.Reading(10.0, _ts(2026, 11, 12, 17, 29)),
        "week": q.Reading(70.0, _ts(2026, 11, 12, 23, 59)),
    }


def test_parse_reset_text_variants():
    assert lq.parse_reset_text("Nov 12, 12:00am (Europe/Istanbul)", NOW) == _ts(2026, 11, 12, 0, 0)
    assert lq.parse_reset_text("Nov 12, 12pm (UTC)", NOW) == _ts(2026, 11, 12, 12, 0, tz=dt.timezone.utc)
    # no date: today, or tomorrow when that time has passed
    assert lq.parse_reset_text("5:29pm (Europe/Istanbul)", NOW) == _ts(2026, 11, 12, 17, 29)
    assert lq.parse_reset_text("9am (Europe/Istanbul)", NOW) == _ts(2026, 11, 13, 9, 0)
    # the year is not printed: early January seen from late December is next year
    dec31 = _ts(2026, 12, 31, 22, 0)
    assert lq.parse_reset_text("Jan 2, 3:00am (Europe/Istanbul)", dec31) == _ts(2027, 1, 2, 3, 0)
    assert lq.parse_reset_text("soon", NOW) is None
    assert lq.parse_reset_text("Foo 3, 1pm", NOW) is None


def test_parse_claude_usage_ignores_other_text():
    assert lq.parse_claude_usage("Hello! How can I help you today?", NOW) == {}
    assert lq.parse_claude_usage("Current week (all models): 3% used", NOW) == {"week": q.Reading(3.0, None)}


def test_parse_codex_rate_limits():
    result = {"rateLimits": {
        "primary": {"usedPercent": 0, "windowDurationMins": 300, "resetsAt": 1794496670},
        "secondary": {"usedPercent": 89, "windowDurationMins": 10080, "resetsAt": 1794900000},
    }, "planType": "plus"}
    assert lq.parse_codex_rate_limits(result) == {
        "5h": q.Reading(0.0, 1794496670.0), "week": q.Reading(89.0, 1794900000.0)}
    assert lq.parse_codex_rate_limits({"rateLimits": {"primary": {"usedPercent": 5, "windowDurationMins": 60}}}) == {}
    assert lq.parse_codex_rate_limits(None) == {}


def _script(path: Path, body: str) -> str:
    path.write_text(f"#!{sys.executable}\n" + textwrap.dedent(body))
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return str(path)


FAKE_APP_SERVER = """\
    import json, sys
    assert sys.argv[1:] == ["app-server"], sys.argv
    for line in sys.stdin:
        msg = json.loads(line)
        if msg.get("method") == "initialize":
            print(json.dumps({"id": msg["id"], "result": {"userAgent": "fake"}}), flush=True)
        elif msg.get("method") == "account/rateLimits/read":
            print(json.dumps({"method": "account/rateLimits/updated", "params": {}}), flush=True)
            print("not json", flush=True)
            print(json.dumps({"id": msg["id"], "result": {"rateLimits": {
                "primary": {"usedPercent": 4, "windowDurationMins": 300, "resetsAt": 2000000000},
                "secondary": {"usedPercent": 89, "windowDurationMins": 10080, "resetsAt": 2000500000}}}}),
                flush=True)
    """


def test_codex_live_with_fake_app_server(tmp_path):
    exe = _script(tmp_path / "codex", FAKE_APP_SERVER)
    assert lq.codex_live(binary=exe) == {
        "5h": q.Reading(4.0, 2000000000.0), "week": q.Reading(89.0, 2000500000.0)}


def test_codex_live_failures_return_none(tmp_path):
    hang = _script(tmp_path / "hang", "import time; time.sleep(30)\n")
    t0 = time.monotonic()
    assert lq.codex_live(binary=hang, timeout=1.0) is None
    assert time.monotonic() - t0 < 6
    err = _script(tmp_path / "err", """\
        import json, sys
        for line in sys.stdin:
            msg = json.loads(line)
            if "id" in msg:
                print(json.dumps({"id": msg["id"], "error": {"message": "not logged in"}}), flush=True)
        """)
    assert lq.codex_live(binary=err) is None
    assert lq.codex_live(binary=str(tmp_path / "missing")) is None


def test_claude_live_with_fake_cli(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setenv("AGENTCAP_QUOTA_JOB", "/x")
    out = tmp_path / "args.json"
    exe = _script(tmp_path / "claude", f"""\
        import json, os, sys
        json.dump({{"argv": sys.argv[1:], "markers": [k for k in ("CLAUDECODE", "AGENTCAP_QUOTA_JOB") if k in os.environ],
                   "cwd": os.getcwd()}}, open({str(out)!r}, "w"))
        sys.stdout.write({USAGE_TEXT!r})
        """)
    probe = tmp_path / "probe"
    probe.mkdir()
    got = lq.claude_live(binary=exe, cwd=probe, now=NOW)
    assert got and got["week"].used == 70.0 and got["5h"].used == 10.0
    seen = json.loads(out.read_text())
    assert seen["argv"] == ["-p", "--no-session-persistence", "/usage"] and seen["markers"] == [] and seen["cwd"] == str(probe)


def test_claude_live_failures_return_none(tmp_path):
    chat = _script(tmp_path / "chat", "print('Sure! What would you like me to do?')\n")
    assert lq.claude_live(binary=chat) is None
    fail = _script(tmp_path / "fail", "import sys; print('Current week (all models): 1% used'); sys.exit(1)\n")
    assert lq.claude_live(binary=fail) is None
    slow = _script(tmp_path / "slow", "import time; time.sleep(30)\n")
    assert lq.claude_live(binary=slow, timeout=1.0) is None


def test_live_switch(monkeypatch):
    assert lq.live_enabled({}) and not lq.live_enabled({"AGENTCAP_LIVE_QUOTA": "0"})
    monkeypatch.setenv("AGENTCAP_LIVE_QUOTA", "0")
    assert lq.live_readings("codex") is None and lq.live_readings("claude") is None


# ------------------------------------------------------------ agentcap quota
def test_quota_status_prefers_live_and_stores_claude(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTCAP_QUOTA_DIR", str(tmp_path / "state"))
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "claude_last.json").write_text(json.dumps(
        {"ts": NOW - 7 * 3600, "readings": {"week": {"used": 55.0, "resets_at": NOW + 3600}}}))
    live = {"claude": {"week": q.Reading(70.0, NOW + 12 * 3600)},
            "codex": {"week": q.Reading(89.0, NOW + 3600), "5h": q.Reading(0.0, NOW + 600)}}
    lines = q.quota_status(now=NOW, sessions_dir=tmp_path / "none", live=live.get)
    assert lines == [
        "claude week: 70.0% used, resets in 12h00m (live from claude)",
        "codex 5h: 0.0% used, resets in 0h10m (live from codex)",
        "codex week: 89.0% used, resets in 1h00m (live from codex)",
    ]
    stored = json.loads((tmp_path / "state" / "claude_last.json").read_text())
    assert stored["ts"] == NOW and stored["readings"]["week"]["used"] == 70.0 and stored["source"] == "usage"


def test_quota_status_falls_back_to_local_when_live_fails(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTCAP_QUOTA_DIR", str(tmp_path / "state"))
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "claude_last.json").write_text(json.dumps(
        {"ts": NOW - 7 * 3600, "readings": {"week": {"used": 55.0, "resets_at": NOW + 3600}}}))
    lines = q.quota_status(now=NOW, sessions_dir=tmp_path / "none", live=lambda agent: None)
    assert lines[0].startswith("claude week: 55.0% used") and "STALE" in lines[0]
    assert lines[1] == "codex: no rate_limits found in ~/.codex/sessions"


def test_cli_quota_local_flag_skips_live(monkeypatch, tmp_path, capsys):
    from agentcap import cli
    monkeypatch.setenv("AGENTCAP_QUOTA_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    calls = []
    monkeypatch.setattr(lq, "live_readings", lambda agent: calls.append(agent))
    assert cli.main(["quota", "--local"]) == 0
    assert calls == []
    assert cli.main(["quota"]) == 0
    assert calls == ["claude", "codex"]


# ------------------------------------------------------------ job baselines
def test_statusline_seed_accepts_minute_rounded_reset(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTCAP_QUOTA_DIR", str(tmp_path / "state"))
    (tmp_path / "state").mkdir()
    job = tmp_path / "job"
    job.mkdir()
    (job / "job.json").write_text(json.dumps({"quota": 5, "window": "week", "started_at": NOW, "enforce": True}))
    exact = NOW + 86400 + 37   # statusline: to the second; /usage: to the minute
    (tmp_path / "state" / "claude_last.json").write_text(json.dumps(
        {"ts": NOW - 30, "readings": {"week": {"used": 70.0, "resets_at": NOW + 86400}}, "source": "usage"}))
    payload = {"session_id": "s", "rate_limits": {"seven_day": {"used_percentage": 71.0, "resets_at": exact}}}
    import io
    q.statusline_main(job, io.StringIO(json.dumps(payload)), io.StringIO(), now=NOW)
    status = json.loads((job / "status.json").read_text())
    assert status["share"] == 1.0 and status["tracker"]["baseline_exact"] is True


@pytest.fixture
def fakebin(tmp_path, monkeypatch):
    from tests.test_quota import FAKE_CLAUDE, FAKE_CODEX
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, src in (("claude", FAKE_CLAUDE), ("codex", FAKE_CODEX)):
        p = bin_dir / name
        p.write_text(src.format(python=sys.executable))
        p.chmod(p.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("AGENTCAP_QUOTA_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    src = str(Path(__file__).resolve().parents[1] / "src")
    monkeypatch.setenv("PYTHONPATH", src + os.pathsep + os.environ.get("PYTHONPATH", ""))
    monkeypatch.setenv("AGENTCAP_LIVE_QUOTA", "1")
    return bin_dir


def _runner(argv, tmp_path, live, **kw):  # noqa: D401
    import io
    err = io.StringIO()
    asked = []

    def reader(agent):
        asked.append(agent)
        return live.get(agent)

    r = q.QuotaRunner(argv, quota=kw.pop("quota", 50.0), stderr=err, poll_s=0.05, claude_grace_s=1.0,
                      receipt_dir=tmp_path / "receipts", cwd=tmp_path, home=tmp_path / "home",
                      live_reader=reader, live_settle_s=0.05, **kw)
    return r, err, asked


def test_codex_job_without_fresh_reading_takes_live_baseline(fakebin, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_START", "39")   # first own reading 40
    monkeypatch.setenv("FAKE_N", "1")
    r, err, asked = _runner(["codex"], tmp_path, {"codex": {"week": q.Reading(38.0, time.time() + 86400)}})
    assert r.run() == 0
    assert asked[0] == "codex"
    assert r.summary.baseline_exact and r.summary.share == 2.0
    receipt = json.loads(next((tmp_path / "receipts").glob("*.json")).read_text())
    assert receipt["baseline_live"] is True and receipt["baseline_age_s"] is None


def test_codex_job_prefers_live_over_a_fresh_lagging_reading(fakebin, tmp_path, monkeypatch):
    d = tmp_path / "codex" / "sessions" / "2026" / "10" / "29"
    d.mkdir(parents=True)
    limits = {"secondary": {"used_percent": 38.0, "window_minutes": 10080, "resets_at": int(time.time() + 86400)}}
    (d / "rollout-old.jsonl").write_text(json.dumps(
        {"timestamp": "t", "type": "event_msg", "payload": {"type": "token_count", "rate_limits": limits}}) + "\n")
    monkeypatch.setenv("FAKE_START", "39")
    monkeypatch.setenv("FAKE_N", "1")
    # the fresh rollout says 38 but lags; the server says 39
    r, err, asked = _runner(["codex"], tmp_path, {"codex": {"week": q.Reading(39.0, time.time() + 86400)}})
    assert r.run() == 0
    assert asked[0] == "codex" and r.summary.share == 1.0
    fallback, err, asked = _runner(["codex"], tmp_path, {})
    assert fallback.run() == 0 and fallback.summary.baseline_exact


def test_codex_job_live_failure_falls_back(fakebin, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_N", "1")
    r, err, asked = _runner(["codex"], tmp_path, {})
    assert r.run() == 0, err.getvalue()
    assert asked[0] == "codex" and "did not answer; using the local readings" in err.getvalue()
    assert not r.summary.baseline_exact


def test_claude_job_refreshes_stale_baseline_from_usage(fakebin, tmp_path, monkeypatch):
    state = tmp_path / "state"
    state.mkdir()
    (state / "claude_last.json").write_text(json.dumps(
        {"ts": time.time() - 7 * 3600, "readings": {"week": {"used": 30.0, "resets_at": time.time() + 86400}}}))
    monkeypatch.setenv("FAKE_START", "39")   # first statusline reading 40
    monkeypatch.setenv("FAKE_N", "1")
    r, err, asked = _runner(["claude"], tmp_path, {"claude": {"week": q.Reading(38.0, time.time() + 86400)}})
    assert r.run() == 0
    assert asked == ["claude"]
    assert r.summary.baseline_exact and r.summary.share == 2.0


def test_claude_job_asks_even_with_a_fresh_reading(fakebin, tmp_path, monkeypatch):
    # live 12 Nov 2026: a reading 80 s old said 4%, the account was at 7% (another session)
    state = tmp_path / "state"
    state.mkdir()
    (state / "claude_last.json").write_text(json.dumps(
        {"ts": time.time() - 80, "readings": {"week": {"used": 36.0, "resets_at": time.time() + 86400}}}))
    monkeypatch.setenv("FAKE_START", "39")   # first statusline reading 40
    monkeypatch.setenv("FAKE_N", "1")
    r, err, asked = _runner(["claude"], tmp_path, {"claude": {"week": q.Reading(39.0, time.time() + 86400)}})
    assert r.run() == 0
    assert asked == ["claude"] and r.summary.share == 1.0


def test_claude_job_uses_a_fresh_reading_when_usage_fails(fakebin, tmp_path, monkeypatch):
    state = tmp_path / "state"
    state.mkdir()
    (state / "claude_last.json").write_text(json.dumps(
        {"ts": time.time() - 60, "readings": {"week": {"used": 38.0, "resets_at": time.time() + 86400}}}))
    monkeypatch.setenv("FAKE_START", "39")
    monkeypatch.setenv("FAKE_N", "1")
    r, err, asked = _runner(["claude"], tmp_path, {})
    assert r.run() == 0
    assert asked == ["claude"] and r.summary.share == 2.0


def test_codex_final_live_read_counts_the_last_response(fakebin, tmp_path, monkeypatch):
    # live 12 Nov 2026: the rollout said 3% after the job, the server said 4%
    monkeypatch.setenv("FAKE_START", "39")   # the job's only rollout reading: 40
    monkeypatch.setenv("FAKE_N", "1")
    resets = time.time() + 86400
    answers = iter([q.Reading(38.0, resets), q.Reading(40.0, resets), q.Reading(41.0, resets)])
    r, err, asked = _runner(["codex"], tmp_path, {}, quota=3.0, live_poll_s=60)
    r.live_reader = lambda agent: {"week": next(answers)}
    assert r.run() == 3                       # 41 - 38 = 3% >= 3% limit
    assert r.summary.share == 3.0
    receipt = json.loads(next((tmp_path / "receipts").glob("*.json")).read_text())
    assert receipt["final_live"] is True and receipt["reason"] == "quota_exceeded"


def test_codex_live_poll_stops_a_long_response(fakebin, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_N", "0")        # one long response, no rollout reading at all
    monkeypatch.setenv("FAKE_HANG", "20")
    resets = time.time() + 86400
    calls = []

    def reader(agent):
        calls.append(time.time())
        return {"week": q.Reading(38.0 if len(calls) == 1 else 45.0, resets)}

    r, err, asked = _runner(["codex", "exec", "x"], tmp_path, {}, quota=5.0, live_poll_s=0.3)
    r.live_reader = reader
    t0 = time.time()
    assert r.run() == 3
    assert time.time() - t0 < 15
    assert r.summary.share == 7.0 and r.summary.reason == "quota_exceeded"


def test_codex_without_live_keeps_the_old_path(fakebin, tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTCAP_LIVE_QUOTA", "0")
    monkeypatch.setenv("FAKE_N", "1")
    r, err, asked = _runner(["codex"], tmp_path, {"codex": {"week": q.Reading(1.0, None)}})
    assert r.run() == 0
    assert asked == [] and r._poller is None
