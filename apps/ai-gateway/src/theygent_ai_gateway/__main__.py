"""The gateway's command line: ``theygent-ai-gateway`` (or ``python -m theygent_ai_gateway``) runs
the server; ``theygent-ai-gateway report`` prints the observer report from the local request log.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime
from pathlib import Path

import uvicorn

from theygent_ai_gateway.app import create_app
from theygent_ai_gateway.report import (
    DEFAULT_DIMENSIONS,
    DIMENSIONS,
    parse_when,
    render_json,
    render_text,
    report_from_log,
)
from theygent_ai_gateway.settings import SettingsError, load_settings, state_dir_from

PROG = "theygent-ai-gateway"


def serve() -> None:
    try:
        settings = load_settings(os.environ)
    except SettingsError as exc:
        sys.exit(f"{PROG}: {exc}")
    uvicorn.run(create_app(settings), host=settings.host, port=settings.port)


def _dimensions(value: str) -> list[str]:
    chosen = [d.strip() for d in value.split(",") if d.strip()]
    unknown = [d for d in chosen if d not in DIMENSIONS]
    if unknown or not chosen:
        raise argparse.ArgumentTypeError(
            f"choose from {', '.join(DIMENSIONS)} (comma-separated), got {value!r}"
        )
    return chosen


def _when(value: str) -> datetime:
    try:
        return parse_when(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from None


def report(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog=f"{PROG} report",
        description="What the traffic through this gateway cost, what it would have cost with "
        "routing, and what is wrong with the setup — from the local request log.",
    )
    parser.add_argument(
        "--state-dir",
        help="the gateway's state directory (default: THEYGENT_AI_GATEWAY_STATE_DIR, else "
        "~/.theygent/ai-gateway)",
    )
    parser.add_argument("--since", type=_when, help="30m, 24h, 7d, 2w, or an ISO date")
    parser.add_argument("--until", type=_when, help="30m, 24h, 7d, 2w, or an ISO date")
    parser.add_argument(
        "--by",
        type=_dimensions,
        default=list(DEFAULT_DIMENSIONS),
        help=f"group by: {', '.join(DIMENSIONS)} (default: {','.join(DEFAULT_DIMENSIONS)})",
    )
    parser.add_argument("--top", type=int, default=20, help="rows per group in text output")
    parser.add_argument("--json", action="store_true", help="print JSON instead of text")
    args = parser.parse_args(argv)
    state_dir = Path(args.state_dir).expanduser() if args.state_dir else state_dir_from(os.environ)
    log_dir = state_dir / "requests"
    if not log_dir.is_dir():
        print(f"{PROG}: no request log at {log_dir}", file=sys.stderr)
        return 1
    result = report_from_log(log_dir, since=args.since, until=args.until, dimensions=args.by)
    sys.stdout.write(render_json(result) if args.json else render_text(result, top=args.top))
    return 0


def main(argv: list[str] | None = None) -> None:
    args = sys.argv[1:] if argv is None else argv
    if not args:
        serve()
        return
    if args[0] == "report":
        sys.exit(report(args[1:]))
    sys.exit(f"{PROG}: unknown command {args[0]!r}; run with no arguments to serve, or 'report'")


if __name__ == "__main__":
    main()
