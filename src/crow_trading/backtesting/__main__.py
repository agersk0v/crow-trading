"""Freeze an experiment, then run offline without overwriting existing reports."""

import argparse
import json
import sys
from pathlib import Path

from crow_trading.backtesting.experiment import freeze
from crow_trading.backtesting.reports import execute


def main() -> int:
    parser = argparse.ArgumentParser(description="Offline single-asset backtesting")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("freeze", "run"):
        command = commands.add_parser(name)
        command.add_argument("input", type=Path)
        command.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    reserved = False
    try:
        if args.command == "freeze":
            plan = freeze(args.input, args.output)
            print(json.dumps({"status": "frozen", "plan_hash": plan["plan_hash"]}))
        else:
            args.output.mkdir(parents=True, exist_ok=False)
            reserved = True
            result = execute(args.input, args.output)
            (args.output / "status.json").write_text('{"status": "success"}\n')
            print(
                json.dumps(
                    {"status": "success", "plan_hash": result["plan"]["plan_hash"]}
                )
            )
        return 0
    except Exception as exc:
        failure = {"status": "rejected", "error": str(exc)}
        if reserved:
            # No success marker/report is emitted for an unsupported or failed run.
            (args.output / "status.json").write_text(
                json.dumps(failure, sort_keys=True) + "\n"
            )
        print(json.dumps(failure, sort_keys=True), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
