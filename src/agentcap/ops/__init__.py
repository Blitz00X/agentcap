"""AgentCap operations tracking.

A persistent, single-writer record of the coordinator's tasks, the model runs
that work on them, and the notifications those runs produce:

* :mod:`.store` -- SQLite store (tasks, runs, signals, explicit active model,
  notification outbox, delivery attempts) behind one ``flock`` writer lock.
* :mod:`.runner` -- runs one model command per task in its worktree, records
  the kernel-reported exit code plus hashed evidence, queues meaningful commit
  notifications after the model exits, and recovers runs whose runner died.
* :mod:`.verify` -- the coordinator's independent test command and commit checks;
  only a passed verification makes a task ``success``.
* :mod:`.notify` -- loads the coordinator's secret-free sender config at runtime
  (an owner-only file or an inherited fd; never stored) and hands the outbox to
  that existing deterministic sender with retries; failures stay queued.
* :mod:`.hooks` -- a Claude Code ``Notification`` hook installed into one
  worktree's ``.claude/settings.local.json`` without disturbing other hooks.

Command line: ``python -m agentcap.ops --help``.
"""
