"""Per-CLI routing adapters.

Some agent CLIs do not honour a plain base-URL environment variable:

* Codex CLI 0.163 ignores ``OPENAI_BASE_URL`` and defaults to a websocket
  transport.  It has to be pointed at the proxy with ``-c`` configuration
  overrides (``model_provider`` plus a custom provider that uses the Responses
  API over plain HTTP).
* Gemini CLI reads settings for its authentication type when it runs headless;
  a gateway base URL alone makes it pick an auth type that headless mode
  rejects.  Gemini CLI >= 0.60 also refuses system settings files whose parent
  directory is not owned by root, so the old
  ``GEMINI_CLI_SYSTEM_SETTINGS_PATH`` overlay is silently skipped.  Instead a
  temporary ``GEMINI_CLI_HOME`` is used: its ``.gemini`` directory links every
  real user entry (history, ``trustedFolders.json``, OAuth credentials, ...)
  and carries a real ``settings.json`` that selects ``gemini-api-key``.  The
  overlay is removed after the run and nothing in the real ``~/.gemini`` is
  modified.

Everything here is pure (apart from creating the temporary Gemini home) so it
can be unit-tested without spawning a process.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
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
GEMINI_SETTINGS_FILENAME = "settings.json"

_GEMINI_SETTINGS_WARNING = (
    "agentcap: could not parse ~/.gemini/settings.json; "
    "using defaults for this run"
)


def gemini_home(env: Mapping[str, str]) -> Path:
    """The home directory Gemini would use without AgentCap's overlay."""

    for name in ("GEMINI_CLI_HOME", "HOME"):
        value = env.get(name)
        if value and value.strip():
            return Path(value)
    return Path(os.path.expanduser("~"))


def _strip_jsonc(text: str) -> str:
    """Drop ``//`` and ``/* */`` comments plus trailing commas outside strings."""

    without_comments: list[str] = []
    in_string = False
    escaped = False
    index = 0
    length = len(text)
    while index < length:
        char = text[index]
        if in_string:
            without_comments.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            index += 1
            continue
        if char == '"':
            in_string = True
            without_comments.append(char)
            index += 1
            continue
        if char == "/" and index + 1 < length:
            following = text[index + 1]
            if following == "/":
                index += 2
                while index < length and text[index] not in "\r\n":
                    index += 1
                continue
            if following == "*":
                index += 2
                while index + 1 < length and not (
                    text[index] == "*" and text[index + 1] == "/"
                ):
                    index += 1
                index += 2
                continue
        without_comments.append(char)
        index += 1

    text = "".join(without_comments)

    result: list[str] = []
    in_string = False
    escaped = False
    index = 0
    length = len(text)
    while index < length:
        char = text[index]
        if in_string:
            result.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            index += 1
            continue
        if char == '"':
            in_string = True
            result.append(char)
            index += 1
            continue
        if char == ",":
            lookahead = index + 1
            while lookahead < length and text[lookahead] in " \t\r\n":
                lookahead += 1
            if lookahead < length and text[lookahead] in "}]":
                index += 1
                continue
        result.append(char)
        index += 1
    return "".join(result)


def _parse_settings_text(text: str) -> dict[str, object] | None:
    for candidate in (text, _strip_jsonc(text)):
        try:
            payload = json.loads(candidate)
        except ValueError:
            continue
        return payload if isinstance(payload, dict) else None
    return None


def _read_user_settings(path: Path) -> dict[str, object]:
    """Read the user's Gemini settings; missing files simply mean defaults."""

    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except OSError:
        print(_GEMINI_SETTINGS_WARNING, file=sys.stderr)
        return {}
    payload = _parse_settings_text(text)
    if payload is None:
        print(_GEMINI_SETTINGS_WARNING, file=sys.stderr)
        return {}
    return payload


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


def _write_gemini_settings(existing: Mapping[str, object] | None, path: Path) -> Path:
    payload = gemini_settings_payload(existing)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, sort_keys=True)
        handle.write("\n")
    os.chmod(path, 0o600)
    return path


def _build_gemini_home(real_gemini: Path, root: Path) -> Path:
    """Create ``<root>/.gemini`` linking the real entries and forcing auth."""

    overlay = root / ".gemini"
    overlay.mkdir(mode=0o700, exist_ok=True)
    os.chmod(overlay, 0o700)
    if real_gemini.is_dir():
        for entry in sorted(real_gemini.iterdir()):
            if entry.name == GEMINI_SETTINGS_FILENAME:
                continue
            try:
                os.symlink(
                    os.path.abspath(str(entry)),
                    overlay / entry.name,
                    target_is_directory=entry.is_dir(),
                )
            except OSError:
                # Unsupported entry (or a race); the overlay still works.
                continue
    _write_gemini_settings(
        _read_user_settings(real_gemini / GEMINI_SETTINGS_FILENAME),
        overlay / GEMINI_SETTINGS_FILENAME,
    )
    return overlay


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
    and the temporary Gemini home is removed by ``cleanup``.
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

    real_gemini = gemini_home(prepared_env) / ".gemini"
    root = Path(tempdir_factory(prefix="agentcap-gemini-"))
    try:
        os.chmod(root, 0o700)
        _build_gemini_home(real_gemini, root)
    except BaseException:
        shutil.rmtree(root, ignore_errors=True)
        raise
    prepared_env["GEMINI_CLI_HOME"] = str(root)
    if trust_workspace:
        prepared_env["GEMINI_CLI_TRUST_WORKSPACE"] = "true"

    def cleanup() -> None:
        # rmtree unlinks the entry symlinks instead of descending into them.
        shutil.rmtree(root, ignore_errors=True)

    return PreparedChild(prepared_argv, prepared_env, cleanup)


__all__ = [
    "AdapterConflict",
    "CODEX_CONFLICT_MESSAGE",
    "GEMINI_SETTINGS",
    "GEMINI_SETTINGS_FILENAME",
    "PreparedChild",
    "codex_config_arguments",
    "codex_conflicts",
    "detect_agent",
    "ensure_codex_routing_is_safe",
    "gemini_home",
    "gemini_settings_payload",
    "prepare_child",
]
