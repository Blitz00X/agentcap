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


def test_free_tier_refuses_hour_and_day_caps(fake_runner, capsys):
    # Hour/day caps are Pro; Free exits 2 before starting anything.
    assert cli.main(
        ["run", "--hour-cap", "1", "--day-cap", "2", "--", "true"]
    ) == 2
    assert len(fake_runner.instances) == 0
    err = capsys.readouterr().err
    assert "--hour-cap/--day-cap require an active Pro license" in err
    assert "$12 one-time (launch week, until 10 Nov; then $19)" in err


@pytest.mark.parametrize("flag", ["--hour-cap", "--day-cap"])
def test_free_tier_refuses_each_cap_individually(fake_runner, capsys, flag):
    assert cli.main(["run", flag, "1", "--", "true"]) == 2
    assert len(fake_runner.instances) == 0
    assert "require an active Pro license" in capsys.readouterr().err


def test_free_tier_alerts_are_gated_with_an_upgrade_hint(
    fake_runner, monkeypatch, capsys
):
    monkeypatch.setenv("AGENTCAP_WEBHOOK_URL", "https://example.invalid/hook")

    assert cli.main(["run", "--", "true"]) == 0
    # The run still starts; only the Pro delivery channel is disabled.
    assert len(fake_runner.instances) == 1
    err = capsys.readouterr().err
    assert "webhook/Telegram alerts require an active Pro license" in err
    assert "is $12 one-time (launch week, until 10 Nov; then $19): https://agentcap.launchbench.studio/#pricing" in err


def test_pro_alerts_do_not_print_the_upgrade_hint(
    fake_runner, monkeypatch, tmp_path, capsys
):
    write_pro_cache(tmp_path / "license.json")
    monkeypatch.setenv("AGENTCAP_WEBHOOK_URL", "https://example.invalid/hook")

    assert cli.main(["run", "--", "true"]) == 0
    err = capsys.readouterr().err
    assert "require an active Pro license" not in err
    assert "hint: Pro is $12 one-time" not in err


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
    write_pro_cache(path)  # older cache entry: no activation_id
    monkeypatch.setenv("AGENTCAP_LICENSE_FILE", str(path))

    assert cli.main(["license", "deactivate"]) == 0
    assert not path.exists()
    out = capsys.readouterr().out.strip()
    assert "local license removed" in out
    assert "older version" in out
    assert "Polar customer portal" in out
    assert "polar-key" not in out

    # Idempotent: no cache is not an error.
    assert cli.main(["license", "deactivate"]) == 0
    assert "nothing to deactivate" in capsys.readouterr().out


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
    write_pro_cache(path)  # legacy entry: removal is local-only
    monkeypatch.setenv("AGENTCAP_LICENSE_FILE", str(path))

    def fail_clear(self):
        raise OSError(13, "Permission denied")

    monkeypatch.setattr(license_module.LicenseManager, "clear", fail_clear)

    assert cli.main(["license", "deactivate"]) == 1
    captured = capsys.readouterr()
    assert "could not remove the local license" in captured.err
    assert str(path) in captured.err
    assert "Permission denied" in captured.err
    assert "Traceback" not in captured.err


@pytest.fixture()
def deactivation(monkeypatch, tmp_path):
    """A fake Polar validator for `license deactivate` + the cache path."""

    state: dict = {"status": 204, "error": None, "calls": []}

    class StubPolar:
        persist = True

        def __init__(self, *args, **kwargs) -> None:
            pass

        def validate(self, key, activation_id=None):
            raise AssertionError("deactivate must not re-validate the key")

        def activate(self, key):
            raise AssertionError("deactivate must not activate")

        def deactivate(self, key, activation_id):
            state["calls"].append((key, activation_id))
            if state["error"] is not None:
                raise state["error"]
            return state["status"]

    monkeypatch.setattr(license_module, "PolarLicenseValidator", StubPolar)
    path = tmp_path / "license.json"
    monkeypatch.setenv("AGENTCAP_LICENSE_FILE", str(path))
    return state, path


