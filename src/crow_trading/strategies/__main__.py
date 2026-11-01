"""Offline signal export: python -m crow_trading.strategies DATASET --strategy ..."""

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

from crow_trading.data.models import DataError
from crow_trading.data.storage import load
from crow_trading.strategies import MeanReversion, Momentum, Strategy
from crow_trading.strategies.runner import report


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Evaluate daily strategy signal intents"
    )
    parser.add_argument("dataset", type=Path)
    parser.add_argument(
        "--strategy", choices=("momentum", "mean-reversion"), required=True
    )
    parser.add_argument("--mode", choices=("replay", "latest"), default="replay")
    parser.add_argument("--as-of", type=datetime.fromisoformat)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--entry-lookback", type=int)
    parser.add_argument("--exit-lookback", type=int)
    parser.add_argument("--lookback", type=int)
    parser.add_argument("--entry-deviation", type=float)
    args = parser.parse_args()
    try:
        strategy: Strategy
        if args.strategy == "momentum":
            if args.lookback is not None or args.entry_deviation is not None:
                parser.error("--lookback and --entry-deviation require mean-reversion")
            strategy = Momentum(
                entry_lookback=20
                if args.entry_lookback is None
                else args.entry_lookback,
                exit_lookback=10 if args.exit_lookback is None else args.exit_lookback,
            )
        else:
            if args.entry_lookback is not None or args.exit_lookback is not None:
                parser.error("--entry-lookback and --exit-lookback require momentum")
            strategy = MeanReversion(
                lookback=20 if args.lookback is None else args.lookback,
                entry_deviation=0.05
                if args.entry_deviation is None
                else args.entry_deviation,
            )
        output = report(load(args.dataset), strategy, mode=args.mode, as_of=args.as_of)
        text = json.dumps(output, sort_keys=True, indent=2, allow_nan=False) + "\n"
        if args.output is not None:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            with args.output.open("x", encoding="utf-8", newline="\n") as stream:
                stream.write(text)
        else:
            print(text, end="")
        return 0
    except (DataError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
