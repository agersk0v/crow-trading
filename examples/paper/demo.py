"""End-to-end synthetic account, including restart, simulated buy/sell, and export."""

import argparse
import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from crow_trading.data.models import Candle, Dataset, Metadata
from crow_trading.data.normalize import schedule
from crow_trading.data.storage import save
from crow_trading.paper.config import read_config
from crow_trading.paper.engine import initialize, step
from crow_trading.paper.operations import export
from crow_trading.scanner.config import read_watchlist
from crow_trading.scanner.engine import scan
from crow_trading.scanner.timing import due_sessions, request_window


def demo(destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=False)
    paper = json.loads(Path(__file__).with_name("paper.json").read_text())
    paper.update(mode="fixture", state_dir="paper-state", watchlist="watchlist.json")
    account = paper["accounts"][0]
    account["name"] = "demo-momentum"
    account["strategy"]["parameters"] = {"entry_lookback": 2, "exit_lookback": 2}
    costs = {
        "fee_rate": "0.01",
        "fixed_fee_quote": "1",
        "spread_bps": "0",
        "slippage_bps": "0",
        "quantity_step": "1",
        "minimum_quantity": "0",
        "minimum_notional_quote": "0",
    }
    account["settings"].update(
        costs=costs, max_exposure="1", max_loss="1", max_drawdown="1"
    )
    account["assets"] = [{"name": "demo", "costs": costs}]
    paper["accounts"] = [account]
    config_path = destination / "paper.json"
    config_path.write_text(json.dumps(paper, indent=2) + "\n")
    (destination / "eur-identity.json").write_bytes(
        Path(__file__).with_name("eur-identity.json").read_bytes()
    )
    item = {
        "name": "demo",
        "kind": "crypto",
        "provider": "snapshot",
        "venue": "synthetic",
        "symbol": "DEMO/EUR",
        "currency": "EUR",
        "calendar": "24/7",
        "adjusted": False,
        "history_bars": 6,
        "overlap_bars": 3,
        "max_catchup_bars": 10,
        "finalization_delay_seconds": 900,
        "max_signal_age_seconds": 64800,
        "strategies": [account["strategy"]],
        "snapshot": "prices-0.parquet",
    }
    watchlist_path = destination / "watchlist.json"
    prices = {
        date(2025, 1, 24): (11.0, 11.0),
        date(2025, 1, 25): (12.0, 12.0),
        date(2025, 1, 26): (10.0, 10.0),
        date(2025, 1, 27): (13.0, 13.0),
        date(2025, 1, 28): (15.0, 9.0),
    }
    for day in range(5):
        now = datetime(2025, 1, 25, 0, 16, tzinfo=UTC) + timedelta(days=day)
        item["snapshot"] = f"prices-{day}.parquet"
        raw = {
            "schema_version": 1,
            "state_dir": "scanner-state",
            "attempts": 1,
            "provider_interval_seconds": 0,
            "instruments": [item],
        }
        watchlist_path.write_text(json.dumps(raw, indent=2) + "\n")
        watchlist = read_watchlist(watchlist_path)
        instrument = watchlist.instruments[0]
        request = request_window(instrument, due_sessions(instrument, now), None, None)
        expected, timezone = schedule(request)
        candles = []
        for opened, closed in expected.items():
            price, close = prices.get(opened.date(), (10.0, 10.0))
            candles.append(
                Candle(
                    opened,
                    closed,
                    price,
                    max(price, close),
                    min(price, close),
                    close,
                    100.0,
                )
            )
        data = Dataset(
            tuple(candles),
            Metadata(
                request,
                "synthetic",
                "synthetic",
                timezone,
                now,
                "constructed paper demo; not market data",
                "1",
            ),
        )
        assert instrument.snapshot is not None
        save(data, instrument.snapshot)
        assert scan(watchlist, clock=lambda instant=now: instant).exit_code == 0
        config = read_config(config_path)
        if day == 0:
            initialize(config, now)
        # Each call opens/closes SQLite, exercising ordinary restart behavior.
        result = step(config, now)
        assert result["status"] == "success", result
        print(json.dumps(result, sort_keys=True))
    print(json.dumps(export(config.state_dir, destination / "report"), sort_keys=True))
    print("Synthetic demo only: one buy and one sell; no network or broker calls.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    demo(parser.parse_args().output)
