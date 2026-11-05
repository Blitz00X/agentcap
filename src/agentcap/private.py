"""Private-by-default filesystem helpers for AgentCap's local state.

AgentCap keeps its state under ``~/.local/state/agentcap`` (run ids, spend,
quota readings, receipts).  That is user data, not world-readable data: the
defaults here make sure a fresh install does not leak it to other local
accounts through a permissive umask.  Directories AgentCap creates, and its
own default directories, are 0700; an existing directory a path option points
into (``$HOME``, a project, a shared dir) is never chmodded.
"""

from __future__ import annotations

import os
from pathlib import Path

_PRIVATE_FILE = 0o600
_PRIVATE_DIR = 0o700


def _default_roots() -> list[Path]:
    """AgentCap's own default directories (and everything under them).

    Mirrors the defaults of ledger/runner/quota/ops (state), history (data)
    and budget (config).  Paths chosen through ``AGENTCAP_*`` options are
    not listed: those may be a user, project or shared directory.
    """

    home = Path.home()
    data = os.environ.get("XDG_DATA_HOME", "").strip()
    config = os.environ.get("XDG_CONFIG_HOME", "").strip()
    return [
        home / ".local" / "state" / "agentcap",
        (Path(data).expanduser() if data else home / ".local" / "share") / "agentcap",
        (Path(config).expanduser() if config else home / ".config") / "agentcap",
    ]


def _tighten(path: Path) -> None:
    """chmod ``path`` to 0700 without following a symlink (best effort)."""

    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        # A symlink (ELOOP), a non-POSIX filesystem or a vanished directory:
        # leave it alone.  Files inside are still opened with 0600.
        return
    try:
        os.fchmod(descriptor, _PRIVATE_DIR)
    except (OSError, AttributeError, NotImplementedError):
        pass
    finally:
        os.close(descriptor)


def _create_missing(path: Path) -> list[Path]:
    """Create the missing directories of ``path``; return only those created here."""

    missing: list[Path] = []
    current = Path(os.path.abspath(path))
    while not os.path.lexists(current):
        missing.append(current)
        if current.parent == current:
            break
        current = current.parent
    created: list[Path] = []
    for directory in reversed(missing):
        try:
            os.mkdir(directory, _PRIVATE_DIR)
        except FileExistsError:
            continue  # someone else made it meanwhile: not ours to chmod
        created.append(directory)
    return created


def _within(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


def _owned_chain(path: Path) -> list[Path]:
    """``path`` and its ancestors up to the AgentCap default root holding it.

    Empty unless ``path`` lies inside one of AgentCap's own default
    directories, both as written and after resolving symlinks, so a symlink
    under the state directory cannot steer a chmod into a user directory.
    """

    try:
        roots = _default_roots()
    except (RuntimeError, KeyError, OSError):
        return []
    target = os.path.abspath(path)
    for root in roots:
        base = os.path.abspath(root)
        if not _within(target, base):
            continue
        if not _within(os.path.realpath(target), os.path.realpath(base)):
            continue
        chain = [target]
        while chain[-1] != base:
            chain.append(os.path.dirname(chain[-1]))
        return [Path(directory) for directory in chain]
    return []


def ensure_private_dir(path: Path) -> Path:
    """Create ``path`` if needed; make AgentCap's own directories owner-only.

    Directories created here (including missing parents) are 0700 whatever
    the umask.  An existing directory keeps its mode -- a user, project or
    shared directory that ``AGENTCAP_DB``-style options point into is never
    chmodded -- unless it is one of AgentCap's own default directories,
    which are tightened to 0700.  Symlinks are never chmodded through.
    """

    created = _create_missing(path)
    # Same errors as before for a file or dangling symlink in the way.
    path.mkdir(parents=True, exist_ok=True, mode=_PRIVATE_DIR)
    for directory in created:
        _tighten(directory)
    for directory in _owned_chain(path):
        if directory not in created:
            _tighten(directory)
    return path


def open_private(
    path: Path,
    flags: int = os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
) -> int:
    """Open a state file with owner-only permissions, refusing symlinks."""

    flags |= getattr(os, "O_NOFOLLOW", 0)
    return os.open(path, flags, _PRIVATE_FILE)


def write_private_text(path: Path, text: str) -> None:
    """Atomically write ``text`` to ``path`` with owner-only permissions."""

    ensure_private_dir(path.parent)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    descriptor = open_private(tmp)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    try:
        os.chmod(path, _PRIVATE_FILE)
    except OSError:
        pass


__all__ = ["ensure_private_dir", "open_private", "write_private_text"]
