"""`agentcap import` and `agentcap report` subcommands."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import budget, history, report


PRO_VIEWS = ("forecast", "calibration", "savings", "receipts")
PRO_ANALYTICS_MESSAGE = (
    "agentcap: `report {view}` is AgentCap Pro (forecast, quota calibration, cache waste "
    "& plan breakeven, receipts/CSV). Free keeps `agentcap report` by day/session/model."
)


def register(subparsers: argparse._SubParsersAction) -> None:
    imp = subparsers.add_parser(
        "import", help="import local Claude Code / Codex / AgentCap usage into usage.db (read-only)"
    )
    imp.add_argument("--claude-dir", type=Path, help="default ~/.claude/projects")
    imp.add_argument("--codex-dir", type=Path, help="default ~/.codex/sessions")
    imp.add_argument("--ledger", type=Path, help="default AgentCap ledger")
    imp.add_argument("--db", type=Path, help="default $XDG_DATA_HOME/agentcap/usage.db")

    rep = subparsers.add_parser("report", help="token & API-equivalent $ report from usage.db")
    rep.add_argument("view", nargs="?", default="table", choices=PRO_VIEWS + ("table",),
                     help="table (Free) or Pro: forecast, calibration, savings, receipts")
    rep.add_argument("--source", choices=("claude", "codex", "all"), default="all")
    rep.add_argument("--window", choices=("week", "5h", "all"), default="all")
    rep.add_argument("--no-import", action="store_true", help="do not refresh usage.db first")
    rep.add_argument("--by", choices=report.GROUPS, default="day")
    rep.add_argument("--since", default="30d", help="all, 7d, 24h or YYYY-MM-DD (default 30d)")
    rep.add_argument("--db", type=Path)
    fmt = rep.add_mutually_exclusive_group()
    fmt.add_argument("--json", action="store_true")
    fmt.add_argument("--csv", action="store_true")

    wst = subparsers.add_parser(
        "waste", help="subagent/Opus share, cache misses, tool loops and error streaks (Free, read-only)"
    )
    wst.add_argument("--since", default="30d", help="all, 7d, 24h or YYYY-MM-DD (default 30d)")
    wst.add_argument("--claude-dir", type=Path, help="default ~/.claude/projects")
    wst.add_argument("--codex-dir", type=Path, help="default ~/.codex/sessions")
    wst.add_argument("--top", type=int, default=3)
    wst.add_argument("--json", action="store_true")

    bud = subparsers.add_parser("budget", help="set task/hour/day $ caps and per-job quota %% in one place")
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
    from .quota import state_root

    stats.skipped += history.import_claude_quota(store, state_root()).skipped
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


def _pro_view(args: argparse.Namespace, store: history.HistoryStore, since: float | None) -> int:
    import time

    from . import analytics
    from .cli import PRO_HINT
    from .license import features, status_from_environment

    if "usage_analytics" not in features(status_from_environment().tier):
        print(PRO_ANALYTICS_MESSAGE.format(view=args.view), file=sys.stderr)
        print(PRO_HINT, file=sys.stderr)
        return 2
    if not args.no_import:
        _do_import(args, store, quiet=True)
    sources = ("claude", "codex") if args.source == "all" else (args.source,)
    windows = ("5h", "week") if args.window == "all" else (args.window,)
    now = time.time()
    if args.view == "forecast":
        items = [f for s in sources for w in windows
                 if (f := analytics.forecast(store, s, w, now=now)) is not None]
        print(analytics.render_forecast(items, now=now))
        return 0
    if args.view == "calibration":
        blocks = []
        for s in sources:
            for w in windows:
                rows = analytics.calibration(store, s, w)
                if rows or (args.source != "all" and args.window != "all"):
                    blocks.append(analytics.render_calibration(rows, s, w))
        print("\n\n".join(blocks) if blocks else analytics.render_calibration([], "claude/codex", "quota"))
        return 0
    events = [e for e in store.events(since) if e["source"] in sources or args.source == "all"]
    if args.view == "savings":
        days = (now - since) / 86400 if since else max(
            (now - events[0]["ts"]) / 86400 if events else 1.0, 1.0)
        print(analytics.render_savings(events, days=days))
        return 0
    # receipts
    if args.by in ("day", "model", "source"):
        args.by = "session"
    if args.csv:
        sys.stdout.write(analytics.receipts_csv(events, args.by))
        return 0
    from .runner import default_receipt_dir

    rows = report.aggregate(events, args.by)
    print(report.render_text(rows, args.by) if rows else "No usage yet.")
    jobs = analytics.job_receipts(default_receipt_dir())
    if jobs:
        print("\nAgentCap job receipts:")
        for j in jobs[-20:]:
            print(f"  {j['started_at'] or '-':<26} {str(j['job'])[:12]:<12} ${j['spent_usd']} / ${j['cap_usd']}"
                  f"  {j['reason']}  {(j['command'] or '')[:40]}")
    return 0


def dispatch(args: argparse.Namespace) -> int | None:
    if args.subcommand == "budget":
        return _budget(args)
    if args.subcommand == "waste":
        from . import waste
        try:
            since = report.parse_since(args.since)
        except ValueError as exc:
            print(f"agentcap: {exc}", file=sys.stderr)
            return 2
        rep = waste.scan(since=since, claude_dir=args.claude_dir, codex_dir=args.codex_dir)
        period = "all time" if since is None else f"since {args.since}"
        print(waste.render_json(rep) if args.json else waste.render(rep, period, args.top))
        return 0
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
        if args.view != "table":
            return _pro_view(args, store, since)
        if not store.events() or args.by == "agent":
            _do_import(args, store, quiet=True if args.by == "agent" else (args.json or args.csv))
        if args.by == "agent":
            tree = report.agent_tree(store.events(since))
            if args.json:
                print(report.render_agent_json(tree))
            elif not tree:
                print("No usage found. Run `agentcap import` (reads ~/.claude and ~/.codex logs).")
            else:
                print(report.render_agent_tree(tree))
            return 0
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
