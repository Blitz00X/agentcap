"""Licence validation, Polar customer-portal calls, and cache semantics.

All HTTP is mocked with ``httpx.MockTransport``.  An autouse guard makes any
attempt to build a real ``httpx.Client`` fail the test.
"""

from __future__ import annotations

import json
import stat
import time

import httpx
import pytest

from agentcap import __version__
from agentcap.license import (
    ACTIVATE_PATH,
    DEACTIVATE_PATH,
    NOT_CONFIGURED_REASON,
    NOT_FOUND_REASON,
    POLAR_API_BASE,
    POLAR_ORG_ID,
    REVALIDATE_AFTER_S,
    VALIDATE_PATH,
    LicenseManager,
    LicenseNetworkError,
    LicenseStatus,
    PolarLicenseValidator,
    features,
    machine_fingerprint,
    polar_api_base,
    polar_organization_id,
    status_from_environment,
)
from license_support import StaticValidator

BASE = "https://polar.test"
ORG = "org_test"
KEY = "polar-key-secret"
PAST = "1970-01-01T00:00:00Z"
FUTURE = "2999-01-01T00:00:00Z"


class FakeClock:
    def __init__(self, now: float = 10_000.0) -> None:
        self.now = float(now)

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += float(seconds)


@pytest.fixture(autouse=True)
def _offline_guard(monkeypatch):
    """Keep the suite offline and deterministic across environments."""

    for name in (
        "AGENTCAP_POLAR_ORG_ID",
        "AGENTCAP_POLAR_API_BASE",
        "AGENTCAP_LICENSE_FILE",
    ):
        monkeypatch.delenv(name, raising=False)

    original = httpx.Client

    def guarded_client(*args, **kwargs):
        if kwargs.get("transport") is None:
            raise AssertionError("tests must not create a real httpx.Client")
        return original(*args, **kwargs)

    monkeypatch.setattr(httpx, "Client", guarded_client)


class PolarStub:
    """Routes customer-portal calls to queued mocked responses."""

    def __init__(self, *, validate=None, activate=None, deactivate=None) -> None:
        self.requests: list[dict[str, object]] = []
        self._validate = validate
        self._activate = activate
        self._deactivate = deactivate
        self.transport = httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content.decode("utf-8") or "{}")
        self.requests.append(
            {
                "path": request.url.path,
                "url": str(request.url),
                "json": payload,
            }
        )
        name = request.url.path.rsplit("/", 1)[-1]
        responder = {
            "validate": self._validate,
            "activate": self._activate,
            "deactivate": self._deactivate,
        }.get(name)
        if responder is None:
            raise AssertionError(f"unexpected request to {request.url.path}")
        if callable(responder):
            return responder(payload)
        return responder

    @property
    def paths(self) -> list[str]:
        return [str(item["path"]) for item in self.requests]

    @property
    def payloads(self) -> list[dict[str, object]]:
        return [item["json"] for item in self.requests]  # type: ignore[return-value]


def sequence(*responses):
    items = list(responses)

    def respond(payload=None):
        if not items:
            raise AssertionError("no more mocked responses queued")
        item = items.pop(0)
        if callable(item):
            return item(payload)
        return item

    return respond


def granted(expires_at=None) -> httpx.Response:
    return httpx.Response(200, json={"status": "granted", "expires_at": expires_at})


def activated(activation_id: str = "act_123", *, expires_at=None) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "id": activation_id,
            "license_key": {"status": "granted", "expires_at": expires_at},
        },
    )


def raise_connect(payload=None):
    raise httpx.ConnectError(
        "connection refused", request=httpx.Request("POST", BASE)
    )


def make_validator(stub: PolarStub, clock: FakeClock, org: str = ORG) -> PolarLicenseValidator:
    return PolarLicenseValidator(
        org,
        base_url=BASE,
        client=httpx.Client(transport=stub.transport),
        clock=clock,
    )


def make_manager(
    stub: PolarStub, tmp_path, clock: FakeClock, org: str = ORG
) -> LicenseManager:
    return LicenseManager(
        make_validator(stub, clock, org),
        tmp_path / "license.json",
        clock=clock,
    )


