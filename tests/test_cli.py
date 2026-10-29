"""CLI surface: cap parsing, Pro gating, license activation, status."""

from __future__ import annotations

import json
import time
from decimal import Decimal

import pytest

import agentcap.cli as cli
import agentcap.license as license_module
from agentcap.ledger import Ledger
from agentcap.license import LicenseNetworkError, LicenseStatus


class FakeRunner:
    instances: list["FakeRunner"] = []

    def __init__(self, argv, **kwargs) -> None:
        self.argv = list(argv)
        self.kwargs = kwargs
        FakeRunner.instances.append(self)

    def run(self) -> int:
        return 0


def write_pro_cache(path):
    """Write a fresh, valid Pro cache entry (source=polar)."""

    path.write_text(
        json.dumps(
            {
                "key": "polar-key",
                "tier": "pro",
                "valid": True,
                "reason": "license valid",
                "checked_at": time.time(),
                "source": "polar",
                "activation_id": None,
            }
        ),
        encoding="utf-8",
    )


@pytest.fixture()
def fake_runner(monkeypatch, tmp_path):
    FakeRunner.instances = []
    monkeypatch.setattr(cli, "Runner", FakeRunner)
    monkeypatch.setenv("AGENTCAP_LICENSE_FILE", str(tmp_path / "license.json"))
    return FakeRunner


def test_run_passes_command_and_default_cap(fake_runner):
    assert cli.main(["run", "--", "echo", "hello"]) == 0
    runner = fake_runner.instances[0]
    assert runner.argv == ["echo", "hello"]
    assert runner.kwargs["cap"] == Decimal("5")


def test_run_cap_comes_from_env(fake_runner, monkeypatch):
    monkeypatch.setenv("AGENTCAP_CAP", "0.25")
    assert cli.main(["run", "--cap", "0.50", "--", "true"]) == 0
    assert fake_runner.instances[0].kwargs["cap"] == Decimal("0.50")

    assert cli.main(["run", "--", "true"]) == 0
    assert fake_runner.instances[1].kwargs["cap"] == Decimal("0.25")


def test_free_tier_refuses_hour_and_day_caps_without_starting(fake_runner, capsys):
    assert cli.main(
        ["run", "--hour-cap", "1", "--day-cap", "2", "--", "true"]
    ) == 2
    assert fake_runner.instances == []
    err = capsys.readouterr().err
    assert "--hour-cap/--day-cap require an active Pro license" in err
    assert "command was not started" in err
    assert 'agentcap license activate "YOUR_LICENSE_KEY"' in err


@pytest.mark.parametrize("flag", ["--hour-cap", "--day-cap"])
def test_free_tier_refuses_each_pro_cap_individually(fake_runner, capsys, flag):
    assert cli.main(["run", flag, "1", "--", "true"]) == 2
    assert fake_runner.instances == []
    assert "require an active Pro license" in capsys.readouterr().err


def test_pro_license_enables_hour_and_day_caps(fake_runner, tmp_path, capsys):
    write_pro_cache(tmp_path / "license.json")
    assert cli.main(
        ["run", "--hour-cap", "1", "--day-cap", "2", "--", "true"]
    ) == 0
    runner = fake_runner.instances[0]
    assert runner.kwargs["hour_cap"] == Decimal("1")
    assert runner.kwargs["day_cap"] == Decimal("2")
    assert "requires AgentCap Pro" not in capsys.readouterr().err


def test_invalid_cap_is_rejected(fake_runner, capsys):
    assert cli.main(["run", "--cap", "not-money", "--", "true"]) == 2
    assert not fake_runner.instances
    assert "--cap must be" in capsys.readouterr().err


def test_run_without_command_is_rejected(fake_runner, capsys):
    assert cli.main(["run", "--cap", "1"]) == 2
    assert not fake_runner.instances
    assert "needs a command" in capsys.readouterr().err


def test_no_clamp_flag_is_parsed(fake_runner):
    assert cli.main(["run", "--", "true"]) == 0
    assert fake_runner.instances[0].kwargs["clamp_output"] is True

    assert cli.main(["run", "--no-clamp", "--", "true"]) == 0
    assert fake_runner.instances[1].kwargs["clamp_output"] is False


def test_trust_workspace_flag_is_passed_only_when_given(fake_runner):
    assert cli.main(["run", "--", "gemini", "-p", "hi"]) == 0
    assert fake_runner.instances[0].kwargs["trust_workspace"] is False

    assert cli.main(["run", "--trust-workspace", "--", "gemini", "-p", "hi"]) == 0
    assert fake_runner.instances[1].kwargs["trust_workspace"] is True


