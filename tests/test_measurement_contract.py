"""AgentCap 0.4 measurement contract: one reading record, four states, no fake precision.

These tests drive the same surfaces a user sees -- the statusline, `agentcap
quota`, the runner's terminal output, the plugin's message and the receipt --
and prove the rules in docs/MEASUREMENT_CONTRACT_0.4.md:

* every reading carries source, read_at, max_age, window, percent, resolution
  and observed, and there is no second internal shape;
* a stale reading is never refreshed by re-reading it, only by a new reading;
* no reading before the first one never reads as "protection active" and
  writes no stop-claiming receipt;
* a window reset clears the baseline and is not counted as consumption;
* whole-percent sources always print their ±1-3% uncertainty;
* a lost source reads "data stale (protection not verified)", never protected;
* starting / protected / stale / stopped all come from one mapping function and
  the same text reaches status, summary, the plugin and the receipt.

No live model or network call is made: fake `claude`/`codex` executables write
the documented local schemas only.
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import stat
import sys
import textwrap
import time
from pathlib import Path

import pytest

from agentcap import quota as q

NOW = 1_793_000_000.0
ROOT = Path(__file__).resolve().parents[1]

FAKE_CLAUDE = textwrap.dedent('''\
    #!__PYTHON__
    """Fake Claude Code: runs the injected statusline and PreToolUse hook."""
    import json, os, subprocess, sys, time
    args = sys.argv[1:]
    settings = json.load(open(args[args.index("--settings") + 1]))
    status_cmd = settings["statusLine"]["command"]
    hook_cmd = settings["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
    mode = os.environ.get("MC_MODE", "under")
    week = float(os.environ.get("MC_START", "40"))
    step = float(os.environ.get("MC_STEP", "1"))
    n = int(os.environ.get("MC_N", "3"))
    if mode == "none":
        sys.exit(0)

    def payload(value):
        return {"session_id": "mc-sess", "model": {"display_name": "Opus"},
                "rate_limits": {
                    "five_hour": {"used_percentage": 10, "resets_at": time.time() + 3600},
                    "seven_day": {"used_percentage": value, "resets_at": time.time() + 86400}}}

    subprocess.run(status_cmd, shell=True, input=json.dumps(payload(week)),
                   capture_output=True, text=True)
    if mode == "stale":
        time.sleep(float(os.environ.get("MC_SLEEP", "2")))
        sys.exit(0)
    for i in range(n):
        week += step
        subprocess.run(status_cmd, shell=True, input=json.dumps(payload(week)),
                       capture_output=True, text=True)
        res = subprocess.run(
            hook_cmd, shell=True,
            input=json.dumps({"hook_event_name": "PreToolUse", "tool_name": "Bash",
                              "tool_input": {"command": f"step {i}"}}),
            capture_output=True, text=True).stdout.strip()
        if res:
            data = json.loads(res)
            if data.get("continue") is False:
                sys.exit(0)
        time.sleep(0.05)
    sys.exit(0)
''')

FAKE_CODEX = textwrap.dedent('''\
    #!__PYTHON__
    """Fake Codex: writes documented rollout token_count lines, including a reset."""
    import datetime, json, os, sys, time
    home = os.environ["CODEX_HOME"]
    day = datetime.date.today()
    d = os.path.join(home, "sessions", f"{day:%Y}", f"{day:%m}", f"{day:%d}")
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, "rollout-%d-mc.jsonl" % time.time())
    fh = open(path, "a")

    def line(week, resets):
        fh.write(json.dumps({"type": "event_msg", "payload": {
            "type": "token_count", "info": {}, "rate_limits": {
                "primary": {"used_percent": 10.0, "window_minutes": 300,
                            "resets_at": int(time.time() + 3600)},
                "secondary": {"used_percent": week, "window_minutes": 10080,
                              "resets_at": int(resets)}}}}) + "\\n")
        fh.flush()

    line(40, time.time() + 86400)
    time.sleep(0.1)
    line(41, time.time() + 86400)
    time.sleep(0.1)
    line(1, time.time() + 7 * 86400)   # the window reset: the account starts over
    time.sleep(0.1)
    line(2, time.time() + 7 * 86400)
    fh.write(json.dumps({"type": "event_msg", "payload": {"type": "task_complete"}}) + "\\n")
    fh.flush()
''')


@pytest.fixture
def fakebin(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, src in (("claude", FAKE_CLAUDE), ("codex", FAKE_CODEX)):
        exe = bin_dir / name
        exe.write_text(src.replace("__PYTHON__", sys.executable))
        exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("AGENTCAP_QUOTA_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    monkeypatch.setenv("PYTHONPATH", str(ROOT / "src") + os.pathsep + os.environ.get("PYTHONPATH", ""))
    return bin_dir


def make_runner(argv, tmp_path, **kw):
    events: list[dict] = []
    err = io.StringIO()
    runner = q.QuotaRunner(
        argv, quota=kw.pop("quota", 5.0), alerts=events.append, stderr=err, poll_s=0.02,
        claude_grace_s=0.4, final_read_s=1.0, receipt_dir=tmp_path / "receipts",
        cwd=tmp_path, home=tmp_path / "home", **kw,
    )
    return runner, events, err


def receipt(tmp_path):
    files = list((tmp_path / "receipts").glob("*.json"))
    assert len(files) == 1, files
    return json.loads(files[0].read_text())


def claude_payload(week=41.0, five=12.0, resets=2_000_000_000):
    return {
        "session_id": "s1", "model": {"display_name": "Opus"},
        "rate_limits": {
            "five_hour": {"used_percentage": five, "resets_at": resets - 3600},
            "seven_day": {"used_percentage": week, "resets_at": resets},
        },
    }


def codex_line(week=40.0, resets=2_000_000_000):
    return {"type": "event_msg", "payload": {
        "type": "token_count", "info": {}, "rate_limits": {
            "primary": {"used_percent": 10.0, "window_minutes": 300, "resets_at": resets - 3600},
            "secondary": {"used_percent": week, "window_minutes": 10080, "resets_at": resets},
        }}}


# ------------------------------------------------------- one reading record
def test_reading_record_fields_table():
    """Item 1: every source produces the same seven fields, one representation."""

    cases = [
        ("claude", q.claude_readings(claude_payload(week=41.5), now=NOW)["week"], 41.5),
        ("codex", q.codex_readings(codex_line(week=42.5), now=NOW)["week"], 42.5),
    ]
    for source, reading, expected in cases:
        assert reading.source == source
        assert reading.window == "week"
        assert reading.percent == expected
        assert reading.used == expected  # legacy alias, same value
        assert reading.read_at == NOW
        assert reading.max_age == q.READING_MAX_AGE_S[source]
        assert reading.resolution == 1.0
        assert reading.observed is True
        data = reading.to_dict()
        for field in ("source", "read_at", "max_age", "window", "percent", "resolution", "observed"):
            assert field in data, field
        assert q.Reading.from_dict(data) == reading
        assert q.Reading.from_dict({"used": 7.0, "resets_at": 1}).percent == 7.0


def test_protection_state_table():
    """Items 2/3/6: the state comes from age and evidence, never from hope."""

    fresh = q.Reading(40.0, NOW + 100, source="claude", read_at=NOW,
                      max_age=60.0, window="week")
    old = q.Reading(40.0, NOW + 100, source="claude", read_at=NOW - 61,
                    max_age=60.0, window="week")
    cases = [
        # (reading, now, started_at, stopped, source_lost, expected)
        (None, NOW, NOW, False, False, q.STATE_STARTING),          # first reading still owed
        (None, NOW + 91, NOW, False, False, q.STATE_STALE),        # grace over, still nothing
        (fresh, NOW + 10, NOW, False, False, q.STATE_PROTECTED),
        (fresh, NOW + 61, NOW, False, False, q.STATE_STALE),       # older than max_age
        (fresh, NOW + 10, NOW, True, False, q.STATE_STOPPED),      # a stop wins over freshness
        (fresh, NOW + 10, NOW, False, True, q.STATE_STALE),        # source went away
    ]
    for reading, now, started, stopped, lost, expected in cases:
        prot = q.protection_state(started_at=started, now=now, reading=reading,
                                  stopped=stopped, source_lost=lost)
        assert prot.state == expected, (reading, now, stopped, lost)
        assert prot.text == q.STATE_TEXT[expected]
        assert prot.observed is (reading is not None)


def test_stale_reading_is_not_refreshed_by_reading_it(tmp_path, monkeypatch):
    """Item 2: only a new reading moves read_at; a cached file does not."""

    monkeypatch.setenv("AGENTCAP_QUOTA_DIR", str(tmp_path / "state"))
    job = tmp_path / "job"
    job.mkdir()
    (job / "job.json").write_text(json.dumps(
        {"quota": 5, "window": "week", "started_at": NOW, "enforce": True}))
    out = io.StringIO()
    q.statusline_main(job, io.StringIO(json.dumps(claude_payload(week=40.0))), out, now=NOW)
    first = json.loads((job / "status.json").read_text())
    assert first["read_at"] == NOW and first["readings"]["week"]["read_at"] == NOW

    reading = q.Reading.from_dict(first["readings"]["week"])
    assert q.protection_state(started_at=NOW, now=NOW + 30, reading=reading).state == q.STATE_PROTECTED
    assert q.protection_state(started_at=NOW, now=NOW + 61, reading=reading).state == q.STATE_STALE
    assert reading.read_at == NOW  # the value itself never moved

    # A render without rate_limits (the statusline still runs) must not refresh it.
    q.statusline_main(job, io.StringIO(json.dumps({"session_id": "s1", "cost": {"total_api_duration_ms": 5}})),
                      io.StringIO(), now=NOW + 70)
    second = json.loads((job / "status.json").read_text())
    assert second["read_at"] == NOW
    assert second["readings"]["week"]["read_at"] == NOW
    assert second["state"] == q.STATE_STALE and second["state_text"] == q.STATE_TEXT[q.STATE_STALE]

    # A genuinely new reading moves it forward.
    q.statusline_main(job, io.StringIO(json.dumps(claude_payload(week=41.0))),
                      io.StringIO(), now=NOW + 80)
    third = json.loads((job / "status.json").read_text())
    assert third["read_at"] == NOW + 80 and third["state"] == q.STATE_PROTECTED


# --------------------------------------------------- startup and source loss
def test_startup_writes_no_receipt_and_claims_no_protection(fakebin, tmp_path, monkeypatch):
    """Item 3: before the first reading there is no 'protection active' and no receipt."""

    monkeypatch.setenv("MC_MODE", "none")
    runner, events, err = make_runner(["claude"], tmp_path, quota=5.0)
    assert runner.run() == 0
    text = err.getvalue()
    assert q.STATE_TEXT[q.STATE_STARTING] in text
    assert q.STATE_TEXT[q.STATE_PROTECTED] not in text
    assert "no quota receipt" in text
    assert not list((tmp_path / "receipts").glob("*.json"))
    assert runner.summary.observed is False
    assert runner.summary.protection.state == q.STATE_STARTING
    assert "quota_receipt" not in [e["type"] for e in events]


def test_lost_statusline_goes_stale_and_the_receipt_says_so(fakebin, tmp_path, monkeypatch):
    """Items 2/6: a statusline that stops reporting never stays 'protected'."""

    monkeypatch.setenv("MC_MODE", "stale")
    monkeypatch.setenv("MC_SLEEP", "1.2")
    runner, _events, err = make_runner(["claude", "-p", "x"], tmp_path, quota=50.0,
                                       reading_max_age_s=0.3)
    assert runner.run() == 0
    text = err.getvalue()
    assert q.STATE_TEXT[q.STATE_PROTECTED] in text          # it was fresh once
    assert q.STATE_TEXT[q.STATE_STALE] in text              # then the source stopped
    assert text.index(q.STATE_TEXT[q.STATE_PROTECTED]) < text.index(q.STATE_TEXT[q.STATE_STALE])
    data = receipt(tmp_path)
    assert data["state"] == q.STATE_STALE
    assert data["state_text"] == q.STATE_TEXT[q.STATE_STALE]
    assert data["state_text"] != q.STATE_TEXT[q.STATE_PROTECTED]
    assert data["reading_age_s"] >= 0.3 and data["max_age_s"] == 0.3
    assert runner.summary.protection.state == q.STATE_STALE


def test_codex_source_loss_still_fails_closed_at_exit_5(tmp_path, monkeypatch):
    """Item 6: the existing strict fail-closed exit 5 is preserved."""

    exe = tmp_path / "bin" / "codex"
    exe.parent.mkdir()
    exe.write_text(textwrap.dedent(f'''\
        #!{sys.executable}
        import datetime, json, os, time
        home = os.environ["CODEX_HOME"]; day = datetime.date.today()
        d = os.path.join(home, "sessions", f"{{day:%Y}}", f"{{day:%m}}", f"{{day:%d}}")
        os.makedirs(d, exist_ok=True)
        fh = open(os.path.join(d, "rollout-%d.jsonl" % time.time()), "a")
        for _ in range(3):
            fh.write(json.dumps({{"type": "event_msg", "payload": {{"type": "token_count", "info": {{}}}}}}) + "\\n")
            fh.flush(); time.sleep(0.1)
        time.sleep(0.2)
    '''))
    exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", f"{exe.parent}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("AGENTCAP_QUOTA_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    runner, _events, err = make_runner(["codex", "exec", "x"], tmp_path, quota=5.0)
    assert runner.run() == q.EXIT_GUARD_FAILED
    assert runner.summary.observed is False
    data = receipt(tmp_path)
    assert data["exit_code"] == 5 and data["reason"] == "guard_failed"
    assert data["state"] == q.STATE_STALE and data["state_text"] != q.STATE_TEXT[q.STATE_PROTECTED]
    assert data["share_pct"] is None and data["measure"] == "none"
    assert q.STATE_TEXT[q.STATE_PROTECTED] not in err.getvalue()


# -------------------------------------------------------------------- reset
def test_reset_clears_the_baseline_and_is_not_consumption():
    """Item 4: a drop that is a reset restarts at zero and never goes negative."""

    cases = [
        # (readings, expected share, expected resets)
        ([(12.0, 100), (13.0, 100), (12.0, 100)], 1.0, 0),       # same-window jitter
        ([(40.0, 100), (44.0, 100), (1.0, 20000)], 5.0, 1),      # reset: 4% carried + 1%
        ([(50.0, 100), (2.0, 20000)], 2.0, 1),                   # reset with nothing used
        ([(40.0, 100), (1.0, 20000), (0.0, 20000)], 1.0, 1),     # 0% after a reset is not negative
    ]
    for readings, expected, resets in cases:
        tracker = q.ShareTracker()
        for used, resets_at in readings:
            tracker.update(q.Reading(used, resets_at))
        assert tracker.share == pytest.approx(expected), readings
        assert tracker.share >= 0.0
        assert tracker.resets_seen == resets, readings
        if resets:
            assert tracker.baseline == 0.0, readings


def test_codex_reset_is_shown_and_not_counted_as_usage(fakebin, tmp_path):
    """Item 4, integration: the user sees the reset; the share is 1+1, not a jump."""

    runner, _events, err = make_runner(["codex"], tmp_path, quota=50.0)
    assert runner.run() == 0
    text = err.getvalue()
    assert "window reset" in text and "adds no usage" in text
    assert runner.summary.resets_seen == 1
    assert runner.summary.share == pytest.approx(3.0)
    data = receipt(tmp_path)
    assert data["window_resets"] == 1 and data["share_pct"] == 3.0


# --------------------------------------------------------------- resolution
def test_resolution_is_printed_without_fake_precision(fakebin, tmp_path):
    """Item 5: ±1-3% on the live line, the read-only view and the receipt."""

    runner, _events, err = make_runner(["claude", "-p", "x"], tmp_path, quota=50.0)
    assert runner.run() == 0
    assert q.uncertainty_text() == "±1-3%"
    assert "±1-3%" in err.getvalue()
    data = receipt(tmp_path)
    assert data["resolution_pct"] == 1.0
    assert data["uncertainty_pct"] == {"low": 1.0, "high": 3.0}

    # The live statusline line carries the same marker.
    job = tmp_path / "sl-job"
    job.mkdir()
    (job / "job.json").write_text(json.dumps(
        {"quota": 5, "window": "week", "started_at": time.time(), "enforce": True}))
    out = io.StringIO()
    q.statusline_main(job, io.StringIO(json.dumps(claude_payload(week=41.0))), out, now=time.time())
    assert "±1-3%" in out.getvalue()

    state = tmp_path / "state"
    state.mkdir(exist_ok=True)
    (state / "claude_last.json").write_text(json.dumps(
        {"ts": time.time(), "readings": {"week": q.Reading(
            41.0, time.time() + 3600, source="claude", read_at=time.time(),
            max_age=60.0, window="week").to_dict()}}))
    lines = q.quota_status(now=time.time(), sessions_dir=tmp_path / "empty")
    assert any("claude week: 41.0% used ±1-3%" in line for line in lines)


def test_free_never_claims_protection(fakebin, tmp_path, monkeypatch):
    """Observe-only is not protection: the same state, but the tier text says so."""

    runner, _events, err = make_runner(["claude"], tmp_path, quota=50.0, enforce=False)
    assert runner.run() == 0
    text = err.getvalue()
    assert q.STATE_TEXT[q.STATE_PROTECTED] not in text
    assert q.WATCH_ONLY_TEXT in text

    monkeypatch.setenv("AGENTCAP_QUOTA_DIR", str(tmp_path / "state2"))
    monkeypatch.setenv("AGENTCAP_LICENSE_FILE", str(tmp_path / "nolicense.json"))
    cc = _load_plugin()
    fresh = q.Reading(41.0, NOW + 100, source="claude", read_at=NOW, max_age=60.0, window="week")
    line = cc.share_line({"quota": 5.0, "window": "week", "share": 1.0, "used": 41.0,
                          "last_readings": {"week": fresh.to_dict()}, "set_at": NOW}, now=NOW + 5)
    assert q.WATCH_ONLY_TEXT in line and q.STATE_TEXT[q.STATE_PROTECTED] not in line


# ------------------------------------------------------- one mapping, one text
def _load_plugin():
    spec = importlib.util.spec_from_file_location("agentcap_cc_mc", ROOT / "scripts" / "agentcap_cc.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_four_states_come_from_one_function_and_one_text(fakebin, tmp_path, monkeypatch):
    """Item 7: status, summary, plugin and receipt render STATE_TEXT, not their own words."""

    monkeypatch.setenv("AGENTCAP_QUOTA_DIR", str(tmp_path / "state"))
    pro = tmp_path / "license.json"
    pro.write_text(json.dumps({"source": "polar", "valid": True, "tier": "pro", "key": "X",
                              "checked_at": 1, "reason": "ok"}))
    monkeypatch.setenv("AGENTCAP_LICENSE_FILE", str(pro))
    cc = _load_plugin()

    fresh = q.Reading(41.0, NOW + 100, source="claude", read_at=NOW, max_age=60.0, window="week")
    base = {"quota": 5.0, "window": "week", "share": 1.0, "used": 41.0,
            "last_readings": {"week": fresh.to_dict()}, "set_at": NOW}

    def line(**over):
        return cc.share_line({**base, **over}, now=NOW + 5)

    assert q.STATE_TEXT[q.STATE_PROTECTED] in line()
    assert q.STATE_TEXT[q.STATE_STALE] in line(last_readings={
        "week": q.Reading(41.0, NOW + 100, source="claude", read_at=NOW - 61,
                          max_age=60.0, window="week").to_dict()})
    assert q.STATE_TEXT[q.STATE_STOPPED] in line(stopped=True)
    assert q.STATE_TEXT[q.STATE_STARTING] in cc.share_line(
        {"quota": 5.0, "window": "week", "set_at": NOW}, now=NOW + 1)

    # The read-only status uses the same text.
    state = tmp_path / "state"
    state.mkdir(exist_ok=True)
    (state / "claude_last.json").write_text(json.dumps(
        {"ts": NOW, "readings": {"week": fresh.to_dict()}}))
    status = q.quota_status(now=NOW + 5, sessions_dir=tmp_path / "empty")
    assert any(q.STATE_TEXT[q.STATE_PROTECTED] in line_ for line_ in status)

    # A real quota stop: the summary and the receipt carry the same "stopped".
    monkeypatch.setenv("MC_MODE", "stop")
    monkeypatch.setenv("MC_START", "40")
    monkeypatch.setenv("MC_N", "6")
    runner, _events, err = make_runner(["claude"], tmp_path, quota=5.0)
    assert runner.run() == q.EXIT_QUOTA_EXCEEDED
    assert runner.summary.protection.state == q.STATE_STOPPED
    assert q.STATE_TEXT[q.STATE_STOPPED] in err.getvalue()
    data = receipt(tmp_path)
    assert data["state_text"] == runner.summary.protection.text == q.STATE_TEXT[q.STATE_STOPPED]
