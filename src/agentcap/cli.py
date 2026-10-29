"""AgentCap command line entry point."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime
from decimal import Decimal

import httpx
import uvicorn

from . import __version__
from .alerts import dispatcher_from_env
from .ledger import Caps, Ledger
from .license import (
    LicenseManager,
    features,
    status_from_environment,
)
from .pricing import PricingError, load_pricing
from .proxy import (
    DEFAULT_GEMINI_UPSTREAM,
    DEFAULT_OPENAI_UPSTREAM,
    ProxyConfig,
    create_app,
)
from .runner import DEFAULT_CAP, Runner, cap_from_env
from .upstreams import UpstreamConfigError, resolve_upstreams

DEFAULT_SERVE_PORT = 8787

WINDOWS_UNSUPPORTED = (
    "agentcap: Windows is not supported yet (process-group stop is POSIX-only). "
    "Use WSL2, macOS or Linux."
)
PRO_CAP_MESSAGE = (
    "agentcap: --hour-cap/--day-cap require an active Pro license; "
    "command was not started. Run `agentcap license status`, then "
    '`agentcap license activate "YOUR_LICENSE_KEY"`.'
)
LICENSE_USAGE_MESSAGE = (
    'Usage: agentcap license activate "YOUR_LICENSE_KEY" '
    "(copy the key from your Polar purchase / customer portal)"
)
LICENSE_INVALID_MESSAGE = (
    "License key not found or no longer valid. Check the copied key; "
    "if it still fails, email kutaykarahan6@gmail.com."
)


def _windows_unsupported() -> bool:
    return os.name == "nt" or sys.platform.startswith("win")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agentcap",
        description="Local USD spend cap for AI agent runs.",
    )
    parser.add_argument("--version", action="version", version=f"agentcap {__version__}")
    subparsers = parser.add_subparsers(dest="subcommand")

    pricing = subparsers.add_parser("pricing", help="inspect bundled model prices")
    pricing.add_argument("model", nargs="?", help="model id, e.g. openai/gpt-4o-mini")

    license_parser = subparsers.add_parser("license", help="manage the license key")
    license_parser.add_argument(
        "action",
        nargs="?",
        default="status",
        choices=("status", "features", "activate", "deactivate"),
        help="status (default), features, activate <KEY>, or deactivate",
    )
    license_parser.add_argument("key", nargs="?", help="license key for 'activate'")

    run = subparsers.add_parser(
        "run", help="run a command under a spend cap (use -- before the command)"
    )
    run.add_argument(
        "--cap",
        default=None,
        help=f"run cap in USD (default: ${DEFAULT_CAP} or $AGENTCAP_CAP)",
    )
    run.add_argument("--hour-cap", default=None, help="hourly cap in USD (Pro)")
    run.add_argument("--day-cap", default=None, help="daily cap in USD (Pro)")
    run.add_argument(
        "--allow-unmetered",
        action="store_true",
        help="forward endpoints that cannot be metered (unsafe)",
    )
    run.add_argument(
        "--no-clamp",
        dest="clamp_output",
        action="store_false",
        default=True,
        help="never lower a request's output limit to fit the remaining budget",
    )
    run.add_argument("--port", type=int, default=0, help="proxy port (default: random free)")
    run.add_argument(
        "--trust-workspace",
        action="store_true",
        help="for Gemini CLI: trust the current workspace in headless runs",
    )
    run.add_argument("--quiet", action="store_true", help="suppress non-essential output")
    run.add_argument("argv", nargs=argparse.REMAINDER, help="command after --")

    serve = subparsers.add_parser(
        "serve", help="run the proxy in the foreground for other terminals"
    )
    serve.add_argument("--port", type=int, default=DEFAULT_SERVE_PORT)
    serve.add_argument("--cap", default=None, help="run cap in USD")
    serve.add_argument("--allow-unmetered", action="store_true")
    serve.add_argument(
        "--no-clamp",
        dest="clamp_output",
        action="store_false",
        default=True,
        help="never lower a request's output limit to fit the remaining budget",
    )
    serve.add_argument("--quiet", action="store_true")

    status = subparsers.add_parser("status", help="show proxy status or ledger spend")
    status.add_argument("--port", type=int, default=DEFAULT_SERVE_PORT)
    status.add_argument(
        "--db", action="store_true", help="read the local ledger file instead of a proxy"
    )

    subparsers.add_parser(
        "demo", help="run a simulated spend-cap demo against a fake local API"
    )
    return parser


# --------------------------------------------------------------------- pricing
def _pricing(args: argparse.Namespace) -> int:
    store = load_pricing()
    if args.model:
        price = store.get(args.model)
        verified = price.verified_at or ("yes" if price.verified else "no")
        print(f"{price.key} (verified={verified})")
        print(f"  input:         ${price.input_per_mtok}/Mtok")
        print(f"  cached input:  ${price.cached_input_per_mtok}/Mtok")
        print(f"  output:        ${price.output_per_mtok}/Mtok")
        if price.cache_write_per_mtok is not None:
            print(f"  cache write:   ${price.cache_write_per_mtok}/Mtok")
        print(f"  max output:    {price.max_output_tokens} tokens")
        if price.long_context is not None:
            cached = price.long_context.cached_input_per_mtok
            cached_text = "" if cached is None else f", ${cached}/Mtok cached"
            print(
                "  long context:  "
                f">{price.long_context.threshold_tokens} tokens -> "
                f"${price.long_context.input_per_mtok}/Mtok input"
                f"{cached_text}, "
                f"${price.long_context.output_per_mtok}/Mtok output"
            )
        if price.schedule:
            print("  scheduled price changes:")
            for entry in price.schedule:
                print(
                    f"    from {entry.effective_from.isoformat()}: "
                    f"${entry.input_per_mtok}/${entry.cached_input_per_mtok}/"
                    f"${entry.output_per_mtok} per Mtok (in/cached/out)"
                )
        if price.source:
            print(f"  source:        {price.source}")
        return 0

    print(
        f"pricing version {store.version} "
        f"(updated {store.updated}, verified={store.verified})"
    )
    for key in sorted(store.keys()):
        price = store.get(key)
        print(
            f"{key}: ${price.input_per_mtok}/${price.cached_input_per_mtok}/"
            f"${price.output_per_mtok} per Mtok (in/cached/out)"
        )
    return 0


# --------------------------------------------------------------------- license
def _license_manager() -> LicenseManager:
    return LicenseManager()


def _license_persist_error(exc: OSError, path) -> str:
    detail = exc.strerror or str(exc)
    return (
        "agentcap: license validated, but could not save activation to "
        f"{path}: {detail}. Check write permission and free disk space."
    )


def _license(args: argparse.Namespace) -> int:
    manager = _license_manager()
    if args.action == "activate":
        if not args.key:
            print(LICENSE_USAGE_MESSAGE, file=sys.stderr)
            return 2
        try:
            status = manager.activate(args.key)
        except OSError as exc:
            print(_license_persist_error(exc, manager.path), file=sys.stderr)
            return 1
        print(f"tier: {status.tier}")
        print(f"valid: {status.valid}")
        print(f"reason: {status.reason}")
        if (
            not status.valid
            and "could not reach the license server" not in status.reason
        ):
            print(LICENSE_INVALID_MESSAGE, file=sys.stderr)
        return 0 if status.valid else 1

    if args.action == "deactivate":
        try:
            manager.clear()
        except OSError as exc:
            print(_license_persist_error(exc, manager.path), file=sys.stderr)
            return 1
        print("license cache cleared")
        return 0

    status = status_from_environment()
    if args.action == "features":
        print(json.dumps(sorted(features(status.tier)), indent=2))
    else:
        print(f"tier: {status.tier}")
        print(f"valid: {status.valid}")
        print(f"reason: {status.reason}")
        print(f"cache path: {manager.path}")
    return 0


# ------------------------------------------------------------------------- run
def _decimal_cap(raw: str | None, label: str) -> Decimal | None:
    if raw is None:
        return None
    try:
        value = Decimal(str(raw))
    except Exception:
        raise ValueError(f"{label} must be a decimal USD amount") from None
    if not value.is_finite() or value < 0:
        raise ValueError(f"{label} must be a non-negative decimal USD amount")
    return value


def _upstream_overrides() -> dict[str, str] | None:
    """Validate override env vars, print a notice, or return None on error."""

    try:
        overrides = resolve_upstreams()
    except UpstreamConfigError as exc:
        print(f"agentcap: {exc}", file=sys.stderr)
        return None
    for override in overrides:
        print(override.notice(), file=sys.stderr)
    return {override.provider: override.url for override in overrides}


def _run(args: argparse.Namespace) -> int:
    if _windows_unsupported():
        print(WINDOWS_UNSUPPORTED, file=sys.stderr)
        return 2
    argv = list(args.argv)
    if argv and argv[0] == "--":
        argv = argv[1:]
    if not argv:
        print(
            "agentcap: run needs a command, e.g. agentcap run --cap 0.50 -- gemini ...",
            file=sys.stderr,
        )
        return 2

    try:
        cap = _decimal_cap(args.cap, "--cap")
        hour_cap = _decimal_cap(args.hour_cap, "--hour-cap")
        day_cap = _decimal_cap(args.day_cap, "--day-cap")
    except ValueError as exc:
        print(f"agentcap: {exc}", file=sys.stderr)
        return 2
    if cap is None:
        cap = cap_from_env()

    overrides = _upstream_overrides()
    if overrides is None:
        return 2

    active_features = set(features(status_from_environment().tier))
    if (
        (hour_cap is not None and "hour_cap" not in active_features)
        or (day_cap is not None and "day_cap" not in active_features)
    ):
        print(PRO_CAP_MESSAGE, file=sys.stderr)
        return 2

    try:
        runner = Runner(
            argv,
            cap=cap,
            hour_cap=hour_cap,
            day_cap=day_cap,
            allow_unmetered=args.allow_unmetered,
            clamp_output=args.clamp_output,
            port=args.port,
            features=active_features,
            quiet=args.quiet,
            openai_upstream=overrides.get("openai"),
            gemini_upstream=overrides.get("gemini"),
            trust_workspace=args.trust_workspace,
        )
        return runner.run()
    except OSError as exc:
        print(
            f"agentcap: could not start the command ({exc.strerror or exc})",
            file=sys.stderr,
        )
        return 127
    except (ValueError, RuntimeError, PricingError) as exc:
        print(f"agentcap: {exc}", file=sys.stderr)
        return 2


# ----------------------------------------------------------------------- serve
def _serve(args: argparse.Namespace) -> int:
    try:
        cap = _decimal_cap(args.cap, "--cap")
    except ValueError as exc:
        print(f"agentcap: {exc}", file=sys.stderr)
        return 2
    if cap is None:
        cap = cap_from_env()

    overrides = _upstream_overrides()
    if overrides is None:
        return 2

    active_features = set(features(status_from_environment().tier))
    run_id = f"serve-{int(time.time())}"
    alerts = dispatcher_from_env(features=active_features)
    config = ProxyConfig(
        run_id=run_id,
        caps=Caps(run_usd=cap),
        features=active_features,
        allow_unmetered=args.allow_unmetered,
        clamp_output=args.clamp_output,
        openai_upstream=overrides.get("openai") or DEFAULT_OPENAI_UPSTREAM,
        gemini_upstream=overrides.get("gemini") or DEFAULT_GEMINI_UPSTREAM,
        on_cap_exceeded=lambda payload: None,
        on_loop_detected=lambda payload: None,
    )
    app = create_app(config, Ledger(), events=alerts.emit)
    base = f"http://127.0.0.1:{args.port}"
    if not args.quiet:
        print("# Route API calls through AgentCap:")
        print(f"export OPENAI_BASE_URL={base}/openai/v1")
        print(f"export GOOGLE_GEMINI_BASE_URL={base}/gemini")
        print(f"export GEMINI_API_BASE_URL={base}/gemini")
        print(f"# run id: {run_id}, cap: ${cap:.2f}")
        sys.stdout.flush()
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="info")
    return 0


# ------------------------------------------------------------------------ demo
def _demo(_args: argparse.Namespace) -> int:
    if _windows_unsupported():
        print(WINDOWS_UNSUPPORTED, file=sys.stderr)
        return 2
    from .demo import run_demo

    return run_demo()


# ---------------------------------------------------------------------- status
def _status(args: argparse.Namespace) -> int:
    if args.db:
        ledger = Ledger()
        now = datetime.now().astimezone()
        midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
        seconds_today = max((now - midnight).total_seconds(), 1.0)
        try:
            today = ledger.window_spent(seconds_today)
            last_hour = ledger.window_spent(3600.0)
        except Exception as exc:
            print(f"agentcap: cannot read ledger: {exc}", file=sys.stderr)
            return 1
        print(f"db: {ledger.path}")
        print(f"today: ${today:.4f}")
        print(f"last hour: ${last_hour:.4f}")
        return 0

    url = f"http://127.0.0.1:{args.port}/_agentcap/status"
    try:
        response = httpx.get(url, timeout=5.0, trust_env=False)
        response.raise_for_status()
        payload = response.json()
    except Exception as exc:
        print(
            f"agentcap: cannot read status from {url} ({type(exc).__name__})",
            file=sys.stderr,
        )
        return 1
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.subcommand == "pricing":
        try:
            return _pricing(args)
        except PricingError as exc:
            print(f"agentcap: {exc}", file=sys.stderr)
            return 2
    if args.subcommand == "license":
        return _license(args)
    if args.subcommand == "run":
        return _run(args)
    if args.subcommand == "serve":
        return _serve(args)
    if args.subcommand == "status":
        return _status(args)
    if args.subcommand == "demo":
        return _demo(args)
    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
