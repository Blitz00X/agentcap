"""Hand the ops notification outbox to the coordinator's existing, authorized sender.

AgentCap never holds a notification secret.  The coordinator's existing
deterministic sender -- a local program that keeps the channel's endpoint URL
and bearer token in its own private settings -- is the only component that
uses them.  AgentCap's delivery config names that program and, at most,
*which* endpoint and bearer the sender should use (reference names it
resolves itself), never their values::

    {"command": ["/abs/path/sender", "--endpoint", "{endpoint}", "--bearer", "{bearer}",
                 "--text", "{message}"],     # no shell, no PATH lookup
     "endpoint": "ops-alerts",               # a name, not a URL
     "bearer": "ops-alerts-token",           # a name in the sender's own store, not a token
     "format": "text",                       # text | json: what the sender gets on stdin
     "timeout_s": 10, "max_attempts": 8}

Anything that would hand AgentCap the secret itself is refused, and error
messages never echo config values: ``url``, ``headers``, ``chat_id``,
``token``-like keys, the ``telegram`` format, and command arguments holding a
URL (``://``), a ``Bearer`` value or ``token=``/``password=``-style
credentials.  The sender program must be owned by the user (or root) and not
writable by group or others.

The config is read only by ``deliver``, at runtime, from an owner-only file
(``--config PATH`` or ``$AGENTCAP_OPS_CONFIG``) outside any git work tree or
an inherited descriptor (``--config-fd N``, closed after reading), and never
inside a tracked model run.  It is never copied into the state directory,
logs or notification rows: delivery attempts keep a fingerprint of the sender
call and an error *class* only, and the sender's own output is discarded.

Delivery is at-least-once: one deliverer at a time (``deliver.lock``), each
notification carries a stable ``event_id`` (``$AGENTCAP_OPS_EVENT_ID`` for
the sender's own deduplication), and any failure puts it back to ``pending``
with exponential backoff (30 s up to 1 h).  After ``max_attempts`` it is
``failed`` -- still stored, listed by ``status`` and requeued by ``notify
retry``.
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import signal
import stat
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping

from . import events, gitutil
from .locks import FileLock, LockBusy
from .runner import ENV_PREFIX, EXIT_BUSY, RUN_MARKER, inside_model_run, recover_stale_runs
from .store import OpsError, OpsStore

CONFIG_ENV = "AGENTCAP_OPS_CONFIG"
FORMATS = ("json", "text")
MAX_CONFIG_BYTES = 64 * 1024
DEFAULT_MAX_ATTEMPTS = 8
BACKOFF_BASE_S = 30.0
BACKOFF_MAX_S = 3600.0
MAX_COMMAND_PARTS = 64
_CONFIG_KEYS = frozenset({"command", "format", "endpoint", "bearer", "timeout_s", "max_attempts"})
#: Keys that would hand AgentCap a secret (the former direct-HTTP config and look-alikes).
_SECRET_KEYS = frozenset(
    {"url", "headers", "chat_id", "token", "bot_token", "authorization", "password", "secret", "api_key", "apikey"}
)
_REF_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_SECRET_ARG = re.compile(
    r"://"
    r"|(?i:\bbearer\s+\S)"
    r"|(?i:\bauthorization\s*[:=])"
    r"|(?i:(?:token|api[-_]?key|password|passwd|secret)\s*[:=]\s*\S)"
)


class ConfigError(OpsError):
    """The sender config is missing, unsafe or invalid (values are never echoed)."""


@dataclass(frozen=True, repr=False)
class SenderConfig:
    """The coordinator's existing sender: argv plus the *names* of its endpoint and bearer."""

    command: tuple[str, ...]
    format: str = "json"
    endpoint: str | None = None
    bearer: str | None = None
    timeout_s: float = 10.0
    max_attempts: int = DEFAULT_MAX_ATTEMPTS

    @property
    def fingerprint(self) -> str:
        """Short, non-reversible id of the sender call for attempt records."""

        basis = "\0".join([*self.command, f"endpoint={self.endpoint or ''}", f"bearer={self.bearer or ''}"])
        return "cmd:sha256:" + hashlib.sha256(basis.encode("utf-8")).hexdigest()[:12]

    def __repr__(self) -> str:
        return f"SenderConfig(format={self.format!r}, sender={self.fingerprint})"

    __str__ = __repr__