def write_cached(tmp_path, key: str, clock: FakeClock):
    """Write a valid Pro cache entry for ``key`` and return its path."""

    path = tmp_path / "license.json"
    path.write_text(
        json.dumps(
            {
                "key": key,
                "tier": "pro",
                "valid": True,
                "reason": "license valid",
                "checked_at": clock.now,
                "source": "polar",
                "activation_id": "act_cached",
            }
        ),
        encoding="utf-8",
    )
    return path


def test_feature_gating():
    assert features("free") == {"run_cap", "terminal_alerts"}
    assert features("pro") == {
        "run_cap",
        "terminal_alerts",
        "hour_cap",
        "day_cap",
        "loop_detection",
        "webhook_alerts",
        "telegram_alerts",
        "spend_receipt",  # 0.1.5 Pro unattended pack
        "quota_mode",
        "usage_analytics",
    }
    assert features("free") < features("pro")
    assert features("unknown-tier") == features("free")


# ------------------------------------------------------- injected validators
def test_non_persisting_validator_activation_is_never_persisted(tmp_path):
    clock = FakeClock()
    path = tmp_path / "license.json"
    validator = StaticValidator(LicenseStatus(True, "pro", "static license accepted"))
    manager = LicenseManager(validator, path, clock=clock)

    status = manager.activate("secret")
    assert status.valid is True
    assert status.tier == "pro"
    assert not path.exists()
    assert manager.tier() == "free"


# ---------------------------------------------- no local license back doors
def test_dev_license_env_var_does_not_grant_pro(monkeypatch, tmp_path):
    """A licence-shaped env var must never unlock Pro by itself."""

    calls: list[tuple[str, str | None]] = []

    class DenyingPolar:
        persist = True

        def validate(self, key, activation_id=None):
            calls.append((key, activation_id))
            return LicenseStatus(False, "free", "license key not found")

        def activate(self, key):
            calls.append((key, None))
            return LicenseStatus(False, "free", "license key not found"), None

    monkeypatch.setenv("AGENTCAP_DEV_LICENSE_KEY", "dev-secret")
    monkeypatch.setenv("AGENTCAP_LICENSE_FILE", str(tmp_path / "license.json"))
    monkeypatch.setattr("agentcap.license.PolarLicenseValidator", DenyingPolar)

    # No key anywhere: the dev variable is not treated as one.
    status = status_from_environment()
    assert status == LicenseStatus(False, "free", "no license key configured")
    assert calls == []

    # With a stale cached key the normal Polar path runs and denies it.
    write_cached(tmp_path, KEY, FakeClock(now=time.time() - 30 * 24 * 60 * 60))
    status = status_from_environment()
    assert status.valid is False
    assert status.tier == "free"
    assert status.reason == "license key not found"
    assert calls == [(KEY, "act_cached")]


# -------------------------------------------------------------- config values
def test_config_defaults_and_env_overrides(monkeypatch):
    assert polar_api_base() == POLAR_API_BASE
    assert polar_organization_id() == POLAR_ORG_ID

    monkeypatch.setenv("AGENTCAP_POLAR_API_BASE", "https://sandbox-api.polar.sh/")
    monkeypatch.setenv("AGENTCAP_POLAR_ORG_ID", "org_from_env")
    assert polar_api_base() == "https://sandbox-api.polar.sh/"
    assert polar_organization_id() == "org_from_env"


def test_placeholder_org_makes_no_request(monkeypatch):
    monkeypatch.setenv("AGENTCAP_POLAR_ORG_ID", "POLAR_ORG_ID_PLACEHOLDER")
    clock = FakeClock()
    stub = PolarStub()  # any request would raise
    validator = PolarLicenseValidator(
        base_url=BASE,
        client=httpx.Client(transport=stub.transport),
        clock=clock,
    )

    assert polar_organization_id() == "POLAR_ORG_ID_PLACEHOLDER"
    status = validator.validate(KEY)
    assert status == LicenseStatus(False, "free", NOT_CONFIGURED_REASON)
    assert stub.requests == []


def test_blank_org_makes_no_request(monkeypatch):
    monkeypatch.setenv("AGENTCAP_POLAR_ORG_ID", "   ")
    monkeypatch.setattr("agentcap.license.POLAR_ORG_ID", "")
    clock = FakeClock()
    stub = PolarStub()  # any request would raise
    validator = PolarLicenseValidator(
        base_url=BASE,
        client=httpx.Client(transport=stub.transport),
        clock=clock,
    )

    assert polar_organization_id() == ""
    status, activation_id = validator.activate(KEY)
    assert status == LicenseStatus(False, "free", NOT_CONFIGURED_REASON)
    assert activation_id is None
    assert stub.requests == []


