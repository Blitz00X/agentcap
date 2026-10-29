"""Per-CLI routing adapters.

Some agent CLIs do not honour a plain base-URL environment variable:

* Codex CLI 0.163 ignores ``OPENAI_BASE_URL`` and defaults to a websocket
  transport.  It has to be pointed at the proxy with ``-c`` configuration
  overrides (``model_provider`` plus a custom provider that uses the Responses
  API over plain HTTP).
* Gemini CLI reads a settings file for its authentication type when it runs
  headless; a gateway base URL alone makes it pick an auth type that headless
  mode rejects.  A temporary settings file selects ``gemini-api-key`` and is
  removed after the run.

Everything here is pure (apart from creating the temporary Gemini settings
directory) so it can be unit-tested without spawning a process.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Mapping, Sequence

CODEX_PACKAGE = "@openai/codex"
GEMINI_PACKAGE = "@google/gemini-cli"

# Config keys the user may not set themselves: AgentCap owns the provider
# routing, and a conflicting override would silently bypass the proxy.
_CODEX_CONFLICT_KEYS = frozenset({"model_provider", "model_providers", "openai_base_url"})

GEMINI_SETTINGS: dict[str, object] = {
    "security": {"auth": {"selectedType": "gemini-api-key", "useExternal": False}}
}

CODEX_CONFLICT_MESSAGE = (
    "refusing to run Codex: the command already sets model_provider, "
    "model_providers.* or openai_base_url via -c/--config, which would bypass "
    "AgentCap. Remove that override and try again."
)


class AdapterConflict(ValueError):
    """The user's argv conflicts with the routing AgentCap must inject."""


@dataclass
class PreparedChild:
    """The final argv/env for the child plus an optional cleanup callback."""

    argv: list[str]
    env: dict[str, str]
    cleanup: Callable[[], None] | None = field(default=None, repr=False)


# ------------------------------------------------------------------ detection
def _executable_name(token: str) -> str:
    name = os.path.basename(str(token)).lower()
    for suffix in (".exe", ".cmd", ".bat"):
        if name.endswith(suffix):
            name = name[: -len(suffix)]
    return name


def _normalize_package(token: str) -> str:
    value = token.strip()
    # Scoped packages start with '@'; a second '@' introduces a version.
    if value.startswith("@") and value.count("@") > 1:
        value = value.rsplit("@", 1)[0]
    return value


def _npx_package(argv: Sequence[str]) -> tuple[str, int] | None:
    """Return ``(package, index)`` for ``npx [flags] <package> ...``."""

    if not argv or _executable_name(argv[0]) not in {"npx", "bunx", "pnpx"}:
        return None
    index = 1
    while index < len(argv):
        token = str(argv[index])
        if token == "--":
            index += 1
            continue
        if token in ("-p", "--package"):
            if index + 1 >= len(argv):
                return None
            return _normalize_package(str(argv[index + 1])), index + 1
        if token.startswith("-"):
            index += 1
            continue
        return _normalize_package(token), index
    return None


def detect_agent(argv: Sequence[str]) -> tuple[str, int] | None:
    """Return ``(kind, insertion_index)`` for a supported CLI, else ``None``.

    ``insertion_index`` is the position right after the executable (or after
    the package argument when launched through ``npx``).
    """

    if not argv:
        return None
    name = _executable_name(argv[0])
    if name == "codex":
        return "codex", 1
    if name == "gemini":
        return "gemini", 1
    package = _npx_package(argv)
    if package is not None:
        package_name, index = package
        if package_name == CODEX_PACKAGE:
            return "codex", index + 1
        if package_name == GEMINI_PACKAGE:
            return "gemini", index + 1
    return None


# --------------------------------------------------------------------- Codex
def codex_config_arguments(port: int) -> list[str]:
    """The two ``-c`` overrides that route Codex through the proxy."""

    base_url = f"http://127.0.0.1:{int(port)}/openai/v1"
    provider = (
        'model_providers.agentcap={name="AgentCap",'
        f'base_url="{base_url}",wire_api="responses",'
        'env_key="OPENAI_API_KEY",requires_openai_auth=false,'
        "supports_websockets=false}"
    )
    return ["-c", 'model_provider="agentcap"', "-c", provider]