def parse_config(data: bytes) -> SenderConfig:
    if len(data) > MAX_CONFIG_BYTES:
        raise ConfigError("notification config is larger than 64 KiB")
    try:
        raw = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ConfigError("notification config is not valid UTF-8 JSON") from None
    if not isinstance(raw, dict):
        raise ConfigError("notification config must be a JSON object")
    secret_keys = sorted(key for key in raw if key.lower() in _SECRET_KEYS)
    if secret_keys or raw.get("format") == "telegram":
        what = f"refused keys: {', '.join(secret_keys)}" if secret_keys else "the telegram format needs a bot token"
        raise ConfigError(
            f"AgentCap does not take endpoint URLs, headers, chat ids or tokens ({what}); keep them in your "
            "existing sender and name them with config.endpoint / config.bearer"
        )
    unknown = sorted(set(raw) - _CONFIG_KEYS)
    if unknown:
        raise ConfigError(f"unknown config keys: {', '.join(unknown)}")
    if "command" not in raw:
        raise ConfigError(
            "config.command is required: the existing sender's absolute program path, then its arguments"
        )
    endpoint = _parse_ref(raw, "endpoint")
    bearer = _parse_ref(raw, "bearer")
    command = _parse_command(raw["command"], endpoint=endpoint, bearer=bearer)

    fmt = raw.get("format", "json")
    if fmt not in FORMATS:
        raise ConfigError(f"config.format must be one of {', '.join(FORMATS)}")
    timeout = raw.get("timeout_s", 10.0)
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 1 <= timeout <= 60:
        raise ConfigError("config.timeout_s must be a number between 1 and 60")
    attempts = raw.get("max_attempts", DEFAULT_MAX_ATTEMPTS)
    if isinstance(attempts, bool) or not isinstance(attempts, int) or not 1 <= attempts <= 100:
        raise ConfigError("config.max_attempts must be an integer between 1 and 100")
    return SenderConfig(
        command=command, format=fmt, endpoint=endpoint, bearer=bearer,
        timeout_s=float(timeout), max_attempts=attempts,
    )


def _parse_ref(raw: Mapping[str, Any], key: str) -> str | None:
    """A reference name the sender resolves itself (never a URL or a token)."""

    value = raw.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not _REF_RE.match(value):
        raise ConfigError(
            f"config.{key} must be a reference name your sender resolves itself (1-64 lowercase "
            "letters, digits, '.', '_' or '-'), not a URL or a token"
        )
    return value


def _parse_command(value: Any, *, endpoint: str | None, bearer: str | None) -> tuple[str, ...]:
    """argv of the existing sender: absolute program path, no shell, no PATH lookup, no secrets."""

    if (
        not isinstance(value, list)
        or not 1 <= len(value) <= MAX_COMMAND_PARTS
        or not all(isinstance(part, str) and "\0" not in part for part in value)
    ):
        raise ConfigError(
            f"config.command must be a list of 1-{MAX_COMMAND_PARTS} strings (the program, then its arguments)"
        )
    program, arguments = value[0], value[1:]
    if not os.path.isabs(program):
        raise ConfigError("config.command[0] must be an absolute path (no PATH lookup, no shell)")
    if any(_SECRET_ARG.search(part) for part in arguments):
        raise ConfigError(
            "config.command arguments must not carry URLs, bearer tokens or credentials; pass the "
            "{endpoint} / {bearer} placeholders with config.endpoint / config.bearer reference names"
        )
    if "{endpoint}" in arguments and endpoint is None:
        raise ConfigError("config.command uses {endpoint} but config.endpoint is not set")
    if "{bearer}" in arguments and bearer is None:
        raise ConfigError("config.command uses {bearer} but config.bearer is not set")
    _check_sender(program)
    return tuple(value)


def _check_sender(program: str) -> None:
    """The authorized sender: an executable regular file only the user (or root) can change.

    Only metadata is looked at: the program's content -- and whatever settings
    or tokens it keeps -- is never read.
    """

    try:
        info = os.stat(program)
    except OSError:
        raise ConfigError("config.command[0] is not an executable file") from None
    if not stat.S_ISREG(info.st_mode) or not os.access(program, os.X_OK):
        raise ConfigError("config.command[0] is not an executable file")
    if info.st_uid not in (os.getuid(), 0):
        raise ConfigError("config.command[0] must be owned by the current user or root")
    if info.st_mode & 0o022:
        raise ConfigError("config.command[0] must not be writable by group or others (chmod go-w)")


def _read_limited(fd: int) -> bytes:
    chunks: list[bytes] = []
    size = 0
    while True:
        chunk = os.read(fd, 65536)
        if not chunk:
            return b"".join(chunks)
        size += len(chunk)
        if size > MAX_CONFIG_BYTES:
            raise ConfigError("notification config is larger than 64 KiB")
        chunks.append(chunk)


