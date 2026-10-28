"""Fake licence validators for tests.

These stand in for :class:`agentcap.license.PolarLicenseValidator` so tests can
exercise licence handling without any network access.  They are injected
through ``LicenseManager(validator)`` or by monkeypatching the module attribute.
"""

from __future__ import annotations

from agentcap.license import LicenseStatus


class StaticValidator:
    """A validator with a fixed answer; results are never persisted."""

    persist = False

    def __init__(self, status: LicenseStatus) -> None:
        self.status = status
        self.calls: list[tuple[str, str | None]] = []

    def validate(self, key: str, activation_id: str | None = None) -> LicenseStatus:
        self.calls.append((key, activation_id))
        return self.status

    def activate(self, key: str) -> tuple[LicenseStatus, str | None]:
        self.calls.append((key, None))
        return self.status, None