def _write_cache_with_activation(path, activation_id="act_cached", key="polar-key"):
    path.write_text(
        json.dumps(
            {
                "key": key,
                "tier": "pro",
                "valid": True,
                "reason": "license valid",
                "checked_at": time.time(),
                "source": "polar",
                "activation_id": activation_id,
            }
        ),
        encoding="utf-8",
    )
    return path


def test_license_deactivate_without_local_license_is_a_noop(deactivation, capsys):
    state, path = deactivation
    assert cli.main(["license", "deactivate"]) == 0
    captured = capsys.readouterr()
    assert captured.out.strip() == (
        "agentcap: no active license on this machine; nothing to deactivate."
    )
    assert state["calls"] == []
    assert not path.exists()


def test_license_deactivate_legacy_cache_removes_without_server_call(
    deactivation, capsys
):
    state, path = deactivation
    _write_cache_with_activation(path, activation_id=None)

    assert cli.main(["license", "deactivate"]) == 0
    captured = capsys.readouterr()
    assert not path.exists()
    assert state["calls"] == []
    assert captured.out.strip() == (
        "agentcap: local license removed. This activation was created by an "
        "older version and could not be released on the server; manage "
        "activations in the Polar customer portal."
    )
    assert "polar-key" not in captured.out + captured.err


def test_license_deactivate_released_on_server_removes_local_file(
    deactivation, capsys
):
    state, path = deactivation
    _write_cache_with_activation(path, activation_id="act_cached")

    assert cli.main(["license", "deactivate"]) == 0
    captured = capsys.readouterr()
    assert not path.exists()
    assert state["calls"] == [("polar-key", "act_cached")]
    assert captured.out.strip() == (
        "agentcap: activation released on the license server and local license "
        "removed. You can activate on another machine now."
    )
    assert "polar-key" not in captured.out + captured.err


def test_license_deactivate_unknown_activation_still_removes_local_file(
    deactivation, capsys
):
    state, path = deactivation
    state["status"] = 404
    _write_cache_with_activation(path, activation_id="act_gone")

    assert cli.main(["license", "deactivate"]) == 0
    captured = capsys.readouterr()
    assert not path.exists()
    assert state["calls"] == [("polar-key", "act_gone")]
    assert captured.out.strip() == (
        "agentcap: the license server no longer knows this activation "
        "(already released?). Local license removed."
    )
    assert "polar-key" not in captured.out + captured.err


@pytest.mark.parametrize(
    ("status", "reason"),
    [(422, "validation error"), (429, "HTTP 429"), (400, "HTTP 400")],
)
def test_license_deactivate_rejected_keeps_local_license(
    deactivation, capsys, status, reason
):
    state, path = deactivation
    state["status"] = status
    _write_cache_with_activation(path, activation_id="act_cached")

    assert cli.main(["license", "deactivate"]) == 1
    captured = capsys.readouterr()
    assert path.exists()
    assert state["calls"] == [("polar-key", "act_cached")]
    assert captured.err.startswith("agentcap: could not reach the license server")
    assert f"({reason})" in captured.err
    assert "Local license kept" in captured.err
    assert "`agentcap license deactivate`" in captured.err
    assert "`--local-only`" in captured.err
    assert "polar-key" not in captured.out + captured.err


@pytest.mark.parametrize(
    "error",
    [
        LicenseNetworkError("license server unreachable (ConnectError)"),
        LicenseNetworkError("license server error (HTTP 503)"),
        LicenseNetworkError("license server request timed out"),
    ],
)
def test_license_deactivate_network_failure_keeps_local_license(
    deactivation, capsys, error
):
    state, path = deactivation
    state["error"] = error
    _write_cache_with_activation(path, activation_id="act_cached")

    assert cli.main(["license", "deactivate"]) == 1
    captured = capsys.readouterr()
    assert path.exists()
    assert f"({error})" in captured.err
    assert "could not reach the license server to release this activation" in (
        captured.err
    )
    assert "polar-key" not in captured.out + captured.err


