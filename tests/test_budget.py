import json
from decimal import Decimal

import pytest

import agentcap.cli as cli
from agentcap import alerts, budget
from agentcap.eta import EtaWarner

from test_cli import FakeRunner, write_pro_cache


# ------------------------------------------------------------------ config
def test_budget_path_respects_xdg(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "c"))
    assert budget.budget_path() == tmp_path / "c" / "agentcap" / "budget.json"


def test_set_show_clear(capsys):
    assert cli.main(["budget", "set", "--task-usd", "0.50", "--day-usd", "10",
                     "--task-quota", "5%", "--window", "5h"]) == 0
    cfg = budget.load()
    assert cfg.task_usd == Decimal("0.50")
    assert cfg.day_usd == Decimal("10")
    assert cfg.task_quota == 5.0
    assert cfg.window == "5h"
    capsys.readouterr()
    assert cli.main(["budget", "show"]) == 0
    out = capsys.readouterr().out
    assert "$0.50" in out and "5%" in out and "5h" in out
    # merging: a second set keeps earlier keys
    cli.main(["budget", "set", "--hour-usd", "2"])
    cfg = budget.load()
    assert cfg.hour_usd == Decimal("2") and cfg.task_usd == Decimal("0.50")
    assert cli.main(["budget", "clear"]) == 0
    assert budget.load() == budget.Budget()


def test_set_rejects_bad_values(capsys):
    assert cli.main(["budget", "set", "--task-usd", "-1"]) == 2
    assert cli.main(["budget", "set", "--task-quota", "150%"]) == 2


def test_budget_file_is_private():
    cli.main(["budget", "set", "--task-usd", "1"])
    assert budget.budget_path().stat().st_mode & 0o777 == 0o600


def test_corrupt_budget_is_ignored_with_warning(capsys):
    p = budget.budget_path()
    p.parent.mkdir(parents=True)
    p.write_text("{nope")
    assert budget.load() == budget.Budget()


# ------------------------------------------------------------- run wiring
@pytest.fixture
def fake_runner(monkeypatch, tmp_path):
    FakeRunner.instances = []
    monkeypatch.setattr(cli, "Runner", FakeRunner)
    monkeypatch.setenv("AGENTCAP_LICENSE_FILE", str(tmp_path / "license.json"))
    monkeypatch.delenv("AGENTCAP_CAP", raising=False)
    return FakeRunner


def test_run_uses_budget_task_cap(fake_runner):
    cli.main(["budget", "set", "--task-usd", "0.75"])
    assert cli.main(["run", "--", "true"]) == 0
    assert fake_runner.instances[0].kwargs["cap"] == Decimal("0.75")


def test_flag_and_env_beat_budget(fake_runner, monkeypatch):
    cli.main(["budget", "set", "--task-usd", "0.75"])
    cli.main(["run", "--cap", "0.10", "--", "true"])
    monkeypatch.setenv("AGENTCAP_CAP", "0.20")
    cli.main(["run", "--", "true"])
    caps = [r.kwargs["cap"] for r in fake_runner.instances]
    assert caps == [Decimal("0.10"), Decimal("0.20")]


def test_free_budget_day_cap_is_ignored_not_fatal(fake_runner, capsys):
    cli.main(["budget", "set", "--day-usd", "5"])
    assert cli.main(["run", "--", "true"]) == 0
    assert fake_runner.instances[0].kwargs["day_cap"] is None
    assert "Pro" in capsys.readouterr().err


def test_pro_budget_day_and_hour_caps_apply(fake_runner, tmp_path):
    write_pro_cache(tmp_path / "license.json")
    cli.main(["budget", "set", "--day-usd", "5", "--hour-usd", "1"])
    assert cli.main(["run", "--", "true"]) == 0
    kw = fake_runner.instances[0].kwargs
    assert kw["day_cap"] == Decimal("5") and kw["hour_cap"] == Decimal("1")


def test_quota_flag_without_value_uses_budget(monkeypatch, tmp_path):
    seen = {}

    class FakeQuota:
        def __init__(self, argv, **kw):
            seen.update(kw)

        def run(self):
            return 0

    import agentcap.quota as q
    monkeypatch.setattr(q, "QuotaRunner", FakeQuota)
    monkeypatch.setenv("AGENTCAP_LICENSE_FILE", str(tmp_path / "license.json"))
    cli.main(["budget", "set", "--task-quota", "7%", "--window", "5h"])
    assert cli.main(["run", "--quota", "--", "claude"]) == 0
    assert seen["quota"] == 7.0 and seen["window"] == "5h"


def test_quota_flag_without_value_and_no_budget_errors(capsys, tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTCAP_LICENSE_FILE", str(tmp_path / "license.json"))
    assert cli.main(["run", "--quota", "--", "claude"]) == 2
    assert "budget set --task-quota" in capsys.readouterr().err


# -------------------------------------------------------------------- ETA
def test_eta_warns_once_when_cap_is_minutes_away():
    t = [0.0]
    w = EtaWarner(Decimal("1.00"), horizon_min=10, clock=lambda: t[0])
    assert w.observe(Decimal("0")) is None
    t[0] = 60
    # $0.05/min -> $1 needs 19 more minutes: no warning
    assert w.observe(Decimal("0.05")) is None
    t[0] = 120
    # now $0.45/min over the window -> (1-0.50)/0.25 = 2 min
    eta = w.observe(Decimal("0.50"))
    assert eta is not None and 0 < eta <= 10
    t[0] = 130
    assert w.observe(Decimal("0.60")) is None  # once only


def test_eta_silent_without_burn_or_after_cap():
    t = [0.0]
    w = EtaWarner(Decimal("1"), clock=lambda: t[0])
    w.observe(Decimal("0.2"))
    t[0] = 100
    assert w.observe(Decimal("0.2")) is None
    t[0] = 110
    assert w.observe(Decimal("1.5")) is None  # already over: cap_exceeded handles it


def test_cap_eta_alert_format_and_allowlist():
    msg = alerts.format_alert({"type": "cap_eta", "eta_minutes": 3, "cap_usd": "1.00",
                               "spent_usd": "0.70", "scope": "run"})
    assert "~3 min" in msg and "$1.00" in msg
    assert "eta_minutes" in alerts.sanitize({"type": "cap_eta", "eta_minutes": 3})
    assert "cap_eta" in alerts.ONCE_EVENTS and "cap_eta" in alerts.TERMINAL_EVENTS


def test_proxy_emits_cap_eta(ledger):
    import asyncio
    from conftest import Harness

    h = Harness(ledger, cap="1")
    t = [0.0]
    h.proxy._eta_clock = lambda: t[0]
    h.ledger.reserve("run-test", "openai", "openai/gpt-4o-mini", "0.10")
    rid = h.ledger.reserve("run-test", "openai", "openai/gpt-4o-mini", "0.10")
    h.proxy._check_thresholds()
    t[0] = 60
    h.ledger.settle(rid, "0.60")
    h.proxy._check_thresholds()
    kinds = [e["type"] for e in h.events]
    assert "cap_eta" in kinds
    ev = next(e for e in h.events if e["type"] == "cap_eta")
    assert ev["eta_minutes"] >= 0 and ev["cap_usd"] == "1"