def test_default_org_is_sent_to_polar():
    clock = FakeClock()
    stub = PolarStub(validate=granted())
    validator = PolarLicenseValidator(
        base_url=BASE,
        client=httpx.Client(transport=stub.transport),
        clock=clock,
    )

    assert validator.organization_id == "2cf7b646-95db-4581-bb30-bc9cd0dc7a95"
    assert validator.validate(KEY).valid is True
    assert stub.paths == [VALIDATE_PATH]
    assert stub.payloads[0]["organization_id"] == (
        "2cf7b646-95db-4581-bb30-bc9cd0dc7a95"
    )
    assert stub.payloads[0]["key"] == KEY


def test_validator_uses_env_org_and_base(monkeypatch):
    monkeypatch.setenv("AGENTCAP_POLAR_ORG_ID", "org_env")
    monkeypatch.setenv("AGENTCAP_POLAR_API_BASE", "https://sandbox.example/")
    clock = FakeClock()
    stub = PolarStub(validate=granted())
    validator = PolarLicenseValidator(
        client=httpx.Client(transport=stub.transport), clock=clock
    )

    assert validator.validate(KEY).valid is True
    assert stub.payloads[0]["organization_id"] == "org_env"
    assert stub.payloads[0]["key"] == KEY
    assert str(stub.requests[0]["url"]).startswith("https://sandbox.example" + VALIDATE_PATH)


# ----------------------------------------------------------------- validate
def test_validate_granted_is_pro():
    clock = FakeClock()
    stub = PolarStub(validate=granted())
    validator = make_validator(stub, clock)

    assert validator.validate(KEY) == LicenseStatus(True, "pro", "license valid")
    assert stub.paths == [VALIDATE_PATH]
    assert stub.payloads[0] == {"key": KEY, "organization_id": ORG}


def test_validate_granted_future_expiry_is_pro():
    clock = FakeClock()
    stub = PolarStub(validate=granted(FUTURE))
    assert make_validator(stub, clock).validate(KEY).tier == "pro"


def test_validate_expired_is_free():
    clock = FakeClock()
    stub = PolarStub(validate=granted(PAST))
    status = make_validator(stub, clock).validate(KEY)
    assert status.valid is False
    assert status.tier == "free"
    assert "expired" in status.reason


def test_validate_non_granted_status_is_free():
    clock = FakeClock()
    stub = PolarStub(validate=httpx.Response(200, json={"status": "revoked"}))
    status = make_validator(stub, clock).validate(KEY)
    assert status.valid is False
    assert "revoked" in status.reason


def test_validate_404_is_definitive_free():
    clock = FakeClock()
    stub = PolarStub(validate=httpx.Response(404, json={"detail": "Not found"}))
    assert make_validator(stub, clock).validate(KEY) == LicenseStatus(
        False, "free", NOT_FOUND_REASON
    )


def test_validate_400_includes_detail_and_redacts_key():
    clock = FakeClock()
    detail = f"invalid key {KEY}"
    stub = PolarStub(validate=httpx.Response(400, json={"detail": detail}))
    status = make_validator(stub, clock).validate(KEY)
    assert status.valid is False
    assert status.tier == "free"
    assert "invalid key" in status.reason
    assert "HTTP 400" in status.reason
    assert KEY not in status.reason


@pytest.mark.parametrize("status_code", [403, 422])
def test_validate_other_client_errors_are_free(status_code):
    clock = FakeClock()
    stub = PolarStub(
        validate=httpx.Response(status_code, json={"detail": "rejected"})
    )
    status = make_validator(stub, clock).validate(KEY)
    assert status.valid is False
    assert "rejected" in status.reason


def test_validate_5xx_raises_network_error():
    clock = FakeClock()
    stub = PolarStub(validate=httpx.Response(503, text="down"))
    with pytest.raises(LicenseNetworkError) as excinfo:
        make_validator(stub, clock).validate(KEY)
    assert KEY not in str(excinfo.value)


def test_validate_connect_error_raises_network_error():
    clock = FakeClock()
    stub = PolarStub(validate=raise_connect)
    with pytest.raises(LicenseNetworkError):
        make_validator(stub, clock).validate(KEY)


