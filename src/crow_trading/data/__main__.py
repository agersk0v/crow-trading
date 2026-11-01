"""Run with python -m crow_trading.data; no servers or credentials required."""

import argparse
import json
import sys
from datetime import date
from pathlib import Path
from typing import Literal, cast

from crow_trading.data.archive import import_archive
from crow_trading.data.crypto import fetch_crypto
from crow_trading.data.models import DataError, Request
from crow_trading.data.stocks import fetch_stock
from crow_trading.data.storage import fingerprint, load, metadata_dict, save


def main() -> int:
    parser = argparse.ArgumentParser(description="Daily historical research datasets")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("fetch-crypto", "fetch-stock", "import-archive"):
        command = commands.add_parser(name)
        command.add_argument("--symbol", required=True)
        command.add_argument("--start", type=date.fromisoformat, required=True)
        command.add_argument("--end", type=date.fromisoformat, required=True)
        command.add_argument("--currency", required=True)
        command.add_argument("--output", type=Path, required=True)
        if name != "fetch-crypto":
            command.add_argument("--calendar", default="XNYS")
            command.add_argument("--adjusted", action="store_true")
        if name != "fetch-stock":
            command.add_argument("--venue", required=True)
        if name == "import-archive":
            command.add_argument("--kind", choices=("crypto", "stock"), required=True)
            command.add_argument("--input", type=Path, required=True)
            command.add_argument("--provenance", required=True)
    inspect = commands.add_parser("inspect")
    inspect.add_argument("path", type=Path)
    args = parser.parse_args()
    try:
        if args.command == "inspect":
            dataset = load(args.path)
        else:
            kind = (
                args.kind
                if args.command == "import-archive"
                else ("crypto" if args.command == "fetch-crypto" else "stock")
            )
            request = Request(
                args.symbol,
                args.start,
                args.end,
                args.currency,
                cast(Literal["crypto", "stock"], kind),
                "24/7" if kind == "crypto" else args.calendar,
                adjusted=getattr(args, "adjusted", False),
            )
            if args.command == "fetch-crypto":
                dataset = fetch_crypto(request, args.venue)
            elif args.command == "fetch-stock":
                dataset = fetch_stock(request)
            else:
                dataset = import_archive(
                    args.input, request, venue=args.venue, provenance=args.provenance
                )
            save(dataset, args.output)
            dataset = load(args.output)
        print(
            json.dumps(
                {**metadata_dict(dataset), "fingerprint": fingerprint(dataset)},
                indent=2,
            )
        )
        return 0
    except (DataError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