def load_config_file(path: str | os.PathLike[str]) -> SenderConfig:
    """Load an owner-only (0600) regular file that lives outside any git work tree."""

    location = Path(path).expanduser()
    try:
        fd = os.open(location, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0))
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise ConfigError(f"notification config {location} must not be a symlink") from None
        raise ConfigError(
            f"cannot open notification config {location} ({exc.strerror or type(exc).__name__})"
        ) from None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise ConfigError(f"notification config {location} must be a regular file")
        if info.st_uid != os.getuid():
            raise ConfigError(f"notification config {location} must be owned by the current user")
        if info.st_mode & 0o077:
            raise ConfigError(
                f"notification config {location} must not be accessible by group or others (chmod 600)"
            )
        data = _read_limited(fd)
    finally:
        os.close(fd)
    if gitutil.inside_work_tree(location.absolute().parent):
        raise ConfigError(
            f"notification config {location} must live outside any git work tree "
            "(a model working there could read or commit it)"
        )
    return parse_config(data)


def load_config_fd(fd: int) -> SenderConfig:
    """Read the config from an inherited descriptor, then close it (if not stdio)."""

    try:
        info = os.fstat(fd)
    except OSError:
        raise ConfigError(f"config fd {fd} is not open") from None
    try:
        if stat.S_ISREG(info.st_mode) and info.st_mode & 0o077:
            raise ConfigError("notification config passed by fd must not be accessible by group or others (chmod 600)")
        data = _read_limited(fd)
    finally:
        if fd > 2:
            os.close(fd)
    return parse_config(data)


def resolve_config(
    *,
    path: str | os.PathLike[str] | None = None,
    fd: int | None = None,
    environ: Mapping[str, str] | None = None,
) -> SenderConfig:
    env = os.environ if environ is None else environ
    if inside_model_run(env):
        if fd is not None and fd > 2:
            try:
                os.close(fd)
            except OSError:
                pass
        raise ConfigError(
            f"deliver runs from the coordinator, never inside a tracked model run ({RUN_MARKER} is set); "
            "the notification config was not read"
        )
    if path is not None and fd is not None:
        raise ConfigError("give either --config or --config-fd, not both")
    if fd is not None:
        return load_config_fd(fd)
    if path:
        return load_config_file(path)
    if env.get(CONFIG_ENV):
        return load_config_file(env[CONFIG_ENV])
    raise ConfigError(f"no notification config given (--config PATH, --config-fd N or ${CONFIG_ENV})")


# -------------------------------------------------------------- sending
@dataclass(frozen=True)
class SendResult:
    ok: bool
    outcome: str  # delivered | sender_error | timeout | internal_error
    # An exception class or a sender's "exit <code>": never message text.
    error: str | None = None


def build_sender_call(
    config: SenderConfig, payload: Mapping[str, Any]
) -> tuple[list[str], bytes, dict[str, str]]:
    """argv, stdin and environment for the existing sender.

    Deterministic: the same notification always gives the same argv and
    stdin.  An argument that is exactly ``{message}``, ``{kind}``,
    ``{event}``, ``{event_id}``, ``{endpoint}`` or ``{bearer}`` is replaced by
    that value -- the last two by the configured *reference names* (whole
    arguments only; there is no shell).  The environment is the deliverer's
    without ``AGENTCAP_OPS_*`` (config path, state dir), plus
    ``AGENTCAP_OPS_EVENT`` (the kind), the stable ``AGENTCAP_OPS_EVENT_ID`` and,
    when configured, ``AGENTCAP_OPS_ENDPOINT_REF`` / ``AGENTCAP_OPS_BEARER_REF``.
    """

    message = str(payload.get("message") or events.message(payload))
    kind = str(payload.get("kind") or payload.get("event") or "")
    event_id = str(payload.get("event_id") or "")
    values = {
        "{message}": message,
        "{kind}": kind,
        "{event}": kind,
        "{event_id}": event_id,
        "{endpoint}": config.endpoint or "",
        "{bearer}": config.bearer or "",
    }
    argv = [config.command[0], *(values.get(part, part).replace("\0", " ") for part in config.command[1:])]
    if config.format == "text":
        # "replace": a lone surrogate in a stored detail must not crash every delivery pass.
        data = message.encode("utf-8", "replace") + b"\n"
    else:
        data = json.dumps(dict(payload), sort_keys=True).encode("utf-8") + b"\n"
    env = {key: value for key, value in os.environ.items() if not key.startswith(ENV_PREFIX)}
    env["AGENTCAP_OPS_EVENT"] = kind
    env["AGENTCAP_OPS_EVENT_ID"] = event_id
    if config.endpoint:
        env["AGENTCAP_OPS_ENDPOINT_REF"] = config.endpoint
    if config.bearer:
        env["AGENTCAP_OPS_BEARER_REF"] = config.bearer
    return argv, data, env