def test_validate_non_json_raises_network_error():
    clock = FakeClock()
    stub = PolarStub(validate=httpx.Response(200, text="<html>nope</html>"))
    with pytest.raises(LicenseNetworkError):
        make_validator(stub, clock).validate(KEY)


def test_validate_sends_activation_id_when_given():
    clock = FakeClock()
    stub = PolarStub(validate=granted())
    make_validator(stub, clock).validate(KEY, activation_id="act_42")
    assert stub.payloads[0]["activation_id"] == "act_42"


# ---------------------------------------------------------------- activation
def test_activate_stores_activation_id_and_later_validate_sends_it(tmp_path):
    clock = FakeClock()
    stub = PolarStub(activate=activated(), validate=granted())
    manager = make_manager(stub, tmp_path, clock)
    path = tmp_path / "license.json"

    status = manager.activate(KEY)
    assert status.valid is True
    assert status.tier == "pro"
    assert path.exists()
    payload = json.loads(path.read_text())
    assert payload["source"] == "polar"
    assert payload["activation_id"] == "act_123"
    assert payload["tier"] == "pro"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600

    # Fresh cache: no HTTP at all.
    assert manager.check().valid is True
    assert len(stub.requests) == 1

    clock.advance(REVALIDATE_AFTER_S + 1)
    assert manager.check().valid is True
    assert stub.paths[-1] == VALIDATE_PATH
    assert stub.payloads[-1]["activation_id"] == "act_123"
    assert json.loads(path.read_text())["checked_at"] == clock.now


def test_activate_payload_contains_label_and_meta():
    clock = FakeClock()
    stub = PolarStub(activate=activated())
    status, activation_id = make_validator(stub, clock).activate(KEY)

    assert status.valid is True
    assert activation_id == "act_123"
    payload = stub.payloads[0]
    fingerprint = machine_fingerprint()
    meta = payload["meta"]
    assert payload["key"] == KEY
    assert payload["organization_id"] == ORG
    assert payload["label"] == "agentcap-" + fingerprint[:12]
    assert meta["fingerprint"] == fingerprint  # type: ignore[index]
    assert meta["agentcap_version"] == __version__  # type: ignore[index]
    assert isinstance(meta["os"], str)  # type: ignore[index]


def test_activate_does_not_support_activations_falls_back_to_validate():
    clock = FakeClock()
    stub = PolarStub(
        activate=httpx.Response(
            403, json={"detail": "License key does not support activations"}
        ),
        validate=granted(),
    )

    status, activation_id = make_validator(stub, clock).activate(KEY)
    assert status.valid is True
    assert activation_id is None
    assert stub.paths == [ACTIVATE_PATH, VALIDATE_PATH]


def test_activate_limit_reached_is_free():
    clock = FakeClock()
    stub = PolarStub(
        activate=httpx.Response(403, json={"detail": "Activation limit reached"})
    )
    status, activation_id = make_validator(stub, clock).activate(KEY)
    assert status.valid is False
    assert status.tier == "free"
    assert activation_id is None
    assert "Activation limit reached" in status.reason


def test_activate_404_is_free():
    clock = FakeClock()
    stub = PolarStub(activate=httpx.Response(404, json={"detail": "Not found"}))
    status, activation_id = make_validator(stub, clock).activate(KEY)
    assert status == LicenseStatus(False, "free", NOT_FOUND_REASON)
    assert activation_id is None


def test_activate_connect_error_returns_free_and_keeps_cache(tmp_path):
    """Regression: an unreachable licence server must not raise or wipe cache."""

    clock = FakeClock()
    stub = PolarStub(activate=raise_connect)
    manager = make_manager(stub, tmp_path, clock)
    path = write_cached(tmp_path, KEY, clock)
    before = path.read_bytes()

    status = manager.activate(KEY)

    assert status == LicenseStatus(
        False,
        "free",
        "could not reach the license server (LicenseNetworkError); "
        "check your connection and try again",
    )
    assert KEY not in status.reason
    assert path.read_bytes() == before
    # The cached licence keeps working inside its offline grace window.
    assert manager.tier() == "pro"


def test_activate_5xx_returns_free_without_touching_cache(tmp_path):
    clock = FakeClock()
    stub = PolarStub(activate=httpx.Response(503, json={"detail": "unavailable"}))
    manager = make_manager(stub, tmp_path, clock)

    status = manager.activate(KEY)

    assert status.valid is False
    assert status.tier == "free"
    assert "could not reach the license server (LicenseNetworkError)" in status.reason
    assert KEY not in status.reason
    assert not (tmp_path / "license.json").exists()


