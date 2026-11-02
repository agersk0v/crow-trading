"""Local paper accounts; all controls are CLI-only and never contact brokers."""

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

from crow_trading.data.models import DataError
from crow_trading.paper.config import read_config
from crow_trading.paper.engine import initialize, step
from crow_trading.paper.operations import control, export, inspect
from crow_trading.scanner.state import Busy


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Local paper trading (simulated funds/orders only)"
    )
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("init", "step"):
        command = commands.add_parser(name)
        command.add_argument("config", type=Path)
        command.add_argument(
            "--as-of",
            type=datetime.fromisoformat,
            help="explicit synthetic fixture clock only",
        )
    for name in ("inspect", "export", "stop", "resume", "cancel"):
        command = commands.add_parser(name)
        command.add_argument("--state", type=Path, required=True)
        if name == "inspect":
            command.add_argument(
                "--section",
                choices=(
                    "accounts",
                    "positions",
                    "orders",
                    "fills",
                    "decisions",
                    "events",
                    "valuations",
                    "all",
                ),
                default="accounts",
            )
        elif name == "export":
            command.add_argument("--output", type=Path, required=True)
        elif name in ("stop", "resume"):
            command.add_argument("--account", required=True)
        elif name == "cancel":
            command.add_argument("--order", required=True)
    args = parser.parse_args()
    try:
        if args.command in ("init", "step"):
            config = read_config(args.config)
            if args.as_of is not None and config.mode != "fixture":
                raise DataError(
                    "--as-of is restricted to explicitly synthetic fixture accounts"
                )
            now = args.as_of or datetime.now(UTC)
            result = (
                initialize(config, now) if args.command == "init" else step(config, now)
            )
        elif args.command == "inspect":
            complete = inspect(args.state)
            for table, rows in complete.items():
                if table != "reconciliation_errors":
                    for row in rows:
                        for field in ("payload", "policy_json"):
                            if field in row:
                                row[field] = json.loads(row[field])
            result = (
                complete
                if args.section == "all"
                else {
                    "reconciliation_errors": complete["reconciliation_errors"],
                    args.section: complete[args.section],
                }
            )
        elif args.command == "export":
            result = export(args.state, args.output)
        else:
            result = control(
                args.state,
                args.command,
                getattr(args, "account", None),
                getattr(args, "order", None),
            )
        print(
            json.dumps(
                result, sort_keys=True, indent=2 if args.command == "inspect" else None
            )
        )
        return int(
            result.get("status") == "degraded"
            or bool(result.get("reconciliation_errors"))
        )
    except Busy as exc:
        print(json.dumps({"status": "busy", "error": str(exc)}), file=sys.stderr)
        return 3
    except Exception as exc:
        print(json.dumps({"status": "error", "error": str(exc)}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
