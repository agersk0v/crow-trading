"""Build a clock-relative offline watchlist with explicitly synthetic snapshots."""

import argparse
import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from crow_trading.data.models import Candle, Dataset, Metadata
from crow_trading.data.normalize import schedule
from crow_trading.data.storage import save
from crow_trading.scanner.config import read_watchlist
from crow_trading.scanner.timing import due_sessions, request_window


def create(destination: Path) -> None:
    now = datetime.now(UTC)
    original = json.loads(Path(__file__).with_name("watchlist.json").read_text())
    original["state_dir"] = "state"
    original["export_reports"] = True
    original["provider_interval_seconds"] = 0
    for raw in original["instruments"]:
        raw["provider"] = "snapshot"
        raw["venue"] = "synthetic-fixture"
        raw["snapshot"] = raw["name"] + ".parquet"
    destination.mkdir(parents=True, exist_ok=False)
    config_path = destination / "watchlist.json"
    config_path.write_text(json.dumps(original, sort_keys=True, indent=2) + "\n")
    config = read_watchlist(config_path)
    for item in config.instruments:
        due = due_sessions(replace(item, finalization_delay_seconds=0), now)
        request = request_window(item, due, None, None)
        expected, timezone = schedule(request)
        candles = tuple(
            Candle(
                opened,
                closed,
                100.0 + 2 * i,
                101.0 + 2 * i,
                99.0 + 2 * i,
                100.0 + 2 * i,
                1000.0,
            )
            for i, (opened, closed) in enumerate(expected.items())
        )
        data = Dataset(
            candles,
            Metadata(
                request,
                "synthetic",
                item.venue,
                timezone,
                now,
                "constructed prices; not investment-performance evidence",
                "1",
            ),
        )
        assert item.snapshot is not None
        save(data, item.snapshot)
    print(
        f"Offline fixtures: {config_path}. "
        "Signals are labelled fixture, never live candidates."
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    create(parser.parse_args().output)