def test_license_deactivate_unexpected_validator_error_is_reported(
    deactivation, capsys
):
    state, path = deactivation
    state["error"] = RuntimeError("boom")
    _write_cache_with_activation(path, activation_id="act_cached")

    assert cli.main(["license", "deactivate"]) == 1
    captured = capsys.readouterr()
    assert path.exists()
    assert "(RuntimeError)" in captured.err
    assert "Traceback" not in captured.err


def test_license_deactivate_local_only_skips_server(deactivation, capsys):
    state, path = deactivation
    _write_cache_with_activation(path, activation_id="act_cached")

    assert cli.main(["license", "deactivate", "--local-only"]) == 0
    captured = capsys.readouterr()
    assert not path.exists()
    assert state["calls"] == []
    assert "local license removed" in captured.out
    assert "remains on the license server" in captured.out
    assert "polar-key" not in captured.out + captured.err


def test_license_deactivate_local_only_removes_legacy_cache_without_warning(
    deactivation, capsys
):
    state, path = deactivation
    _write_cache_with_activation(path, activation_id=None)

    assert cli.main(["license", "deactivate", "--local-only"]) == 0
    captured = capsys.readouterr()
    assert not path.exists()
    assert state["calls"] == []
    assert "remains on the license server" in captured.out


def test_license_deactivate_local_only_without_a_file_is_a_noop(deactivation, capsys):
    state, path = deactivation

    assert cli.main(["license", "deactivate", "--local-only"]) == 0
    captured = capsys.readouterr()
    assert captured.out.strip() == (
        "agentcap: no active license on this machine; nothing to deactivate."
    )
    assert state["calls"] == []


def test_license_deactivate_local_only_removes_an_unreadable_cache(
    deactivation, capsys
):
    state, path = deactivation
    path.write_text("{not json", encoding="utf-8")

    assert cli.main(["license", "deactivate", "--local-only"]) == 0
    captured = capsys.readouterr()
    assert not path.exists()
    assert state["calls"] == []
    assert "remains on the license server" in captured.out


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


class _KeyRecorder:
    persist = True
    seen: list = []

    def activate(self, key):
        _KeyRecorder.seen.append(key)
        return LicenseStatus(True, "pro", "license valid"), "act_1"

    def validate(self, key, activation_id=None):
        return LicenseStatus(True, "pro", "license valid")


@pytest.fixture
def recorder(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENTCAP_LICENSE_FILE", str(tmp_path / "license.json"))
    monkeypatch.delenv("AGENTCAP_LICENSE_KEY", raising=False)
    _KeyRecorder.seen = []
    monkeypatch.setattr(license_module, "PolarLicenseValidator", _KeyRecorder)
    return _KeyRecorder


@pytest.mark.parametrize("argv", [["license", "activate", "-"], ["license", "activate"]])
def test_license_activate_reads_key_from_piped_stdin(recorder, monkeypatch, capsys, argv):
    import io
    monkeypatch.setattr("sys.stdin", io.StringIO("  KEY-FROM-STDIN \n"))
    assert cli.main(argv) == 0
    assert recorder.seen == ["KEY-FROM-STDIN"]
    out, err = capsys.readouterr()
    assert "KEY-FROM-STDIN" not in out + err and "tier: pro" in out


def test_license_activate_reads_key_from_env(recorder, monkeypatch, capsys):
    import io
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    monkeypatch.setenv("AGENTCAP_LICENSE_KEY", "KEY-FROM-ENV")
    assert cli.main(["license", "activate"]) == 0
    assert recorder.seen == ["KEY-FROM-ENV"]
    assert "KEY-FROM-ENV" not in "".join(capsys.readouterr())


def test_license_activate_prompts_hidden_on_tty(recorder, monkeypatch, capsys):
    import io
    tty = io.StringIO("")
    tty.isatty = lambda: True
    monkeypatch.setattr("sys.stdin", tty)
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt="": "KEY-TYPED")
    assert cli.main(["license", "activate"]) == 0
    assert recorder.seen == ["KEY-TYPED"]


def test_license_activate_positional_still_works(recorder, capsys):
    assert cli.main(["license", "activate", "KEY-ARG"]) == 0
    assert recorder.seen == ["KEY-ARG"]
