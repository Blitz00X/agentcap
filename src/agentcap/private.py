"""Private-by-default filesystem helpers for AgentCap's local state.

AgentCap keeps its state under ``~/.local/state/agentcap`` (run ids, spend,
quota readings, receipts).  That is user data, not world-readable data: the
defaults here make sure a fresh install does not leak it to other local
accounts through a permissive umask.
"""

from __future__ import annotations

import os
from pathlib import Path

_PRIVATE_FILE = 0o600
_PRIVATE_DIR = 0o700


def ensure_private_dir(path: Path) -> Path:
    """Create ``path`` if needed and keep it owner-only (0700)."""

    path.mkdir(parents=True, exist_ok=True, mode=_PRIVATE_DIR)
    try:
        os.chmod(path, _PRIVATE_DIR)
    except OSError:
        # Best effort: the directory may be on a filesystem without POSIX
        # modes.  Files inside are still opened with 0600.
        pass
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