def _config_values(argv: Sequence[str], start: int) -> list[str]:
    values: list[str] = []
    index = start
    while index < len(argv):
        token = str(argv[index])
        if token in ("-c", "--config"):
            if index + 1 < len(argv):
                values.append(str(argv[index + 1]))
            index += 2
            continue
        if token.startswith("--config="):
            values.append(token[len("--config=") :])
        elif token.startswith("-c="):
            values.append(token[len("-c=") :])
        index += 1
    return values


def _config_key(value: str) -> str:
    text = value.strip()
    if text.startswith(("'", '"')) and text[-1:] == text[:1]:
        text = text[1:-1]
    return text.split("=", 1)[0].strip().strip("'\"")


def codex_conflicts(argv: Sequence[str]) -> list[str]:
    """Config keys the user set that AgentCap must own."""

    detection = detect_agent(argv)
    if detection is None or detection[0] != "codex":
        return []
    conflicts: list[str] = []
    for value in _config_values(argv, detection[1]):
        key = _config_key(value)
        if key in _CODEX_CONFLICT_KEYS or key.startswith("model_providers."):
            conflicts.append(key)
    return conflicts


def ensure_codex_routing_is_safe(argv: Sequence[str]) -> None:
    """Raise :class:`AdapterConflict` when the user overrides Codex routing."""

    if codex_conflicts(argv):
        raise AdapterConflict(CODEX_CONFLICT_MESSAGE)


# -------------------------------------------------------------------- Gemini
def _read_settings(path: str | None) -> dict[str, object]:
    """Read the existing settings JSON; unreadable/invalid files are ignored."""

    if not path:
        return {}
    try:
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _deep_merge(
    base: Mapping[str, object], override: Mapping[str, object]
) -> dict[str, object]:
    merged: dict[str, object] = dict(base)
    for key, value in override.items():
        current = merged.get(key)
        if isinstance(value, Mapping):
            nested = current if isinstance(current, Mapping) else {}
            merged[key] = _deep_merge(nested, value)
        else:
            merged[key] = value
    return merged


def gemini_settings_payload(existing: Mapping[str, object] | None) -> dict[str, object]:
    """Deep-merge the AgentCap auth override over an existing settings document."""

    return _deep_merge(existing or {}, GEMINI_SETTINGS)


def _write_gemini_settings(source: str | None, directory: Path) -> Path:
    payload = gemini_settings_payload(_read_settings(source))
    path = directory / "settings.json"
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, sort_keys=True)
        handle.write("\n")
    return path


# --------------------------------------------------------------------- entry
def prepare_child(
    argv: Sequence[str],
    *,
    port: int,
    env: Mapping[str, str],
    trust_workspace: bool = False,
    tempdir_factory: Callable[..., str] = tempfile.mkdtemp,
) -> PreparedChild:
    """Apply the routing adapter for the detected CLI, if any.

    The returned ``env`` is a copy: the caller's environment is never changed,
    and the temporary Gemini settings directory is removed by ``cleanup``.
    """

    prepared_argv = list(argv)
    prepared_env = dict(env)
    detection = detect_agent(prepared_argv)
    if detection is None:
        return PreparedChild(prepared_argv, prepared_env)

    kind, index = detection
    if kind == "codex":
        ensure_codex_routing_is_safe(prepared_argv)
        prepared_argv[index:index] = codex_config_arguments(port)
        return PreparedChild(prepared_argv, prepared_env)

    source = prepared_env.get("GEMINI_CLI_SYSTEM_SETTINGS_PATH")
    directory = tempdir_factory(prefix="agentcap-gemini-")
    path = Path(directory)
    try:
        settings_path = _write_gemini_settings(source, path)
    except BaseException:
        shutil.rmtree(path, ignore_errors=True)
        raise
    prepared_env["GEMINI_CLI_SYSTEM_SETTINGS_PATH"] = str(settings_path)
    if trust_workspace:
        prepared_env["GEMINI_CLI_TRUST_WORKSPACE"] = "true"

    def cleanup() -> None:
        shutil.rmtree(path, ignore_errors=True)

    return PreparedChild(prepared_argv, prepared_env, cleanup)


__all__ = [
    "AdapterConflict",
    "CODEX_CONFLICT_MESSAGE",
    "GEMINI_SETTINGS",
    "PreparedChild",
    "codex_config_arguments",
    "codex_conflicts",
    "detect_agent",
    "ensure_codex_routing_is_safe",
    "gemini_settings_payload",
    "prepare_child",
]
