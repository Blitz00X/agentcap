"""`agentcap import` and `agentcap report` subcommands."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import budget, history, report


def register(subparsers: argparse._SubParsersAction) -> None:
    imp = subparsers.add_parser(
        "import", help="import local Claude Code / Codex / AgentCap usage into usage.db (read-only)"
    )
    imp.add_argument("--claude-dir", type=Path, help="default ~/.claude/projects")
    imp.add_argument("--codex-dir", type=Path, help="default ~/.codex/sessions")
    imp.add_argument("--ledger", type=Path, help="default AgentCap ledger")
    imp.add_argument("--db", type=Path, help="default $XDG_DATA_HOME/agentcap/usage.db")

    rep = subparsers.add_parser("report", help="token & API-equivalent $ report from usage.db")
    rep.add_argument("--by", choices=report.GROUPS, default="day")
    rep.add_argument("--since", default="30d", help="all, 7d, 24h or YYYY-MM-DD (default 30d)")
    rep.add_argument("--db", type=Path)
    fmt = rep.add_mutually_exclusive_group()
    fmt.add_argument("--json", action="store_true")
    fmt.add_argument("--csv", action="store_true")

    bud = subparsers.add_parser("budget", help="set task/hour/day $ caps and per-job quota % in one place")
    bsub = bud.add_subparsers(dest="budget_action")
    bset = bsub.add_parser("set", help="set or change budget values (others are kept)")
    bset.add_argument("--task-usd", help="per run/session cap in API $ (default for run --cap)")
    bset.add_argument("--session-usd", dest="task_usd_alias",
                      help="alias of --task-usd: one agentcap run is one agent session")
    bset.add_argument("--hour-usd", help="rolling hour cap across runs (Pro)")
    bset.add_argument("--day-usd", help="rolling day cap across runs (Pro)")
    bset.add_argument("--task-quota", help="per job share of subscription quota, e.g. 5%% (Pro stops)")
    bset.add_argument("--window", choices=("week", "5h"), help="quota window for --task-quota")
    bsub.add_parser("show", help="print the current budget")
    bsub.add_parser("clear", help="delete the budget file")


def _do_import(args: argparse.Namespace, store: history.HistoryStore, quiet: bool = False) -> history.ImportStats:
    from .ledger import default_ledger_path

    home = Path.home()
    pricer = history._Pricer()
    stats = history.ImportStats()
    stats.add(history.import_claude(store, getattr(args, "claude_dir", None) or home / ".claude" / "projects", pricer))
    stats.add(history.import_codex(store, getattr(args, "codex_dir", None) or home / ".codex" / "sessions", pricer))
    stats.add(history.import_ledger(store, getattr(args, "ledger", None) or default_ledger_path()))
    if not quiet:
        print(
            f"agentcap import: imported {stats.imported} new, {stats.duplicates} already known,"
            f" {stats.skipped} unreadable line(s) skipped, {stats.unpriced} unpriced"
            f" ({stats.files} file(s)) -> {store.path}"
        )
    return stats


def _budget(args: argparse.Namespace) -> int:
    action = args.budget_action or "show"
    if action == "clear":
        budget.clear()
        print("agentcap: budget cleared.")
        return 0
    if action == "set":
        current = budget.load()
        data = {k: getattr(current, k) for k in ("task_usd", "hour_usd", "day_usd", "task_quota", "window")}
        try:
            task = args.task_usd or args.task_usd_alias
            if task is not None:
                data["task_usd"] = budget.parse_usd(task, "--task-usd")
            if args.hour_usd is not None:
                data["hour_usd"] = budget.parse_usd(args.hour_usd, "--hour-usd")
            if args.day_usd is not None:
                data["day_usd"] = budget.parse_usd(args.day_usd, "--day-usd")
            if args.task_quota is not None:
                data["task_quota"] = budget.parse_pct(args.task_quota, "--task-quota")
            if args.window is not None:
                data["window"] = args.window
        except ValueError as exc:
            print(f"agentcap: {exc}", file=sys.stderr)
            return 2
        path = budget.save(budget.Budget(**data))
        print(f"agentcap: budget saved to {path}")
    for line in budget.describe(budget.load()):
        print(line)
    return 0


def dispatch(args: argparse.Namespace) -> int | None:
    if args.subcommand == "budget":
        return _budget(args)
    if args.subcommand == "import":
        _do_import(args, history.HistoryStore(args.db))
        return 0
    if args.subcommand == "report":
        try:
            since = report.parse_since(args.since)
        except ValueError as exc:
            print(f"agentcap: {exc}", file=sys.stderr)
            return 2
        store = history.HistoryStore(args.db)
        if not store.events():
            _do_import(args, store, quiet=args.json or args.csv)
        rows = report.aggregate(store.events(since), args.by)
        if args.json:
            print(report.render_json(rows, args.by))
        elif args.csv:
            sys.stdout.write(report.render_csv(rows))
        elif not rows:
            print("No usage found. Run `agentcap import` (reads ~/.claude and ~/.codex logs).")
        else:
            print(report.render_text(rows, args.by))
        return 0
    return None