def test_demo_runs_a_simulated_capped_run(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("AGENTCAP_DB", str(tmp_path / "demo.db"))
    assert cli.main(["demo"]) == 3

    captured = capsys.readouterr()
    assert "SIMULATED DEMO" in captured.out
    assert "no real provider" in captured.out
    assert "no real money" in captured.out
    assert captured.out.rstrip().endswith("(simulated)")
    assert "agentcap run summary" in captured.err
    assert "reason: cap_exceeded" in captured.err


def test_license_activate_goes_through_polar_and_caches(monkeypatch, tmp_path, capsys):
    path = tmp_path / "license.json"
    monkeypatch.setenv("AGENTCAP_LICENSE_FILE", str(path))

    class GrantingPolar:
        persist = True

        def activate(self, key):
            return LicenseStatus(True, "pro", "license valid"), "act_123"

        def validate(self, key, activation_id=None):
            return LicenseStatus(True, "pro", "license valid")

    monkeypatch.setattr(license_module, "PolarLicenseValidator", GrantingPolar)

    assert cli.main(["license", "activate", "polar-key-secret"]) == 0
    out = capsys.readouterr().out
    assert "tier: pro" in out
    assert path.exists()

    # The cached result is reused; no second call is needed.
    assert cli.main(["license", "status"]) == 0
    assert "tier: pro" in capsys.readouterr().out


def test_license_activate_wrong_key_exits_one(monkeypatch, tmp_path, capsys):
    path = tmp_path / "license.json"
    monkeypatch.setenv("AGENTCAP_LICENSE_FILE", str(path))

    class DenyingPolar:
        persist = True

        def activate(self, key):
            return LicenseStatus(False, "free", "license key not found"), None

        def validate(self, key, activation_id=None):
            return LicenseStatus(False, "free", "license key not found")

    monkeypatch.setattr(license_module, "PolarLicenseValidator", DenyingPolar)

    assert cli.main(["license", "activate", "wrong"]) == 1
    assert "tier: free" in capsys.readouterr().out
    assert not path.exists()


def test_dev_license_env_var_does_not_unlock_pro(monkeypatch, tmp_path, capsys):
    """Regression: the removed local override must not grant Pro."""

    path = tmp_path / "license.json"
    monkeypatch.setenv("AGENTCAP_LICENSE_FILE", str(path))
    monkeypatch.setenv("AGENTCAP_DEV_LICENSE_KEY", "dev-secret")

    class DenyingPolar:
        persist = True

        def activate(self, key):
            return LicenseStatus(False, "free", "license key not found"), None

        def validate(self, key, activation_id=None):
            return LicenseStatus(False, "free", "license key not found")

    monkeypatch.setattr(license_module, "PolarLicenseValidator", DenyingPolar)

    assert cli.main(["license", "status"]) == 0
    out = capsys.readouterr().out
    assert "tier: free" in out
    assert "dev-secret" not in out
    assert not path.exists()

    assert cli.main(["license", "activate", "dev-secret"]) == 1
    assert "tier: free" in capsys.readouterr().out
    assert not path.exists()


def test_license_status_reads_cached_polar_license(monkeypatch, tmp_path, capsys):
    path = tmp_path / "license.json"
    write_pro_cache(path)
    monkeypatch.setenv("AGENTCAP_LICENSE_FILE", str(path))

    assert cli.main(["license", "status"]) == 0
    out = capsys.readouterr().out
    assert "tier: pro" in out
    assert "polar-key" not in out


def test_license_deactivate_clears_local_cache(monkeypatch, tmp_path, capsys):
    path = tmp_path / "license.json"
    write_pro_cache(path)
    monkeypatch.setenv("AGENTCAP_LICENSE_FILE", str(path))

    assert cli.main(["license", "deactivate"]) == 0
    assert not path.exists()
    assert capsys.readouterr().out.strip() == "license cache cleared"

    # Idempotent: no cache is not an error.
    assert cli.main(["license", "deactivate"]) == 0


def test_license_activate_without_key_fails(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("AGENTCAP_LICENSE_FILE", str(tmp_path / "license.json"))
    assert cli.main(["license", "activate"]) == 2
    err = capsys.readouterr().err
    assert 'Usage: agentcap license activate "YOUR_LICENSE_KEY"' in err
    assert "Polar purchase / customer portal" in err


def test_license_activate_invalid_key_prints_actionable_message(
    monkeypatch, tmp_path, capsys
):
    monkeypatch.setenv("AGENTCAP_LICENSE_FILE", str(tmp_path / "license.json"))

    class DenyingPolar:
        persist = True

        def activate(self, key):
            return LicenseStatus(False, "free", "license key not found"), None

        def validate(self, key, activation_id=None):
            return LicenseStatus(False, "free", "license key not found")

    monkeypatch.setattr(license_module, "PolarLicenseValidator", DenyingPolar)

    assert cli.main(["license", "activate", "wrong-key"]) == 1
    captured = capsys.readouterr()
    assert "tier: free" in captured.out
    assert "License key not found or no longer valid" in captured.err
    assert "kutaykarahan6@gmail.com" in captured.err
    assert "wrong-key" not in captured.out + captured.err


def test_license_status_prints_cache_path_but_never_the_key(
    monkeypatch, tmp_path, capsys
):
    path = tmp_path / "license.json"
    write_pro_cache(path)
    monkeypatch.setenv("AGENTCAP_LICENSE_FILE", str(path))

    assert cli.main(["license", "status"]) == 0
    out = capsys.readouterr().out
    assert f"cache path: {path}" in out
    assert "polar-key" not in out


def test_license_activate_save_error_is_reported_without_traceback(
    monkeypatch, tmp_path, capsys
):
    path = tmp_path / "license.json"
    monkeypatch.setenv("AGENTCAP_LICENSE_FILE", str(path))

    class GrantingPolar:
        persist = True

        def activate(self, key):
            return LicenseStatus(True, "pro", "license valid"), "act_123"

        def validate(self, key, activation_id=None):
            return LicenseStatus(True, "pro", "license valid")

    monkeypatch.setattr(license_module, "PolarLicenseValidator", GrantingPolar)

    def fail_write(*args, **kwargs):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(license_module.LicenseManager, "_write", fail_write)

    assert cli.main(["license", "activate", "polar-key-secret"]) == 1
    captured = capsys.readouterr()
    assert captured.err.startswith("agentcap: license validated, but could not save"
                                   " activation to ")
    assert str(path) in captured.err
    assert "No space left on device" in captured.err
    assert "Check write permission and free disk space." in captured.err
    assert "Traceback" not in captured.err
    assert "polar-key-secret" not in captured.out + captured.err


def test_license_deactivate_clear_error_is_reported_without_traceback(
    monkeypatch, tmp_path, capsys
):
    path = tmp_path / "license.json"
    monkeypatch.setenv("AGENTCAP_LICENSE_FILE", str(path))

    def fail_clear(self):
        raise OSError(13, "Permission denied")

    monkeypatch.setattr(license_module.LicenseManager, "clear", fail_clear)

    assert cli.main(["license", "deactivate"]) == 1
    captured = capsys.readouterr()
    assert "license validated, but could not save activation" in captured.err
    assert str(path) in captured.err
    assert "Permission denied" in captured.err
    assert "Traceback" not in captured.err


@pytest.mark.parametrize("platform_name", ["nt"])
def test_run_refuses_windows_before_starting(
    monkeypatch, fake_runner, capsys, platform_name
):
    monkeypatch.setattr(cli.os, "name", platform_name)
    assert cli.main(["run", "--", "true"]) == 2
    assert fake_runner.instances == []
    err = capsys.readouterr().err
    assert "Windows is not supported yet" in err
    assert "Use WSL2, macOS or Linux." in err


def test_run_refuses_windows_when_only_sys_platform_is_win(
    monkeypatch, fake_runner, capsys
):
    monkeypatch.setattr(cli.sys, "platform", "win32")
    assert cli.main(["run", "--", "true"]) == 2
    assert fake_runner.instances == []
    assert "Windows is not supported yet" in capsys.readouterr().err


def test_demo_refuses_windows_before_starting(monkeypatch, capsys):
    monkeypatch.setattr(cli.os, "name", "nt")
    assert cli.main(["demo"]) == 2
    captured = capsys.readouterr()
    assert "Windows is not supported yet" in captured.err
    assert "SIMULATED DEMO" not in captured.out


def test_license_activate_network_failure_exits_one_without_traceback(
    monkeypatch, tmp_path, capsys
):
    """Regression: an unreachable licence server must not dump a traceback."""

    path = tmp_path / "license.json"
    monkeypatch.setenv("AGENTCAP_LICENSE_FILE", str(path))

    class UnreachableValidator:
        persist = True

        def validate(self, key, activation_id=None):
            raise LicenseNetworkError("license server unreachable (ConnectError)")

        def activate(self, key):
            raise LicenseNetworkError("license server unreachable (ConnectError)")

    monkeypatch.setattr(license_module, "PolarLicenseValidator", UnreachableValidator)

    raised: BaseException | None = None
    code = None
    try:
        code = cli.main(["license", "activate", "polar-key-secret"])
    except BaseException as exc:  # pragma: no cover - regression guard
        raised = exc
    captured = capsys.readouterr()

    assert raised is None
    assert code == 1
    assert captured.err == ""
    assert "Traceback" not in captured.err
    assert "tier: free" in captured.out
    assert "valid: False" in captured.out
    assert "could not reach the license server (LicenseNetworkError)" in captured.out
    assert "polar-key-secret" not in captured.out
    assert not path.exists()


def test_status_db_reads_todays_spend(monkeypatch, tmp_path, capsys):
    ledger_path = tmp_path / "status.db"
    monkeypatch.setenv("AGENTCAP_DB", str(ledger_path))
    ledger = Ledger(ledger_path)
    reservation = ledger.reserve("run-1", "openai", "gpt-4o", Decimal("0.25"))
    ledger.settle(reservation, Decimal("0.125"))

    assert cli.main(["status", "--db"]) == 0
    out = capsys.readouterr().out
    assert f"db: {ledger_path}" in out
    assert "today: $0.1250" in out
    assert "last hour: $0.1250" in out


def test_pricing_shows_schedule_and_verification(capsys):
    assert cli.main(["pricing", "google/gemini-4-argon"]) == 0
    out = capsys.readouterr().out
    assert "verified=2026-10-25" in out
    assert "scheduled price changes" in out
    assert "from 2026-12-01" in out
