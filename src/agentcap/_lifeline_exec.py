#!/usr/bin/env python3
"""Tiny exec shim that arms Linux ``PR_SET_PDEATHSIG`` before exec.

AgentCap spawns this file (not the agent directly) so the guarded process is
killed by the kernel if AgentCap itself dies, including a SIGKILL where no
Python cleanup can run.  The shim only uses the standard library, then
``execvp``s the real command so its PID, process group and environment are
unchanged.

Usage: ``python _lifeline_exec.py --parent PID [--errfd FD] -- COMMAND [ARGS...]``

With ``--errfd`` the shim reports an ``execvp`` failure as its errno on that
close-on-exec pipe instead of printing it, so AgentCap can raise the same
start failure ``subprocess.Popen`` raises for a command that cannot run.
"""

from __future__ import annotations

import ctypes
import os
import signal
import sys

_PR_SET_PDEATHSIG = 1


def _arm_parent_death_signal() -> bool:
    """Ask Linux to SIGKILL this process when its parent dies."""

    try:
        libc = ctypes.CDLL(None, use_errno=True)
        return libc.prctl(_PR_SET_PDEATHSIG, signal.SIGKILL, 0, 0, 0) == 0
    except (OSError, AttributeError):
        return False


def _parse(argv: list[str]) -> tuple[int, int | None, list[str]] | None:
    args = argv[1:]
    try:
        sep = args.index("--")
    except ValueError:
        return None
    options, command = args[:sep], args[sep + 1:]
    if not command or len(options) not in (2, 4) or options[0] != "--parent":
        return None
    if len(options) == 4 and options[2] != "--errfd":
        return None
    try:
        parent = int(options[1])
        errfd = int(options[3]) if len(options) == 4 else None
    except ValueError:
        return None
    return parent, errfd, command


def main(argv: list[str]) -> int:
    parsed = _parse(argv)
    if parsed is None:
        print("lifeline: bad arguments", file=sys.stderr)
        return 2
    parent, errfd, command = parsed
    if errfd is not None:
        try:
            os.set_inheritable(errfd, False)  # a successful exec closes it
        except OSError:
            print("lifeline: bad error pipe", file=sys.stderr)
            return 2

    if not _arm_parent_death_signal():
        print("lifeline: could not arm PR_SET_PDEATHSIG; refusing to run unguarded",
              file=sys.stderr)
        return 5
    # The parent may have died between fork/exec and prctl.  In that case the
    # kernel had nobody to signal, so check the reparented pid explicitly.
    if os.getppid() != parent:
        return 137
    try:
        os.execvp(command[0], command)
    except OSError as exc:
        if errfd is not None:
            try:
                os.write(errfd, str(exc.errno or 0).encode("ascii"))
            except OSError:
                pass
        else:
            print(f"lifeline: cannot exec {command[0]!r}: {exc.strerror or exc}", file=sys.stderr)
        return 127
    return 0  # pragma: no cover - execvp never returns


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
