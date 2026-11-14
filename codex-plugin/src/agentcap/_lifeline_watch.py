#!/usr/bin/env python3
"""Watchdog that SIGKILLs the agent's process group if AgentCap dies.

``PR_SET_PDEATHSIG`` (see :mod:`agentcap._lifeline_exec`) only reaches the
process AgentCap started.  A launcher that spawns the real agent as a child
(Codex's npm wrapper on Node without ``process.execve``) and the agent's tool
processes live in the agent's process group but are not that process, so they
would keep running after AgentCap is SIGKILLed or OOM-killed.

AgentCap holds the write end of this process's stdin.  The kernel closes it
however AgentCap dies, so end-of-file without a ``done`` line means AgentCap
is gone: kill the group.  Protocol, one line each: ``pgid N`` arms, ``done``
disarms.  Standard library only; started with ``python -I``.
"""

from __future__ import annotations

import os
import signal
import sys


def main() -> int:
    pgid: int | None = None
    own = os.getpgrp()
    for raw in sys.stdin.buffer:
        line = raw.strip()
        if line == b"done":
            return 0
        if line.startswith(b"pgid "):
            try:
                value = int(line[5:])
            except ValueError:
                continue
            if value > 1 and value != own:
                pgid = value
    if pgid is not None:
        try:
            os.killpg(pgid, signal.SIGKILL)
        except OSError:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
