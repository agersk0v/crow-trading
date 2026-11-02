"""Run one scan or explicitly export committed scanner reports."""

import argparse
import json
import sys
from dataclasses import replace
from pathlib import Path

from crow_trading.scanner.config import read_watchlist
from crow_trading.scanner.engine import scan
from crow_trading.scanner.export import export_reports
from crow_trading.scanner.state import Busy


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Daily watchlist scans; use 'export --state ROOT --output NEWDIR' "
            "for report files"
        )
    )
    exporting = len(sys.argv) > 1 and sys.argv[1] == "export"
    if exporting:
        parser.add_argument("command", choices=("export",))
        parser.add_argument("--state", type=Path, required=True)
        parser.add_argument("--output", type=Path, required=True)
    else:
        parser.add_argument("watchlist", type=Path)
        parser.add_argument(
            "--refresh",
            action="store_true",
            help="recheck overlap without waiting for a new bar",
        )
        parser.add_argument(
            "--export-reports",
            action="store_true",
            help="opt in to automatic JSON/Markdown files and restart repair",
        )
    args = parser.parse_args()
    try:
        if exporting:
            print(json.dumps(export_reports(args.state, args.output), sort_keys=True))
            return 0
        config = read_watchlist(args.watchlist)
        if args.export_reports:
            config = replace(config, export_reports=True)
        result = scan(config, refresh=args.refresh)
        print(
            json.dumps(
                {
                    "status": result.report["status"]
                    if result.report
                    else "no_changes",
                    "as_of": result.as_of.isoformat(),
                    "run_id": result.report["run_id"] if result.report else None,
                    "new_signals": len(result.report["signals"])
                    if result.report
                    else 0,
                    "fresh_entries": result.report["fresh_entries"]
                    if result.report
                    else [],
                    "instruments": result.instruments,
                    "report_files": result.report_files,
                    "failure_summary": result.report.get("failure_summary")
                    if result.report
                    else None,
                },
                sort_keys=True,
            )
        )
        return result.exit_code
    except Busy as exc:
        print(json.dumps({"status": "busy", "error": str(exc)}), file=sys.stderr)
        return 3
    except Exception as exc:
        print(json.dumps({"status": "error", "error": str(exc)}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
