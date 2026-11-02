"""One foreground command; Ctrl+C stops the loop without stopping accounts."""

import argparse
import sys
from pathlib import Path

from crow_trading.paper.config import read_config
from crow_trading.runner.engine import run
from crow_trading.scanner.state import Busy


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run scanner and simulated paper accounts together"
    )
    parser.add_argument("config", type=Path)
    parser.add_argument(
        "--interval",
        type=float,
        default=300,
        help="seconds after each completed cycle (default: 300)",
    )
    args = parser.parse_args()
    try:
        run(
            read_config(args.config),
            args.interval,
            emit=lambda line: print(line, flush=True),
        )
        return 0
    except KeyboardInterrupt:
        print(
            "Runner stopped. Saved accounts and orders resume on the next start.",
            flush=True,
        )
        return 0
    except Busy as exc:
        print(f"Runner busy: {exc}", file=sys.stderr)
        return 3
    except Exception as exc:
        print(f"Runner stopped on error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