def test_activate_unexpected_validator_error_is_free_not_raised(tmp_path):
    class ExplodingValidator:
        persist = True

        def validate(self, key, activation_id=None):
            raise RuntimeError("boom")

    manager = LicenseManager(
        ExplodingValidator(), tmp_path / "license.json", clock=FakeClock()
    )
    status = manager.activate(KEY)

    assert status.tier == "free"
    assert "(RuntimeError)" in status.reason
    assert KEY not in status.reason
    assert not (tmp_path / "license.json").exists()


def test_activate_rejection_clears_same_key_cache(tmp_path):
    clock = FakeClock()
    stub = PolarStub(
        activate=httpx.Response(403, json={"detail": "Activation limit reached"})
    )
    manager = make_manager(stub, tmp_path, clock)
    path = write_cached(tmp_path, KEY, clock)

    status = manager.activate(KEY)

    assert status.valid is False
    assert status.tier == "free"
    assert not path.exists()
    assert manager.tier() == "free"


def test_activate_rejection_keeps_cache_for_a_different_key(tmp_path):
    clock = FakeClock()
    stub = PolarStub(
        activate=httpx.Response(403, json={"detail": "Activation limit reached"})
    )
    manager = make_manager(stub, tmp_path, clock)
    path = write_cached(tmp_path, "other-key", clock)

    status = manager.activate(KEY)

    assert status.valid is False
    assert path.exists()
    assert manager.check().valid is True


# -------------------------------------------------------------- deactivation
def test_deactivate_releases_activation_with_key_and_org():
    clock = FakeClock()
    stub = PolarStub(deactivate=httpx.Response(204))
    validator = make_validator(stub, clock)

    assert validator.deactivate(KEY, "act_123") == 204
    assert stub.paths == [DEACTIVATE_PATH]
    assert DEACTIVATE_PATH == "/v1/customer-portal/license-keys/deactivate"
    assert stub.payloads == [
        {"key": KEY, "organization_id": ORG, "activation_id": "act_123"}
    ]


def test_deactivate_unknown_activation_returns_404():
    clock = FakeClock()
    stub = PolarStub(deactivate=httpx.Response(404, json={"detail": "Not found"}))
    assert make_validator(stub, clock).deactivate(KEY, "act_gone") == 404
    assert stub.paths == [DEACTIVATE_PATH]


def test_deactivate_validation_error_returns_422():
    clock = FakeClock()
    stub = PolarStub(
        deactivate=httpx.Response(422, json={"detail": "activation_id is required"})
    )
    assert make_validator(stub, clock).deactivate(KEY, "act_bad") == 422


def test_deactivate_network_error_raises_without_echoing_key():
    clock = FakeClock()
    stub = PolarStub(deactivate=raise_connect)
    with pytest.raises(LicenseNetworkError) as excinfo:
        make_validator(stub, clock).deactivate(KEY, "act_123")
    assert KEY not in str(excinfo.value)


def test_deactivate_5xx_raises_network_error():
    clock = FakeClock()
    stub = PolarStub(deactivate=httpx.Response(503, json={"detail": "unavailable"}))
    with pytest.raises(LicenseNetworkError):
        make_validator(stub, clock).deactivate(KEY, "act_123")
    assert stub.paths == [DEACTIVATE_PATH]


def test_deactivate_without_org_makes_no_request(monkeypatch):
    monkeypatch.setenv("AGENTCAP_POLAR_ORG_ID", "POLAR_ORG_ID_PLACEHOLDER")
    stub = PolarStub()  # any request would raise
    validator = PolarLicenseValidator(
        base_url=BASE,
        client=httpx.Client(transport=stub.transport),
        clock=FakeClock(),
    )
    with pytest.raises(LicenseNetworkError):
        validator.deactivate(KEY, "act_123")
    assert stub.requests == []


# --------------------------------------------------------------------- cache
def test_cache_is_reused_within_seven_days_without_http(tmp_path):
    clock = FakeClock()
    stub = PolarStub(activate=activated())
    manager = make_manager(stub, tmp_path, clock)
    manager.activate(KEY)

    clock.advance(REVALIDATE_AFTER_S - 1)
    assert manager.check().tier == "pro"
    assert manager.tier() == "pro"
    assert len(stub.requests) == 1


