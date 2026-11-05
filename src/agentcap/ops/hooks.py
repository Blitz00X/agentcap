"""Worktree-scoped Claude Code hook for permission prompts.

``install_hooks`` adds one ``Notification`` hook to
``<worktree>/.claude/settings.local.json`` -- that worktree's own local
settings.  It never writes ``~/.claude``, another worktree, git's shared
``.git/hooks`` or git config (which every worktree of a repository shares).
It refuses: paths that are not a worktree root, symlinked ``.claude`` or
settings files, a settings file tracked by git, and files it cannot parse.
Every other key and hook in the file is kept as it was, and installing twice
changes nothing.

Before the first change the original bytes are saved in the ops state
directory.  ``uninstall_hooks`` restores them byte for byte (or deletes the
file and ``.claude`` directory it created) when nothing else changed since;
if the file was edited meanwhile it removes only the AgentCap entry.

The hook itself (``python -m agentcap.ops hook claude-notification``) turns a
``permission_prompt`` notification into a deduplicated ``permission`` signal
for the run active in that worktree and always exits 0, so it can never
break the Claude Code session.
"""

from __future__ import annotations

import base64
import copy
import hashlib
import json
import os
import shlex
import stat
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping

from .private import write_private_text
from . import gitutil
from .store import OpsError, OpsStore

HOOK_EVENT = "Notification"
MARKER = "AGENTCAP_OPS_HOOK=1"
HOOK_TIMEOUT_S = 10
SETTINGS_RELPATH = ".claude/settings.local.json"
MAX_SETTINGS_BYTES = 1024 * 1024
MAX_HOOK_INPUT = 1024 * 1024
DEDUP_WINDOW_S = 600


class HookError(OpsError):
    """The hook change was refused; nothing was written."""


@dataclass(frozen=True)
class HookReport:
    worktree: str
    settings_path: str
    action: str  # installed | updated | already-installed | restored | uninstalled | not-installed
    changed: bool
    backup_path: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def hook_command(state_dir: str | os.PathLike[str], worktree: str | os.PathLike[str], python: str | None = None) -> str:
    return " ".join(
        [
            MARKER,
            shlex.quote(python or sys.executable),
            "-m", "agentcap.ops",
            "--state-dir", shlex.quote(os.path.abspath(os.fspath(state_dir))),
            "hook", "claude-notification",
            "--worktree", shlex.quote(os.fspath(worktree)),
        ]
    )


def is_ours(command: Any) -> bool:
    return isinstance(command, str) and MARKER in command and "agentcap.ops" in command


# ------------------------------------------------------------- checks
def _root(worktree: str | os.PathLike[str]) -> str:
    real = os.path.realpath(os.fspath(worktree))
    if not os.path.isdir(real):
        raise HookError(f"{worktree} is not a directory")
    top = gitutil.toplevel(real)
    if top is None:
        raise HookError(f"{worktree} is not inside a git work tree")
    if os.path.realpath(top) != real:
        raise HookError(f"{worktree} is not the top level of its git work tree ({top}); pass the worktree root")
    return real


def _settings(root: str, home: str | os.PathLike[str] | None) -> Path:
    claude_dir = Path(root) / ".claude"
    settings = claude_dir / "settings.local.json"
    for path, kind in ((claude_dir, "directory"), (settings, "file")):
        try:
            info = os.lstat(path)
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode):
            raise HookError(f"{path} is a symlink; refusing to write through it")
        if kind == "directory" and not stat.S_ISDIR(info.st_mode):
            raise HookError(f"{path} is not a directory")
        if kind == "file" and not stat.S_ISREG(info.st_mode):
            raise HookError(f"{path} is not a regular file")
    home_claude = os.path.realpath(os.path.join(os.fspath(home) if home is not None else str(Path.home()), ".claude"))
    if os.path.realpath(claude_dir) == home_claude:
        raise HookError("refusing to modify the user-global ~/.claude directory")
    return settings


