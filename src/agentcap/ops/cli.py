"""``python -m agentcap.ops`` -- command line for the operations tracker.

Exit codes: 0 ok; 1 delivery incomplete (something still pending/failed);
2 refused or invalid (bad config, duplicate dispatch, wrong model, ...);
75 busy (worktree, store or delivery lock held by a live process).
``run`` exits with the model's own exit code (128+N if a signal killed it,
127/126 if it could not be started).  ``hook`` always exits 0.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import uuid
from typing import Any, Callable, Sequence, TextIO

from . import events
from .hooks import MAX_HOOK_INPUT, handle_claude_notification, hooks_status, install_hooks, uninstall_hooks
from .notify import CONFIG_ENV, deliver, requeue_abandoned_claims, resolve_config
from .runner import EXIT_BUSY, EXIT_REFUSED, Classifier, RunRefused, recover_stale_runs, run_task
from .store import NOTIFY_STATES, SIGNAL_KINDS, STATE_DIR_ENV, OpsError, OpsStore, StoreBusy, iso
from .verify import verify_task


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m agentcap.ops",
        description="AgentCap operations tracking: tasks, model runs with evidence, and a notification queue.",
    )
    parser.add_argument(
        "--state-dir",
        help=f"state directory (default: ${STATE_DIR_ENV} or ~/.local/state/agentcap/ops)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    task = sub.add_parser("task", help="record and inspect dispatched tasks")
    task_sub = task.add_subparsers(dest="action", required=True)
    add = task_sub.add_parser("add", help="record a task (an identical re-add is a no-op)")
    add.add_argument("--id", required=True)
    add.add_argument("--title", required=True)
    add.add_argument("--worktree", default=".", help="git worktree the task runs in (default: .)")
    add.add_argument("--depends-on", action="append", default=[], metavar="TASK",
                     help="task that must be verified before this one may run (repeatable)")
    add.add_argument("--project", help="project label carried in notifications")
    add.add_argument("--no-commit", action="store_true",
                     help="verification does not require a meaningful commit (default: it does)")
    show = task_sub.add_parser("show", help="one task with its runs")
    show.add_argument("task_id")
    task_sub.add_parser("list", help="all tasks")
    verify = task_sub.add_parser("verify", help="verify a completed run with independent tests and commits")
    verify.add_argument("task_id")
    verify.add_argument("--commit", action="append", default=[], metavar="SHA",
                        help="commit holding the work (default: the meaningful commits recorded for the task)")
    verify.add_argument("--timeout", type=float, help="stop the test command after this many seconds")
    verify.add_argument("--verified-by", default="coordinator", help="who ran the verification")
    # `--` separates the test command; its own flags are not parsed as ours.
    verify.add_argument("test_command", nargs="*", metavar="COMMAND",
                        help="-- independent test command and arguments")

    model = sub.add_parser("model", help="explicit active model per worktree")
    model_sub = model.add_subparsers(dest="action", required=True)
    model_set = model_sub.add_parser("set", help="make a model the active one (refused mid-run)")
    model_set.add_argument("--worktree", default=".")
    model_set.add_argument("--model", required=True)
    model_set.add_argument("--note")
    model_show = model_sub.add_parser("show", help="active model(s) and history")
    model_show.add_argument("--worktree")

    run = sub.add_parser("run", help="run a model command for a task and record it")
    run.add_argument("--task", required=True)
    run.add_argument("--model", required=True, help="model id; must be the worktree's active model")
    run.add_argument("--title", help="create the task with this title if it does not exist")
    run.add_argument("--worktree", help="worktree for a new task (default: .); must match an existing task")
    run.add_argument("--timeout", type=float, help="stop the model after this many seconds")
    run.add_argument("--quota-exit-code", type=int, action="append", default=[])
    run.add_argument("--permission-exit-code", type=int, action="append", default=[])
    run.add_argument("--quota-pattern", action="append", default=[], help="regex on the output tail (non-zero exits only)")
    run.add_argument("--permission-pattern", action="append", default=[])
    run.add_argument("--stdin-file", help="file fed to the model on stdin (default: /dev/null)")
    run.add_argument("--rerun", action="store_true", help="run a task that already succeeded again")
    # Not dest="command": that is the subcommand's dest, and a subparser's values
    # overwrite the parent namespace (``args.command`` would become this list).
    run.add_argument("model_command", nargs=argparse.REMAINDER, metavar="COMMAND",
                     help="-- model command and arguments")

    signal = sub.add_parser("signal", help="record a quota/permission signal for a run or worktree")
    signal.add_argument("--kind", required=True, choices=SIGNAL_KINDS)
    signal.add_argument("--run")
    signal.add_argument("--worktree")
    signal.add_argument("--source", default="manual")
    signal.add_argument("--detail")
    signal.add_argument("--dedup-key")

    hook = sub.add_parser("hook", help="hook entry points (read JSON on stdin; always exit 0)")
    hook_sub = hook.add_subparsers(dest="action", required=True)
    notification = hook_sub.add_parser("claude-notification", help="Claude Code Notification hook")
    notification.add_argument("--worktree")

    hooks = sub.add_parser("hooks", help="install/uninstall the worktree-scoped Claude Code hook")
    hooks_sub = hooks.add_subparsers(dest="action", required=True)
    for name in ("install", "uninstall", "status"):
        item = hooks_sub.add_parser(name)
        item.add_argument("--worktree", default=".")
        if name != "uninstall":
            item.add_argument("--python", help="interpreter for the hook command (default: this one)")

    notify = sub.add_parser("notify", help="inspect or requeue notifications")
    notify_sub = notify.add_subparsers(dest="action", required=True)
    notify_list = notify_sub.add_parser("list")
    notify_list.add_argument("--state", choices=NOTIFY_STATES)
    notify_retry = notify_sub.add_parser("retry", help="requeue failed notifications")
    notify_retry.add_argument("--id", type=int, action="append", default=[])
    notify_probe = notify_sub.add_parser("probe", help="queue a harmless test notification (endpoint check)")

    send = sub.add_parser(
        "deliver", help="send due notifications to the coordinator's endpoint or existing sender command",
    )
    send.add_argument(
        "--config", help=f"owner-only (0600) endpoint/sender config file (default: ${CONFIG_ENV})",
    )
    send.add_argument("--config-fd", type=int, help="read the endpoint/sender config from this inherited fd")
    send.add_argument("--limit", type=int, default=100)

    recover = sub.add_parser(
        "recover",
        help="mark runs whose runner died as interrupted; requeue notifications a stopped deliverer left sending",
    )
    recover.add_argument("--kill-orphans", action="store_true", help="also stop model processes they left running")

    sub.add_parser("status", help="tasks, running runs, models and the notification queue")

    for item in (show, task_sub.choices["list"], verify, model_show, run, signal, hooks_sub.choices["install"],
                 hooks_sub.choices["uninstall"], hooks_sub.choices["status"], notify_list, notify_retry,
                 notify_probe, send, recover, sub.choices["status"]):
        item.add_argument("--json", action="store_true", help="machine-readable output")
    return parser


def _store(args: argparse.Namespace, **kwargs: Any) -> OpsStore:
    return OpsStore(args.state_dir, **kwargs) if args.state_dir else OpsStore(**kwargs)


def _real_dir(path: str) -> str:
    real = os.path.realpath(path)
    if not os.path.isdir(real):
        raise OpsError(f"{path} is not a directory")
    return real


def _out(stdout: TextIO, args: argparse.Namespace, data: Any, text: str) -> None:
    if getattr(args, "json", False):
        print(json.dumps(data, indent=2, sort_keys=True, default=str), file=stdout)
    else:
        print(text, file=stdout)


# ------------------------------------------------------------ commands
def _task(args, *, stdout, stderr, **_):
    store = _store(args)
    if args.action == "verify":
        command = list(args.test_command)
        if command and command[0] == "--":
            command = command[1:]
        outcome = verify_task(
            store, task_id=args.task_id, argv=command, commits=args.commit,
            timeout_s=args.timeout, verified_by=args.verified_by,
        )
        print(
            f"agentcap-ops: task {outcome.task_id} verification #{outcome.verification_id} "
            f"{outcome.state.upper()}: {outcome.reason}; evidence {outcome.evidence_dir / 'verification.json'}",
            file=stderr,
        )
        if args.json:
            print(json.dumps(outcome.as_dict(), indent=2, sort_keys=True), file=stdout)
        return outcome.cli_exit_code
    if args.action == "add":
        task, created = store.add_task(
            args.id, args.title, _real_dir(args.worktree),
            depends_on=args.depends_on, project=args.project, requires_commit=not args.no_commit,
        )
        _out(stdout, args, task, f"{'added' if created else 'unchanged'}: task {task['id']} "
             f"({task['state']}) in {task['worktree']}")
        return 0
    if args.action == "show":
        task = store.get_task(args.task_id)
        if task is None:
            raise OpsError(f"task {args.task_id} does not exist")
        runs = store.list_runs(task_id=args.task_id)
        verifications = store.list_verifications(task_id=args.task_id)
        commits = store.list_commits(task_id=args.task_id)
        lines = [f"task {task['id']} [{task['state']}] {task['title']} ({task['worktree']})"]
        lines += [f"  run {r['id']} {r['state']} model={r['model']} exit={r['exit_code']} {r['reason'] or ''}"
                  for r in runs]
        lines += [f"  verification #{v['id']} {v['state']}: {v['reason']}" for v in verifications]
        lines += [f"  commit {c['sha'][:12]} ({c['source']})" for c in commits]
        _out(stdout, args, {"task": task, "runs": runs, "verifications": verifications, "commits": commits},
             "\n".join(lines))
        return 0
    tasks = store.list_tasks()
    _out(stdout, args, tasks, "\n".join(f"{t['id']} [{t['state']}] {t['title']}" for t in tasks) or "no tasks")
    return 0


def _model(args, *, stdout, **_):
    store = _store(args)
    if args.action == "set":
        active = store.set_model(_real_dir(args.worktree), args.model, note=args.note)
        _out(stdout, args, active, f"active model for {active['worktree']}: {active['model']} ({active['state']})")
        return 0
    if args.worktree:
        worktree = _real_dir(args.worktree)
        data = {"active": store.get_model(worktree), "history": store.model_history(worktree)}
        active = data["active"]
        text = f"{worktree}: {active['model']} ({active['state']})" if active else f"{worktree}: no active model"
        _out(stdout, args, data, text)
        return 0
    models = store.list_models()
    _out(stdout, args, models,
         "\n".join(f"{m['worktree']}: {m['model']} ({m['state']})" for m in models) or "no active models")
    return 0


def _run(args, *, stdout, stderr, **_):
    command = list(args.model_command)
    if command and command[0] == "--":
        command = command[1:]
    try:
        classifier = Classifier.build(
            quota_exit_codes=args.quota_exit_code,
            permission_exit_codes=args.permission_exit_code,
            quota_patterns=args.quota_pattern,
            permission_patterns=args.permission_pattern,
        )
    except re.error as exc:
        raise RunRefused(f"invalid pattern: {exc}") from None
    store = _store(args)
    task = store.get_task(args.task)
    if task is None:
        if not args.title:
            raise RunRefused(f"task {args.task} does not exist; add it with `task add` or pass --title")
        store.add_task(args.task, args.title, _real_dir(args.worktree or "."))
    elif args.worktree and _real_dir(args.worktree) != task["worktree"]:
        raise RunRefused(f"task {args.task} belongs to {task['worktree']}, not {args.worktree}")
    outcome = run_task(
        store, task_id=args.task, model=args.model, argv=command, classifier=classifier,
        timeout_s=args.timeout, stdin_path=args.stdin_file, rerun=args.rerun,
    )
    print(
        f"agentcap-ops: run {outcome.run_id} {outcome.state.upper()} ({outcome.reason}); "
        f"evidence {outcome.run_dir / 'evidence.json'}",
        file=stderr,
    )
    if args.json:
        print(json.dumps(outcome.as_dict(), indent=2, sort_keys=True), file=stdout)
    return outcome.cli_exit_code


def _signal(args, *, stdout, **_):
    if not args.run and not args.worktree:
        raise OpsError("give --run or --worktree")
    store = _store(args)
    recorded, created = store.record_signal(
        kind=args.kind, source=f"cli:{args.source}", detail=args.detail, run_id=args.run,
        worktree=_real_dir(args.worktree) if args.worktree and not args.run else None,
        dedup_key=args.dedup_key,
    )
    _out(stdout, args, {"signal": recorded, "created": created},
         f"{'recorded' if created else 'duplicate'}: {recorded['kind']} signal #{recorded['id']} "
         f"(seen {recorded['seen_count']}x)")
    return 0


def _hook(args, *, stdin, stderr, **_):
    try:
        source = getattr(stdin, "buffer", stdin)
        raw = source.read(MAX_HOOK_INPUT + 1)
        if isinstance(raw, str):
            raw = raw.encode("utf-8")
        if len(raw) <= MAX_HOOK_INPUT:
            store = _store(args, lock_timeout=5.0)
            handle_claude_notification(store, raw, worktree=args.worktree)
    except Exception as exc:  # a hook must never break the Claude Code session
        try:
            print(f"agentcap-ops hook: {type(exc).__name__}", file=stderr)
        except Exception:
            pass
    return 0


def _hooks(args, *, stdout, **_):
    store = _store(args)  # the hook records into this state dir
    worktree = args.worktree
    if args.action == "install":
        report = install_hooks(worktree, state_dir=store.state_dir, python=args.python)
    elif args.action == "uninstall":
        report = uninstall_hooks(worktree, state_dir=store.state_dir)
    else:
        status = hooks_status(worktree, state_dir=store.state_dir, python=args.python)
        _out(stdout, args, status,
             f"{status['settings_path']}: {'installed' if status['installed'] else 'not installed'}"
             f" (entries {status['entries']}, stale {status['stale_entries']}, tracked {status['tracked']},"
             f" git-ignored {status['git_ignored']})")
        return 0
    _out(stdout, args, report.as_dict(), f"{report.action}: {report.settings_path}")
    return 0


def _notify(args, *, stdout, **_):
    store = _store(args)
    if args.action == "retry":
        count = store.retry_failed(args.id or None)
        _out(stdout, args, {"requeued": count}, f"requeued {count} failed notification(s)")
        return 0
    if args.action == "probe":
        note, _ = store.enqueue(
            dedup_key=f"probe:{uuid.uuid4().hex}", event="probe", payload=events.probe(iso(store.clock())),
        )
        _out(stdout, args, note, f"queued probe notification #{note['id']}; send it with `deliver`")
        return 0
    rows = store.list_notifications(state=args.state)
    lines = [
        f"#{n['id']} {n['state']} {n['event']} task={n['task_id']} attempts={n['attempts']}"
        + (f" last_error={n['last_error']}" if n["last_error"] else "")
        + (f" duplicates={n['duplicates']}" if n["duplicates"] else "")
        for n in rows
    ]
    _out(stdout, args, rows, "\n".join(lines) or "no notifications")
    return 0


def _deliver(args, *, stdout, **_):
    config = resolve_config(path=args.config, fd=args.config_fd)
    store = _store(args)
    report = deliver(store, config, limit=args.limit)
    if report.busy:
        text = "another deliverer is running; nothing sent"
    else:
        text = (f"delivered {report.delivered}, retry later {report.retried}, dead-lettered "
                f"{report.dead_lettered}; queue: {report.pending} pending, {report.failed_total} failed")
    _out(stdout, args, report.as_dict(), text)
    return report.exit_code


def _recover(args, *, stdout, **_):
    store = _store(args)
    actions = recover_stale_runs(store, kill_orphans=args.kill_orphans)
    lines = [f"{a['action']}: run {a['run_id']} (task {a['task_id']})"
             + (" -- model processes still alive" if a["child_alive"] else "") for a in actions]
    requeued = requeue_abandoned_claims(store)  # None: a live deliverer owns its claims
    if requeued:
        actions.append({"action": "notifications_requeued", "count": requeued})
        lines.append(f"notifications_requeued: {requeued} left 'sending' by a stopped deliverer")
    _out(stdout, args, actions, "\n".join(lines) or "nothing to recover")
    return 0


def _status(args, *, stdout, **_):
    summary = _store(args).summary()
    tasks = ", ".join(f"{state} {count}" for state, count in summary["tasks"].items())
    notes = ", ".join(f"{state} {count}" for state, count in summary["notifications"].items())
    lines = [f"state: {summary['state_dir']}", f"tasks: {tasks}"]
    lines += [f"running: run {r['id']} task {r['task_id']} model {r['model']} since {r['started_at']}"
              for r in summary["running_runs"]] or ["running: none"]
    lines += [f"model: {m['worktree']}: {m['model']} ({m['state']})" for m in summary["models"]]
    lines.append(f"notifications: {notes}")
    lines += [f"failed delivery: #{n['id']} {n['event']} task={n['task_id']} attempts={n['attempts']} "
              f"last_error={n['last_error']}" for n in summary["failed_notifications"]]
    _out(stdout, args, summary, "\n".join(lines))
    return 0


_COMMANDS: dict[str, Callable[..., int]] = {
    "task": _task,
    "model": _model,
    "run": _run,
    "signal": _signal,
    "hooks": _hooks,
    "notify": _notify,
    "deliver": _deliver,
    "recover": _recover,
    "status": _status,
}


def main(
    argv: Sequence[str] | None = None,
    *,
    stdin: Any = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> int:
    stdin = stdin if stdin is not None else sys.stdin
    stdout = stdout if stdout is not None else sys.stdout
    stderr = stderr if stderr is not None else sys.stderr
    try:
        args = build_parser().parse_args(list(argv) if argv is not None else None)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else EXIT_REFUSED
    if args.command == "hook":
        return _hook(args, stdin=stdin, stderr=stderr)
    try:
        return _COMMANDS[args.command](args, stdin=stdin, stdout=stdout, stderr=stderr)
    except RunRefused as exc:
        print(f"agentcap-ops: {exc}", file=stderr)
        return exc.exit_code
    except StoreBusy as exc:
        print(f"agentcap-ops: {exc}", file=stderr)
        return EXIT_BUSY
    except (OpsError, ValueError) as exc:
        print(f"agentcap-ops: {exc}", file=stderr)
        return EXIT_REFUSED


__all__ = ["build_parser", "main"]