def test_cache_revalidates_after_seven_days(tmp_path):
    clock = FakeClock()
    stub = PolarStub(activate=activated(), validate=granted())
    manager = make_manager(stub, tmp_path, clock)
    manager.activate(KEY)

    clock.advance(REVALIDATE_AFTER_S + 1)
    status = manager.check()
    assert status.valid is True
    assert stub.paths[-1] == VALIDATE_PATH
    assert len(stub.requests) == 2
    assert stat.S_IMODE((tmp_path / "license.json").stat().st_mode) == 0o600


def test_network_failure_after_seven_days_keeps_pro(tmp_path):
    clock = FakeClock()
    stub = PolarStub(activate=activated(), validate=raise_connect)
    manager = make_manager(stub, tmp_path, clock)
    manager.activate(KEY)

    clock.advance(8 * 24 * 60 * 60)
    status = manager.check()
    assert status.valid is True
    assert status.tier == "pro"
    assert status.reason.startswith("offline:")
    assert manager.tier() == "pro"


def test_network_failure_never_downgrades_after_a_year_offline(tmp_path):
    clock = FakeClock()
    stub = PolarStub(activate=activated(), validate=raise_connect)
    manager = make_manager(stub, tmp_path, clock)
    manager.activate(KEY)

    for _ in range(12):
        clock.advance(30 * 24 * 60 * 60)
        status = manager.check()
        assert status.valid is True
        assert status.tier == "pro"
    assert manager.tier() == "pro"


def test_failed_revalidation_is_retried_after_seven_days(tmp_path):
    clock = FakeClock()
    stub = PolarStub(activate=activated(), validate=raise_connect)
    manager = make_manager(stub, tmp_path, clock)
    manager.activate(KEY)
    assert len(stub.requests) == 1

    clock.advance(REVALIDATE_AFTER_S + 1)
    assert manager.check().tier == "pro"
    assert len(stub.requests) == 2

    # A failed attempt is not retried on every command...
    clock.advance(24 * 60 * 60)
    assert manager.check().tier == "pro"
    assert len(stub.requests) == 2

    # ...but again once the retry window has elapsed.
    clock.advance(REVALIDATE_AFTER_S)
    assert manager.check().tier == "pro"
    assert len(stub.requests) == 3


def test_force_refresh_offline_keeps_pro(tmp_path):
    clock = FakeClock()
    stub = PolarStub(activate=activated(), validate=raise_connect)
    manager = make_manager(stub, tmp_path, clock)
    manager.activate(KEY)

    status = manager.check(force_refresh=True)
    assert status.valid is True
    assert status.tier == "pro"
    assert status.reason.startswith("offline:")


def test_network_failure_without_cached_license_stays_free(tmp_path):
    clock = FakeClock()
    stub = PolarStub(validate=raise_connect)
    manager = make_manager(stub, tmp_path, clock)

    status = manager.check(KEY)
    assert status.valid is False
    assert status.tier == "free"
    assert "no validated license is cached" in status.reason


def test_tier_never_downgrades_cached_pro_without_network(tmp_path):
    clock = FakeClock()
    stub = PolarStub(activate=activated())
    manager = make_manager(stub, tmp_path, clock)
    manager.activate(KEY)

    clock.advance(10 * 24 * 60 * 60)
    assert manager.tier() == "pro"

    clock.advance(400 * 24 * 60 * 60)
    assert manager.tier() == "pro"
    assert len(stub.requests) == 1


def test_definitive_404_on_revalidation_clears_pro(tmp_path):
    clock = FakeClock()
    stub = PolarStub(
        activate=activated(),
        validate=httpx.Response(404, json={"detail": "Not found"}),
    )
    manager = make_manager(stub, tmp_path, clock)
    manager.activate(KEY)
    path = tmp_path / "license.json"
    assert path.exists()

    clock.advance(8 * 24 * 60 * 60)
    status = manager.check()
    assert status.valid is False
    assert status.tier == "free"
    assert not path.exists()
    assert manager.cached_status() is None
    assert manager.tier() == "free"


def test_negative_age_is_treated_as_stale(tmp_path):
    clock = FakeClock()
    stub = PolarStub(activate=activated(), validate=granted())
    manager = make_manager(stub, tmp_path, clock)
    manager.activate(KEY)

    clock.now -= 60
    assert manager.check().valid is True
    assert len(stub.requests) == 2
    assert stub.paths[-1] == VALIDATE_PATH


