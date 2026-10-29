"""License validation and feature gating.

Pro licences are validated against Polar's public customer-portal endpoints.
Those endpoints require no API token, which keeps them safe to call from a
desktop application.  Results are cached in a private JSON file and reused
without a network round-trip for seven days; after that the licence is
revalidated.

Network failures (timeouts, connection errors, 5xx responses and unexpected
bodies) never downgrade a licence that was previously validated as Pro: the
cached licence keeps working and revalidation is retried about every seven
days.  Only an explicit rejection from Polar (for example HTTP 404, a revoked
or disabled key, or an expired grant) falls back to the free tier.

Only status from the Polar validator is ever written to disk.
"""

from __future__ import annotations

import getpass
import hashlib
import json
import os
import platform
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Literal, Protocol, runtime_checkable

import httpx

from . import __version__

Tier = Literal["free", "pro"]

DAY = 24 * 60 * 60
REVALIDATE_AFTER_S = 7 * DAY
GRACE_SECONDS = REVALIDATE_AFTER_S

POLAR_API_BASE = "https://api.polar.sh"
POLAR_ORG_ID = "2cf7b646-95db-4581-bb30-bc9cd0dc7a95"

# Values that mean "no organization id configured".  The literal placeholder is
# kept so an old environment file that still pins the sentinel stays disabled
# instead of sending it to the API.
_UNCONFIGURED_ORG_IDS = frozenset({"", "POLAR_ORG_ID_PLACEHOLDER"})

VALIDATE_PATH = "/v1/customer-portal/license-keys/validate"
ACTIVATE_PATH = "/v1/customer-portal/license-keys/activate"

NOT_CONFIGURED_REASON = "license server not configured (AGENTCAP_POLAR_ORG_ID)"
NOT_FOUND_REASON = "license key not found, revoked, disabled, or expired"

CACHE_SOURCE = "polar"

FREE_FEATURES = frozenset({"run_cap", "terminal_alerts"})
PRO_FEATURES = FREE_FEATURES | frozenset(
    {
        "hour_cap",
        "day_cap",
        "loop_detection",
        "webhook_alerts",
        "telegram_alerts",
    }
)

FEATURES: dict[str, frozenset[str]] = {
    "free": FREE_FEATURES,
    "pro": PRO_FEATURES,
}


def features(tier: str) -> set[str]:
    """Feature set for a tier.  Unknown tiers fail closed to free."""

    if tier == "pro":
        return set(PRO_FEATURES)
    return set(FREE_FEATURES)


@dataclass(frozen=True)
class LicenseStatus:
    valid: bool
    tier: Tier
    reason: str

    def __post_init__(self) -> None:
        if self.tier not in ("free", "pro"):
            raise ValueError(f"invalid license tier: {self.tier!r}")
        if not self.valid and self.tier != "free":
            raise ValueError("an invalid license must fall back to the free tier")


class LicenseNetworkError(Exception):
    """The licence server could not give a definitive answer.

    Raised for timeouts, connection failures, 5xx responses and malformed
    (non-JSON) bodies.  A previously validated Pro licence is never downgraded
    for these: the cache keeps working and validation is retried later.
    """


@runtime_checkable
class LicenseValidator(Protocol):
    persist: bool

    def validate(self, key: str, activation_id: str | None = None) -> LicenseStatus: ...


# ------------------------------------------------------------------- config
def polar_api_base() -> str:
    """Polar API base URL, overridable for sandbox/testing."""

    value = os.environ.get("AGENTCAP_POLAR_API_BASE", "").strip()
    return value or POLAR_API_BASE


def polar_organization_id() -> str:
    """Configured Polar organization id (defaults to the AgentCap org)."""

    value = os.environ.get("AGENTCAP_POLAR_ORG_ID", "").strip()
    return value or POLAR_ORG_ID


