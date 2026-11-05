"""Read-only git queries for run evidence, commit checks and worktree checks.

Nothing here writes to a repository: ``GIT_OPTIONAL_LOCKS=0`` keeps even
``status``-style commands from refreshing the index while a model works.
"""

from __future__ import annotations

import os
import re
import subprocess

GIT_TIMEOUT_S = 15.0
_HEX_REF = re.compile(r"^[0-9a-fA-F]{4,64}$")


def _run(cwd: str | os.PathLike[str], args: tuple[str, ...], timeout: float) -> subprocess.CompletedProcess[str] | None:
    env = dict(os.environ)
    env.update({"GIT_OPTIONAL_LOCKS": "0", "GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C"})
    try:
        return subprocess.run(
            ["git", "-C", os.fspath(cwd), *args],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            env=env,
        )
    except (OSError, subprocess.SubprocessError):
        return None


def git(cwd: str | os.PathLike[str], *args: str, timeout: float = GIT_TIMEOUT_S) -> str | None:
    """Stripped stdout of ``git -C cwd args``, or None if git failed or is missing."""

    result = _run(cwd, args, timeout)
    if result is None or result.returncode != 0:
        return None
    return result.stdout.strip()


def head(worktree: str | os.PathLike[str]) -> str | None:
    return git(worktree, "rev-parse", "--verify", "--quiet", "HEAD") or None


def new_commits(worktree: str | os.PathLike[str], before: str | None, after: str | None, limit: int = 200) -> list[str]:
    """Commits reachable from ``after`` but not ``before``, oldest first (as git sees them).

    With no ``before`` (no commit existed when the run started) every commit
    reachable from ``after`` is new.
    """

    if not after or before == after:
        return []
    spec = f"{before}..{after}" if before else after
    out = git(worktree, "rev-list", "--reverse", f"--max-count={limit}", spec)
    return out.split() if out else []


def branch(worktree: str | os.PathLike[str]) -> str | None:
    """Short name of the checked-out branch; None for a detached HEAD or if git fails."""

    return git(worktree, "symbolic-ref", "--quiet", "--short", "HEAD") or None


def repo_name(worktree: str | os.PathLike[str]) -> str | None:
    """Name of the repository behind a worktree: the directory that holds its common git dir.

    Every linked worktree of one repository gives the same name (no full path).
    """

    common = git(worktree, "rev-parse", "--git-common-dir")
    if not common:
        return None
    path = common if os.path.isabs(common) else os.path.join(os.fspath(worktree), common)
    path = os.path.normpath(path)
    name = os.path.basename(path)
    if name == ".git":
        name = os.path.basename(os.path.dirname(path))
    elif name.endswith(".git"):
        name = name[: -len(".git")]
    return name or None


def resolve_commit(worktree: str | os.PathLike[str], ref: str) -> str | None:
    """Full id of the commit a hex ``ref`` names, or None (non-hex refs are never passed to git)."""

    if not isinstance(ref, str) or not _HEX_REF.match(ref):
        return None
    out = git(worktree, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")
    return out.lower() if out else None


def is_ancestor(worktree: str | os.PathLike[str], ancestor: str, descendant: str) -> bool | None:
    """True if ``ancestor`` is reachable from ``descendant`` (a commit is its own ancestor); None if git cannot tell."""

    result = _run(worktree, ("merge-base", "--is-ancestor", ancestor, descendant), GIT_TIMEOUT_S)
    if result is None or result.returncode not in (0, 1):
        return None
    return result.returncode == 0


def is_meaningful(worktree: str | os.PathLike[str], sha: str) -> bool:
    """True when ``sha`` changes files: its tree differs from its first parent's (or it is a non-empty root).

    An ``--allow-empty`` commit, or a merge that brings nothing in, is not meaningful.
    """

    tree = git(worktree, "rev-parse", "--verify", "--quiet", f"{sha}^{{tree}}")
    if not tree:
        return False
    parent_tree = git(worktree, "rev-parse", "--verify", "--quiet", f"{sha}^1^{{tree}}")
    if parent_tree:
        return tree != parent_tree
    return bool(git(worktree, "ls-tree", "--name-only", tree))


def dirty(worktree: str | os.PathLike[str]) -> bool | None:
    """True when tracked files differ from ``HEAD`` (staged or not); None if git cannot tell."""

    result = _run(worktree, ("status", "--porcelain", "--untracked-files=no"), GIT_TIMEOUT_S)
    if result is None or result.returncode != 0:
        return None
    return bool(result.stdout.strip())


def toplevel(path: str | os.PathLike[str]) -> str | None:
    return git(path, "rev-parse", "--show-toplevel") or None


def inside_work_tree(path: str | os.PathLike[str]) -> bool:
    return git(path, "rev-parse", "--is-inside-work-tree") == "true"


def is_tracked(worktree: str | os.PathLike[str], relpath: str) -> bool:
    return git(worktree, "ls-files", "--error-unmatch", "--", relpath) is not None


def is_ignored(worktree: str | os.PathLike[str], relpath: str) -> bool | None:
    """True/False from ``git check-ignore``; None when git cannot tell."""

    result = _run(worktree, ("check-ignore", "-q", "--", relpath), GIT_TIMEOUT_S)
    if result is None or result.returncode not in (0, 1):
        return None
    return result.returncode == 0


__all__ = [
    "branch",
    "dirty",
    "git",
    "head",
    "inside_work_tree",
    "is_ancestor",
    "is_ignored",
    "is_meaningful",
    "is_tracked",
    "new_commits",
    "repo_name",
    "resolve_commit",
    "toplevel",
]