def test_negative_age_does_not_downgrade_tier(tmp_path):
    clock = FakeClock()
    stub = PolarStub(activate=activated())
    manager = make_manager(stub, tmp_path, clock)
    manager.activate(KEY)

    clock.now -= 60
    assert manager.tier() == "pro"


def test_old_cache_without_source_is_ignored(tmp_path):
    path = tmp_path / "license.json"
    path.write_text(
        json.dumps(
            {
                "key": KEY,
                "tier": "pro",
                "valid": True,
                "reason": "legacy cache entry without source",
                "checked_at": time.time(),
            }
        ),
        encoding="utf-8",
    )
    clock = FakeClock()
    stub = PolarStub()  # any request would raise
    manager = make_manager(stub, tmp_path, clock)

    assert manager.tier() == "free"
    assert manager.cached_status() is None
    status = manager.check()
    assert status.valid is False
    assert status.reason == "no license key configured"
    assert stub.requests == []


def test_unknown_cache_source_is_ignored(tmp_path):
    path = tmp_path / "license.json"
    payload = {
        "key": KEY,
        "tier": "pro",
        "valid": True,
        "reason": "dev",
        "checked_at": time.time(),
        "source": "dev",
    }
    path.write_text(json.dumps(payload), encoding="utf-8")

    manager = LicenseManager(
        StaticValidator(LicenseStatus(True, "pro", "static license accepted")),
        path,
        clock=FakeClock(),
    )
    assert manager.tier() == "free"


def test_invalid_key_is_not_cached(tmp_path):
    path = tmp_path / "license.json"
    clock = FakeClock()
    stub = PolarStub(activate=httpx.Response(404, json={}))
    manager = make_manager(stub, tmp_path, clock)
    status = manager.activate(KEY)
    assert status.valid is False
    assert not path.exists()
    assert manager.cached_status() is None
    assert manager.tier() == "free"


def test_clear_removes_license(tmp_path):
    path = tmp_path / "license.json"
    clock = FakeClock()
    stub = PolarStub(activate=activated())
    manager = make_manager(stub, tmp_path, clock)
    manager.activate(KEY)
    manager.clear()
    assert not path.exists()
    assert manager.tier() == "free"


def test_activate_propagates_oserror_when_activation_cannot_be_saved(
    tmp_path, monkeypatch
):
    class PersistingValidator:
        persist = True

        def activate(self, key):
            return LicenseStatus(True, "pro", "static license accepted"), None

        def validate(self, key, activation_id=None):
            return LicenseStatus(True, "pro", "static license accepted")

    manager = LicenseManager(
        PersistingValidator(),
        tmp_path / "license.json",
        clock=FakeClock(),
    )

    def fail_write(*args, **kwargs):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(manager, "_write", fail_write)
    with pytest.raises(OSError) as excinfo:
        manager.activate("secret-key")
    assert excinfo.value.strerror == "No space left on device"


def test_clear_propagates_oserror(tmp_path):
    # A directory cannot be unlinked with Path.unlink(), which raises OSError
    # (IsADirectoryError/PermissionError depending on platform).
    manager = LicenseManager(
        StaticValidator(LicenseStatus(True, "pro", "static license accepted")),
        tmp_path,
        clock=FakeClock(),
    )
    with pytest.raises(OSError):
        manager.clear()


def test_key_never_appears_in_reasons():
    clock = FakeClock()
    scenarios = [
        PolarStub(validate=httpx.Response(400, json={"detail": f"bad {KEY}"})),
        PolarStub(validate=httpx.Response(404, json={"detail": f"bad {KEY}"})),
        PolarStub(validate=granted(PAST)),
        PolarStub(
            validate=httpx.Response(200, json={"status": KEY, "expires_at": None})
        ),
        PolarStub(
            activate=httpx.Response(
                403, json={"detail": f"activation limit {KEY}"}
            )
        ),
    ]

    reasons: list[str] = []
    for stub in scenarios:
        validator = make_validator(stub, clock)
        if stub._activate is not None:
            status, _ = validator.activate(KEY)
        else:
            status = validator.validate(KEY)
        reasons.append(status.reason)

    assert reasons
    for reason in reasons:
        assert KEY not in reason