# ---------------------------------------------------------------- fingerprint
def _safe_user() -> str:
    try:
        return getpass.getuser()
    except Exception:  # pragma: no cover - defensive, platform dependent
        return ""


def _safe_platform(part: Callable[[], str]) -> str:
    try:
        return part()
    except Exception:  # pragma: no cover - defensive, platform dependent
        return ""


def machine_fingerprint() -> str:
    """SHA-256 of local machine identity.

    The raw hostname and username never leave the machine: only this digest
    (and a short label derived from it) is sent to the licence server.
    """

    parts = (
        _safe_platform(platform.node),
        _safe_user(),
        _safe_platform(platform.system),
        _safe_platform(platform.machine),
    )
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


def activation_label() -> str:
    return "agentcap-" + machine_fingerprint()[:12]


def activation_meta() -> dict[str, str]:
    return {
        "fingerprint": machine_fingerprint(),
        "agentcap_version": __version__,
        "os": _safe_platform(platform.system),
    }


# ------------------------------------------------------------------- helpers
def _parse_expiry(value: object) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        text = value.strip()
        if text.endswith(("Z", "z")):
            text = text[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _collapse(text: object, limit: int = 200) -> str:
    if isinstance(text, (list, dict)):
        try:
            text = json.dumps(text, sort_keys=True)
        except (TypeError, ValueError):  # pragma: no cover - defensive
            text = str(text)
    collapsed = " ".join(str(text).split())
    return collapsed[:limit]


def _redact(text: str, key: str) -> str:
    if key and key in text:
        return text.replace(key, "<redacted>")
    return text


def _detail(data: dict[str, object] | None) -> str:
    if not isinstance(data, dict):
        return ""
    for field in ("detail", "error", "message"):
        if field in data and data[field] not in (None, ""):
            return _collapse(data[field])
    return ""


def _now_from_clock(clock: Callable[[], float]) -> datetime:
    return datetime.fromtimestamp(clock(), tz=timezone.utc)


# ------------------------------------------------------------------ validators
class PolarLicenseValidator:
    """Validate licence keys through Polar's public customer portal.

    No API token is used.  Every failure mode that is not a definitive server
    answer is reported as :class:`LicenseNetworkError` so the caller can keep a
    previously validated licence instead of downgrading it.
    """

    persist = True

    def __init__(
        self,
        organization_id: str | None = None,
        *,
        base_url: str | None = None,
        client: httpx.Client | None = None,
        timeout: float = 5.0,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.organization_id = organization_id if organization_id is not None else polar_organization_id()
        self.base_url = (base_url or polar_api_base()).rstrip("/")
        self._client = client
        self.timeout = float(timeout)
        self._clock = clock or time.time

    # ---------------------------------------------------------- configuration
    def _configured_organization_id(self) -> str | None:
        org = (self.organization_id or "").strip()
        if org in _UNCONFIGURED_ORG_IDS:
            return None
        return org

    # ---------------------------------------------------------------- transport
    def _post(self, path: str, payload: dict[str, object]):
        url = self.base_url + path
        client = self._client
        owned = client is None
        if owned:
            client = httpx.Client(timeout=self.timeout, trust_env=False)
        try:
            try:
                response = client.post(url, json=payload, timeout=self.timeout)
            except httpx.TimeoutException as exc:
                raise LicenseNetworkError("license server request timed out") from exc
            except httpx.TransportError as exc:
                raise LicenseNetworkError(
                    f"license server unreachable ({type(exc).__name__})"
                ) from exc
            except httpx.HTTPError as exc:  # pragma: no cover - defensive
                raise LicenseNetworkError(
                    f"license request failed ({type(exc).__name__})"
                ) from exc
        finally:
            if owned:
                client.close()

        if response.status_code >= 500:
            raise LicenseNetworkError(
                f"license server error (HTTP {response.status_code})"
            )
        return response

    @staticmethod
    def _json_body(response) -> dict[str, object]:
        try:
            data = response.json()
        except ValueError as exc:
            raise LicenseNetworkError(
                "license server returned a non-JSON response"
            ) from exc
        if not isinstance(data, dict):
            raise LicenseNetworkError("license server returned an unexpected response")
        return data

    # ------------------------------------------------------------------ status
    def _status_from_license(
        self, data: dict[str, object] | None, key: str
    ) -> LicenseStatus:
        if not isinstance(data, dict):
            raise LicenseNetworkError("license server returned an unexpected response")
        state = data.get("status")
        if state != "granted":
            reason = "license is not granted"
            if isinstance(state, str) and state.strip():
                reason = f"license status is {_collapse(state, 40)}"
            return LicenseStatus(False, "free", _redact(reason, key))
        expires_at = _parse_expiry(data.get("expires_at"))
        if data.get("expires_at") not in (None, "") and expires_at is None:
            raise LicenseNetworkError(
                "license server returned an unparseable expiration date"
            )
        if expires_at is not None and expires_at <= _now_from_clock(self._clock):
            return LicenseStatus(False, "free", "license expired")
        return LicenseStatus(True, "pro", "license valid")

    def _reject(
        self, status_code: int, data: dict[str, object] | None, key: str
    ) -> LicenseStatus:
        detail = _detail(data)
        reason = f"license rejected (HTTP {status_code})"
        if detail:
            reason = f"{reason}: {detail}"
        return LicenseStatus(False, "free", _redact(reason, key))

    # ------------------------------------------------------------------- calls
    def validate(
        self, key: str, activation_id: str | None = None
    ) -> LicenseStatus:
        org = self._configured_organization_id()
        if org is None:
            return LicenseStatus(False, "free", NOT_CONFIGURED_REASON)

        payload: dict[str, object] = {"key": key, "organization_id": org}
        if activation_id:
            payload["activation_id"] = activation_id

        response = self._post(VALIDATE_PATH, payload)
        code = response.status_code
        if code == 200:
            return self._status_from_license(self._json_body(response), key)
        if code == 404:
            return LicenseStatus(False, "free", NOT_FOUND_REASON)
        if code == 429:
            raise LicenseNetworkError("license server rate limited the request")
        if 400 <= code < 500:
            try:
                data = self._json_body(response)
            except LicenseNetworkError:
                data = None
            return self._reject(code, data, key)
        raise LicenseNetworkError(f"license server returned HTTP {code}")

    def activate(self, key: str) -> tuple[LicenseStatus, str | None]:
        org = self._configured_organization_id()
        if org is None:
            return LicenseStatus(False, "free", NOT_CONFIGURED_REASON), None

        payload: dict[str, object] = {
            "key": key,
            "organization_id": org,
            "label": activation_label(),
            "meta": activation_meta(),
        }

        response = self._post(ACTIVATE_PATH, payload)
        code = response.status_code
        if code == 200:
            data = self._json_body(response)
            nested = data.get("license_key")
            status = self._status_from_license(nested, key)
            activation_id = data.get("id")
            if status.valid and not (isinstance(activation_id, str) and activation_id):
                activation_id = None
            return status, (activation_id if status.valid else None)
        if code == 404:
            return LicenseStatus(False, "free", NOT_FOUND_REASON), None
        if code == 429:
            raise LicenseNetworkError("license server rate limited the request")
        if 400 <= code < 500:
            try:
                data = self._json_body(response)
            except LicenseNetworkError:
                data = None
            if code == 403 and "does not support activation" in _detail(data).lower():
                # Polar documents /validate as the fallback for keys without
                # activations enabled.
                return self.validate(key), None
            return self._reject(code, data, key), None
        raise LicenseNetworkError(f"license server returned HTTP {code}")


def status_from_environment() -> LicenseStatus:
    """Resolve the licence the way the CLI and runner do.

    Always uses the normal cached/Polar path: a cached licence inside its
    revalidation window is reused, otherwise Polar is asked.  Environment
    variables only select the configuration (organisation id, API base and
    cache path), never the validation result.
    """

    return LicenseManager().check()


def default_license_path() -> Path:
    env_path = os.environ.get("AGENTCAP_LICENSE_FILE")
    if env_path:
        return Path(env_path).expanduser()
    return Path.home() / ".config" / "agentcap" / "license.json"


@dataclass(frozen=True)
class CachedLicense:
    key: str
    status: LicenseStatus
    checked_at: float
    source: str = CACHE_SOURCE
    activation_id: str | None = None
    last_attempt_at: float | None = None

    @property
    def retry_at(self) -> float:
        """Timestamp of the most recent validation attempt (success or not)."""

        if self.last_attempt_at is not None:
            return max(self.checked_at, self.last_attempt_at)
        return self.checked_at


class LicenseManager:
    """Store a licence key and a timestamped status in a private JSON file."""

    def __init__(
        self,
        validator: LicenseValidator | None = None,
        path: str | os.PathLike[str] | None = None,
        *,
        clock: Callable[[], float] | None = None,
        revalidate_after_s: float = REVALIDATE_AFTER_S,
        offline_grace_s: float | None = None,
    ) -> None:
        self.validator = validator or PolarLicenseValidator()
        self.path = Path(path).expanduser() if path is not None else default_license_path()
        self._clock = clock or time.time
        self.revalidate_after_s = float(revalidate_after_s)
        # Deprecated: kept for API compatibility.  Network failures no longer
        # expire a validated licence, so there is no offline grace horizon.
        self.offline_grace_s = offline_grace_s

    # ------------------------------------------------------------ persistence
    @property
    def persists(self) -> bool:
        return bool(getattr(self.validator, "persist", False))

    def _read(self) -> CachedLicense | None:
        try:
            with self.path.open("r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            return None
        if not isinstance(payload, dict):
            return None
        # Cache files written without a source are ignored.
        if payload.get("source") != CACHE_SOURCE:
            return None
        key = payload.get("key")
        tier = payload.get("tier")
        valid = payload.get("valid")
        reason = payload.get("reason", "")
        checked_at = payload.get("checked_at")
        activation_id = payload.get("activation_id")
        last_attempt_at = payload.get("last_attempt_at")
        if (
            not isinstance(key, str)
            or not key
            or tier not in ("free", "pro")
            or not isinstance(valid, bool)
            or not valid
            or isinstance(checked_at, bool)
            or not isinstance(checked_at, (int, float))
        ):
            return None
        if activation_id is not None and not isinstance(activation_id, str):
            activation_id = None
        if last_attempt_at is not None and (
            isinstance(last_attempt_at, bool)
            or not isinstance(last_attempt_at, (int, float))
        ):
            last_attempt_at = None
        return CachedLicense(
            key=key,
            status=LicenseStatus(valid=True, tier=tier, reason=str(reason)),
            checked_at=float(checked_at),
            source=CACHE_SOURCE,
            activation_id=activation_id,
            last_attempt_at=(
                float(last_attempt_at) if last_attempt_at is not None else None
            ),
        )

    def _write(
        self,
        key: str,
        status: LicenseStatus,
        checked_at: float,
        activation_id: str | None = None,
        last_attempt_at: float | None = None,
    ) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "key": key,
            "tier": status.tier,
            "valid": status.valid,
            "reason": status.reason,
            "checked_at": checked_at,
            "source": CACHE_SOURCE,
            "activation_id": activation_id,
            "last_attempt_at": last_attempt_at,
        }
        temporary = self.path.with_name(self.path.name + ".tmp")
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, sort_keys=True)
                handle.write("\n")
        except BaseException:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise
        os.replace(temporary, self.path)
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    # --------------------------------------------------------------- checking
    def check(self, key: str | None = None, *, force_refresh: bool = False) -> LicenseStatus:
        """Validate (or reuse a cached) licence.

        A cached licence is reused for seven days, then revalidated.  If the
        licence server cannot be reached, a previously validated licence is
        kept: network failures never downgrade it.  Only a definitive rejection
        from Polar falls back to the free tier.
        """

        cached = self._read()
        effective_key = key if key is not None else (cached.key if cached else None)
        if not effective_key:
            return LicenseStatus(False, "free", "no license key configured")

        now = self._clock()
        same_key = cached if cached is not None and cached.key == effective_key else None

        if same_key is not None and not force_refresh:
            retry_age = now - same_key.retry_at
            if 0 <= retry_age <= self.revalidate_after_s:
                return same_key.status

        activation_id = same_key.activation_id if same_key is not None else None
        try:
            if activation_id:
                status = self.validator.validate(effective_key, activation_id=activation_id)
            else:
                status = self.validator.validate(effective_key)
        except LicenseNetworkError as exc:
            return self._offline(same_key, now, type(exc).__name__)
        except Exception as exc:  # defensive: never let a validator crash the proxy
            return self._offline(same_key, now, type(exc).__name__)

        if status.valid:
            if self.persists:
                self._write(
                    effective_key,
                    status,
                    now,
                    activation_id,
                    last_attempt_at=now,
                )
        elif same_key is not None and self.persists:
            # Definitive rejection: make sure a stale Pro cache cannot survive.
            self.clear()
        return status

    def _offline(
        self,
        cached: CachedLicense | None,
        now: float,
        error_name: str,
    ) -> LicenseStatus:
        """Fail open on network trouble: keep a validated licence.

        The failed attempt is recorded so the next revalidation happens in
        about seven days instead of on every command.
        """

        if cached is not None and cached.status.valid:
            if self.persists:
                try:
                    self._write(
                        cached.key,
                        cached.status,
                        cached.checked_at,
                        cached.activation_id,
                        last_attempt_at=now,
                    )
                except OSError:
                    pass
            return LicenseStatus(
                True,
                cached.status.tier,
                "offline: keeping previously validated license; "
                "will retry validation",
            )
        return LicenseStatus(
            False,
            "free",
            f"license validation unavailable ({error_name}) "
            "and no validated license is cached",
        )

    def activate(self, key: str) -> LicenseStatus:
        """Validate a key immediately and cache the result when allowed."""

        activator = getattr(self.validator, "activate", None)
        try:
            if callable(activator):
                status, activation_id = activator(key)
            else:
                # Validators that only expose validate() never provide an
                # activation id.
                status, activation_id = self.validator.validate(key), None
        except Exception as exc:  # never let a transport failure crash the CLI
            # The cache is deliberately left untouched so a previously
            # validated licence keeps working.
            return LicenseStatus(
                False,
                "free",
                "could not reach the license server "
                f"({type(exc).__name__}); check your connection and try again",
            )

        if status.valid:
            if self.persists:
                self._write(key, status, self._clock(), activation_id)
            return status

        cached = self._read()
        if self.persists and cached is not None and cached.key == key:
            # Definitive rejection for the cached key: make sure a stale Pro
            # cache cannot survive (same rule as check()).
            self.clear()
        return status

    def tier(self) -> Tier:
        """Current effective tier without contacting a validator.

        A cached Pro licence never expires because of elapsed time or a network
        failure; only an explicit rejection removes it from the cache.
        """

        cached = self._read()
        if cached is None or not cached.status.valid:
            return "free"
        return cached.status.tier

    def cached_status(self) -> LicenseStatus | None:
        cached = self._read()
        if cached is None:
            return None
        return cached.status

    def clear(self) -> None:
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass

    # Friendly aliases used by embedders.
    def get_status(self, key: str | None = None) -> LicenseStatus:
        return self.check(key)

    validate = check


# Alias spellings for the cache manager.
LicenseStore = LicenseManager
LicenseCache = LicenseManager
