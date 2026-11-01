"""Create deterministic synthetic examples; these prices are not market history."""

import argparse
import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from crow_trading.data.models import Candle, Dataset, Metadata, Request
from crow_trading.data.normalize import schedule
from crow_trading.data.storage import save


def create(destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=False)
    points = []
    day = date(2025, 1, 1)
    while day <= date(2025, 4, 2):
        points.append(
            {
                "available_at": f"{day}T00:00:00+00:00",
                "eur_per_unit": str(Decimal("0.90") + Decimal(len(points)) / 10000),
            }
        )
        day += timedelta(days=1)
    (destination / "fx.json").write_text(
        json.dumps(
            {
                "base": "EUR",
                "source": "synthetic daily USD/EUR fixture, not market rates",
                "rates": {"USD": points},
            },
            indent=2,
        )
        + "\n"
    )
    for kind in ("crypto", "stock"):
        request = Request(
            "BTC/EUR" if kind == "crypto" else "AAPL",
            date(2025, 1, 2),
            date(2025, 4, 1),
            "EUR" if kind == "crypto" else "USD",
            kind,
            "24/7" if kind == "crypto" else "XNYS",
        )
        expected, timezone = schedule(request)
        candles = []
        for i, (opened, closed) in enumerate(expected.items()):
            price = 100.0 + 2 * (i % 30)
            candles.append(
                Candle(opened, closed, price, price + 1, price - 1, price, 1000.0)
            )
        data = Dataset(
            tuple(candles),
            Metadata(
                request,
                "synthetic",
                "fixture",
                timezone,
                datetime(2025, 6, 1, tzinfo=UTC),
                "deterministic triangular fixture; not real prices",
                "1",
            ),
        )
        save(data, destination / f"{kind}.parquet")
        config = {
            "experiment_id": f"synthetic-{kind}-v1",
            "market_kind": kind,
            "dataset": f"{kind}.parquet",
            "fx_snapshot": "fx.json",
            "strategy": {
                "id": "momentum",
                "parameters": {"entry_lookback": 20, "exit_lookback": 10},
            },
            "settings": {
                "account_currency": "EUR",
                "initial_cash": "1000",
                "target_eur": "50",
                "max_position_eur": "50",
                "max_exposure": "0.5",
                "max_loss": "0.1",
                "max_drawdown": "0.1",
                "delivery_delay_seconds": 0,
                "fx_max_age_hours": 96,
                "costs": {
                    "fee_rate": "0.001",
                    "fixed_fee_quote": "0.10" if kind == "stock" else "0",
                    "spread_bps": "10",
                    "slippage_bps": "5",
                    "quantity_step": "0.001",
                    "minimum_quantity": "0.001",
                    "minimum_notional_quote": "1",
                },
            },
            "windows": [
                {"name": "training", "start": "2025-02-10", "end": "2025-02-24"},
                {"name": "validation", "start": "2025-02-24", "end": "2025-03-10"},
                {"name": "test", "start": "2025-03-10", "end": "2025-03-24"},
            ],
        }
        if kind == "stock":
            config["corporate_actions"] = "actions.json"
            (destination / "actions.json").write_text(
                json.dumps(
                    {
                        "symbol": "AAPL",
                        "start": str(request.start),
                        "end": str(request.end),
                        "source": "synthetic no-action fixture",
                        "coverage_complete": True,
                        "prices_unadjusted": True,
                        "events": [],
                    },
                    indent=2,
                )
                + "\n"
            )
        (destination / f"{kind}.json").write_text(json.dumps(config, indent=2) + "\n")
    print(f"Synthetic fixtures written to {destination}; these are not market results.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    create(parser.parse_args().output)
