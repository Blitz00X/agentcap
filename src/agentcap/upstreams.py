"""Environment-driven upstream overrides for local testing.

The proxy talks to the real provider APIs by default.  For offline / local
experiments the upstream base URLs can be overridden with:

* ``AGENTCAP_OPENAI_UPSTREAM``
* ``AGENTCAP_GEMINI_UPSTREAM``
* ``AGENTCAP_ANTHROPIC_UPSTREAM``

Safety rule: an override is only accepted when it is ``https://`` or when it is
``http://`` and the host is a loopback address (``127.0.0.1``, ``localhost`` or
``::1``).  Anything else is refused so a stray environment variable can never
silently redirect a user's prompts and credentials to a remote plaintext host.

The URLs themselves carry no secrets, but userinfo and query strings are
stripped before anything is printed.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Mapping
from urllib.parse import urlsplit, urlunsplit

OPENAI_UPSTREAM_ENV = "AGENTCAP_OPENAI_UPSTREAM"
GEMINI_UPSTREAM_ENV = "AGENTCAP_GEMINI_UPSTREAM"
ANTHROPIC_UPSTREAM_ENV = "AGENTCAP_ANTHROPIC_UPSTREAM"

_LOCAL_HTTP_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})

YELLOW = "\033[33m"
RESET = "\033[0m"


class UpstreamConfigError(ValueError):
    """An upstream override is malformed or unsafe."""


@dataclass(frozen=True)
class UpstreamOverride:
    """One validated upstream override."""

    env: str
    provider: str
    url: str

    def notice(self) -> str:
        """One-line yellow notice safe to print (no userinfo or query)."""

        base = display_url(self.url)
        return (
            f"{YELLOW}agentcap: {self.env} override active: {base}{RESET}"
        )


def display_url(value: str) -> str:
    """Strip userinfo, query and fragment from a URL for display."""

    try:
        parts = urlsplit(value)
        host = parts.hostname or ""
    except ValueError:
        return "<invalid URL>"
    if ":" in host:
        host = f"[{host}]"
    try:
        port = parts.port
    except ValueError:
        port = None
    netloc = f"{host}:{port}" if port is not None else host
    path = parts.path.rstrip("/")
    return urlunsplit((parts.scheme, netloc, path, "", ""))


def _validate(raw: str, env_name: str) -> str:
    value = raw.strip()
    if not value:
        raise UpstreamConfigError(
            f"{env_name} is empty; unset it or give a URL."
        )
    try:
        parts = urlsplit(value)
        host = parts.hostname or ""
    except ValueError:
        raise UpstreamConfigError(
            f"{env_name} is not a valid URL: {display_url(value)!r}"
        ) from None
    if parts.scheme not in ("http", "https") or not host:
        raise UpstreamConfigError(
            f"{env_name} must be an https:// URL "
            f"(http:// is allowed only on loopback); got {display_url(value)!r}"
        )
    if parts.scheme == "http" and host.lower() not in _LOCAL_HTTP_HOSTS:
        raise UpstreamConfigError(
            f"{env_name} may use http:// only for 127.0.0.1, localhost or "
            f"::1; got {display_url(value)!r}. Use https:// for a remote "
            "upstream."
        )
    return value.rstrip("/")


def resolve_upstreams(
    environ: Mapping[str, str] | None = None,
) -> tuple[UpstreamOverride, ...]:
    """Validate every active override, raising on the first unsafe one."""

    env = environ if environ is not None else os.environ
    overrides: list[UpstreamOverride] = []
    for env_name, provider in (
        (OPENAI_UPSTREAM_ENV, "openai"),
        (GEMINI_UPSTREAM_ENV, "gemini"),
        (ANTHROPIC_UPSTREAM_ENV, "anthropic"),
    ):
        raw = env.get(env_name)
        if raw is None or not raw.strip():
            continue
        overrides.append(
            UpstreamOverride(
                env=env_name,
                provider=provider,
                url=_validate(raw, env_name),
            )
        )
    return tuple(overrides)


__all__ = [
    "GEMINI_UPSTREAM_ENV",
    "ANTHROPIC_UPSTREAM_ENV",
    "OPENAI_UPSTREAM_ENV",
    "UpstreamConfigError",
    "UpstreamOverride",
    "display_url",
    "resolve_upstreams",
]