def _load(settings: Path) -> tuple[dict[str, Any] | None, bytes | None]:
    try:
        fd = os.open(settings, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except FileNotFoundError:
        return None, None
    except OSError as exc:
        raise HookError(f"cannot read {settings}: {exc.strerror or type(exc).__name__}") from None
    with os.fdopen(fd, "rb") as handle:
        raw = handle.read(MAX_SETTINGS_BYTES + 1)
    if len(raw) > MAX_SETTINGS_BYTES:
        raise HookError(f"{settings} is larger than 1 MiB; refusing to rewrite it")
    if not raw.strip():
        return {}, raw
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise HookError(f"{settings} is not valid JSON; refusing to rewrite it") from None
    if not isinstance(data, dict):
        raise HookError(f"{settings} does not hold a JSON object; refusing to rewrite it")
    return data, raw


# -------------------------------------------------------- transformation
def _our_commands(data: Mapping[str, Any] | None) -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    hooks = data.get("hooks") if isinstance(data, Mapping) else None
    if not isinstance(hooks, Mapping):
        return found
    for event, groups in hooks.items():
        if not isinstance(groups, list):
            continue
        for group in groups:
            if isinstance(group, Mapping) and isinstance(group.get("hooks"), list):
                for hook in group["hooks"]:
                    if isinstance(hook, Mapping) and is_ours(hook.get("command")):
                        found.append((event, hook["command"]))
    return found


def _strip_ours(hooks: dict[str, Any]) -> int:
    """Remove AgentCap entries (and only the groups/events they leave empty)."""

    removed = 0
    for event in list(hooks):
        groups = hooks[event]
        if not isinstance(groups, list):
            continue
        kept_groups = []
        touched = False
        for group in groups:
            if isinstance(group, dict) and isinstance(group.get("hooks"), list):
                kept = [h for h in group["hooks"] if not (isinstance(h, dict) and is_ours(h.get("command")))]
                if len(kept) != len(group["hooks"]):
                    removed += len(group["hooks"]) - len(kept)
                    touched = True
                    if not kept:
                        continue
                    group = {**group, "hooks": kept}
            kept_groups.append(group)
        if touched:
            if kept_groups:
                hooks[event] = kept_groups
            else:
                del hooks[event]
    return removed


def _with_ours(data: Mapping[str, Any] | None, command: str) -> dict[str, Any]:
    new = copy.deepcopy(dict(data)) if data is not None else {}
    hooks = new.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise HookError("'hooks' in the settings file is not an object; refusing to rewrite it")
    if HOOK_EVENT in hooks and not isinstance(hooks[HOOK_EVENT], list):
        raise HookError(f"'hooks.{HOOK_EVENT}' in the settings file is not a list; refusing to rewrite it")
    _strip_ours(hooks)
    hooks.setdefault(HOOK_EVENT, []).append(
        {"hooks": [{"type": "command", "command": command, "timeout": HOOK_TIMEOUT_S}]}
    )
    return new


def _atomic_write(path: Path, blob: bytes) -> None:
    try:
        mode = stat.S_IMODE(os.lstat(path).st_mode)
    except FileNotFoundError:
        mode = 0o600
    tmp = path.with_name(f".{path.name}.agentcap-ops.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), mode)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(blob)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _write_settings(settings: Path, data: Mapping[str, Any]) -> None:
    settings.parent.mkdir(mode=0o700, exist_ok=True)
    _atomic_write(settings, (json.dumps(data, indent=2, ensure_ascii=False) + "\n").encode("utf-8"))


# ------------------------------------------------------------- backups
def _backup_path(state_dir: str | os.PathLike[str], root: str) -> Path:
    digest = hashlib.sha256(root.encode("utf-8")).hexdigest()[:20]
    return Path(os.path.abspath(os.fspath(state_dir))) / "hook-backups" / f"{digest}.json"


def _read_backup(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _backup_original(backup: Mapping[str, Any]) -> tuple[bytes | None, dict[str, Any] | None, bool]:
    """(original bytes, original parsed JSON, readable) from a backup record."""

    encoded = backup.get("original_b64")
    if encoded is None:
        return None, None, True
    try:
        raw = base64.b64decode(encoded)
        parsed = json.loads(raw.decode("utf-8")) if raw.strip() else {}
    except (ValueError, UnicodeDecodeError):
        return None, None, False
    return raw, parsed if isinstance(parsed, dict) else None, isinstance(parsed, dict)


# --------------------------------------------------------------- public
def install_hooks(
    worktree: str | os.PathLike[str],
    *,
    state_dir: str | os.PathLike[str],
    python: str | None = None,
    home: str | os.PathLike[str] | None = None,
) -> HookReport:
    root = _root(worktree)
    settings = _settings(root, home)
    if gitutil.is_tracked(root, SETTINGS_RELPATH):
        raise HookError(f"{settings} is tracked by git; refusing to modify a committed file")
    data, raw = _load(settings)
    command = hook_command(state_dir, root, python)
    backup_path = _backup_path(state_dir, root)
    current = _our_commands(data)
    if current == [(HOOK_EVENT, command)]:
        return HookReport(root, str(settings), "already-installed", False,
                          str(backup_path) if backup_path.exists() else None)
    new = _with_ours(data, command)  # raises before anything is written
    backup = _read_backup(backup_path)
    if backup is None:
        backup = {
            "worktree": root,
            "settings_path": str(settings),
            "claude_dir_existed": settings.parent.is_dir(),
            "original_b64": base64.b64encode(raw).decode("ascii") if raw is not None else None,
            "original_sha256": hashlib.sha256(raw).hexdigest() if raw is not None else None,
        }
    backup["command"] = command
    write_private_text(backup_path, json.dumps(backup, indent=2, sort_keys=True) + "\n")
    _write_settings(settings, new)
    return HookReport(root, str(settings), "updated" if current else "installed", True, str(backup_path))


def uninstall_hooks(
    worktree: str | os.PathLike[str],
    *,
    state_dir: str | os.PathLike[str],
    home: str | os.PathLike[str] | None = None,
) -> HookReport:
    root = _root(worktree)
    settings = _settings(root, home)
    data, _ = _load(settings)
    backup_path = _backup_path(state_dir, root)
    backup = _read_backup(backup_path)
    if data is None or not _our_commands(data):
        return HookReport(root, str(settings), "not-installed", False, str(backup_path) if backup else None)

    original_had_hooks = True
    if backup is not None:
        original_raw, original, readable = _backup_original(backup)
        if readable:
            original_had_hooks = original is not None and "hooks" in original
            try:
                expected = _with_ours(original, str(backup.get("command", "")))
            except HookError:
                expected = None
            if expected is not None and data == expected:
                # Untouched since install: put back exactly what was there.
                if original_raw is None:
                    settings.unlink()
                    if not backup.get("claude_dir_existed", True):
                        try:
                            settings.parent.rmdir()
                        except OSError:
                            pass  # someone added files to .claude meanwhile; keep them
                else:
                    _atomic_write(settings, original_raw)
                backup_path.unlink(missing_ok=True)
                return HookReport(root, str(settings), "restored", True, None)

    # Edited since install: remove only the AgentCap entry, keep everything else.
    new = copy.deepcopy(data)
    hooks = new.get("hooks")
    if isinstance(hooks, dict):
        _strip_ours(hooks)
        if not hooks and not original_had_hooks:
            del new["hooks"]
    _write_settings(settings, new)
    if backup is not None:
        backup_path.unlink(missing_ok=True)
    return HookReport(root, str(settings), "uninstalled", True, None)


def hooks_status(
    worktree: str | os.PathLike[str],
    *,
    state_dir: str | os.PathLike[str],
    python: str | None = None,
    home: str | os.PathLike[str] | None = None,
) -> dict[str, Any]:
    root = _root(worktree)
    settings = _settings(root, home)
    data, _ = _load(settings)
    command = hook_command(state_dir, root, python)
    ours = _our_commands(data)
    return {
        "worktree": root,
        "settings_path": str(settings),
        "exists": data is not None,
        "installed": (HOOK_EVENT, command) in ours,
        "entries": len(ours),
        "stale_entries": sum(1 for _, cmd in ours if cmd != command),
        "tracked": gitutil.is_tracked(root, SETTINGS_RELPATH),
        "git_ignored": gitutil.is_ignored(root, SETTINGS_RELPATH),
        "backup": _backup_path(state_dir, root).exists(),
    }


def handle_claude_notification(
    store: OpsStore,
    raw: bytes,
    *,
    worktree: str | None = None,
    environ: Mapping[str, str] | None = None,
) -> dict[str, Any] | None:
    """Record a Claude Code permission prompt as a ``permission`` signal (deduplicated)."""

    env = os.environ if environ is None else environ
    try:
        payload = json.loads(raw.decode("utf-8")) if raw.strip() else None
    except (UnicodeDecodeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    message = payload.get("message") if isinstance(payload.get("message"), str) else ""
    kind = payload.get("notification_type")
    if kind is not None:
        if kind != "permission_prompt":
            return None  # idle_prompt, auth_success, ...: not a permission block
    elif "permission" not in message.lower():
        return None

    run_id = env.get("AGENTCAP_OPS_RUN_ID") or None
    if run_id is not None and store.get_run(run_id) is None:
        run_id = None
    root = None
    if run_id is None:
        cwd = payload.get("cwd")
        if worktree:
            root = os.path.realpath(worktree)
        elif isinstance(cwd, str) and os.path.isabs(cwd):
            root = os.path.realpath(cwd)
        else:
            return None
    session = str(payload.get("session_id") or "")
    window = int(store.clock() // DEDUP_WINDOW_S)
    basis = "|".join([session, run_id or "", root or "", message, str(window)])
    key = "claude-notification:" + hashlib.sha256(basis.encode("utf-8")).hexdigest()[:32]
    recorded, _ = store.record_signal(
        kind="permission",
        source="hook:claude-notification",
        detail=message or "permission prompt",
        run_id=run_id,
        worktree=root,
        dedup_key=key,
    )
    return recorded


__all__ = [
    "HOOK_EVENT",
    "HookError",
    "HookReport",
    "MARKER",
    "handle_claude_notification",
    "hook_command",
    "hooks_status",
    "install_hooks",
    "uninstall_hooks",
]
