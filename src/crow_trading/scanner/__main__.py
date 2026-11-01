"""Run once; an external scheduler invokes this command periodically."""

import argparse
import json
import sys
from pathlib import Path

from crow_trading.scanner.config import read_watchlist
from crow_trading.scanner.engine import scan
from crow_trading.scanner.state import Busy


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Daily watchlist data and signal reports"
    )
    parser.add_argument("watchlist", type=Path)
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="recheck overlap without waiting for a new bar",
    )
    args = parser.parse_args()
    try:
        result = scan(read_watchlist(args.watchlist), refresh=args.refresh)
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