def _exit_text(code: int) -> str:
    if code < 0:
        try:
            return f"killed by {signal.Signals(-code).name}"
        except ValueError:
            return f"killed by signal {-code}"
    return f"exit {code}"


def send_one(config: SenderConfig, payload: Mapping[str, Any]) -> SendResult:
    """Hand one notification to the existing sender; exit 0 means delivered."""

    argv, data, env = build_sender_call(config, payload)
    try:
        proc = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE,
            # Its output may echo its own settings or tokens: never kept anywhere.
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=env,
            close_fds=True,  # never the delivery lock or any other descriptor
            start_new_session=True,  # own process group: a timeout stops its helpers too
        )
    except (OSError, ValueError) as exc:
        return SendResult(False, "sender_error", error=type(exc).__name__)
    try:
        proc.communicate(data, timeout=config.timeout_s)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            pass
        proc.communicate()
        if proc.stdin is not None:
            try:
                proc.stdin.close()
            except OSError:
                pass
        return SendResult(False, "timeout", error="TimeoutExpired")
    if proc.returncode == 0:
        return SendResult(True, "delivered")
    return SendResult(False, "sender_error", error=_exit_text(proc.returncode))


def backoff_s(attempts: int) -> float:
    return min(BACKOFF_MAX_S, BACKOFF_BASE_S * (2 ** max(0, attempts - 1)))


@dataclass
class DeliveryReport:
    busy: bool = False
    recovered_runs: int = 0
    recovered_sending: int = 0
    delivered: int = 0
    retried: int = 0
    dead_lettered: int = 0
    pending: int = 0
    failed_total: int = 0

    @property
    def exit_code(self) -> int:
        """0 only when nothing is left pending or failed; 75 if another deliverer runs."""

        if self.busy:
            return EXIT_BUSY
        return 0 if self.pending == 0 and self.failed_total == 0 else 1

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["exit_code"] = self.exit_code
        return data


def deliver(
    store: OpsStore,
    config: SenderConfig,
    *,
    limit: int = 100,
    recover_runs: bool = True,
) -> DeliveryReport:
    """One delivery pass over due notifications (single deliverer, failures kept)."""

    lock = FileLock(store.delivery_lock_path, label="ops deliver")
    try:
        lock.acquire(timeout=0.0)
    except LockBusy:
        return DeliveryReport(busy=True)
    try:
        report = DeliveryReport()
        if recover_runs:
            report.recovered_runs = len(recover_stale_runs(store))
        # We hold the delivery lock, so any "sending" row belongs to a dead deliverer.
        report.recovered_sending = store.reset_sending()
        for _ in range(max(0, int(limit))):
            item = store.claim_next()
            if item is None:
                break
            try:
                payload = json.loads(item["payload_json"])
            except ValueError:
                payload = {"message": f"agentcap-ops: {item['event']}", "event_id": ""}
            try:
                result = send_one(config, payload)
            except Exception as exc:  # one malformed row must not wedge the whole queue
                result = SendResult(False, "internal_error", error=type(exc).__name__)
            if result.ok:
                store.complete_delivery(item["id"], outcome="delivered", endpoint=config.fingerprint)
                report.delivered += 1
                continue
            give_up = item["attempts"] >= config.max_attempts
            store.complete_delivery(
                item["id"], outcome=result.outcome, endpoint=config.fingerprint, error=result.error,
                next_attempt_at=store.clock() + backoff_s(item["attempts"]), give_up=give_up,
            )
            if give_up:
                report.dead_lettered += 1
            else:
                report.retried += 1
        counts = store.notification_counts()
        report.pending = counts["pending"] + counts["sending"]
        report.failed_total = counts["failed"]
        return report
    finally:
        lock.release()


def requeue_abandoned_claims(store: OpsStore) -> int | None:
    """Put notifications a stopped deliverer left ``sending`` back to ``pending``.

    Used by ``recover`` (``deliver`` does the same under its own lock).  Returns
    ``None`` and changes nothing while a live deliverer holds the delivery lock:
    its claims are in flight, not abandoned.
    """

    lock = FileLock(store.delivery_lock_path, label="ops recover")
    try:
        lock.acquire(timeout=0.0)
    except LockBusy:
        return None
    try:
        return store.reset_sending()
    finally:
        lock.release()


__all__ = [
    "CONFIG_ENV",
    "ConfigError",
    "DeliveryReport",
    "SendResult",
    "SenderConfig",
    "backoff_s",
    "build_sender_call",
    "deliver",
    "load_config_fd",
    "load_config_file",
    "parse_config",
    "requeue_abandoned_claims",
    "resolve_config",
    "send_one",
]
