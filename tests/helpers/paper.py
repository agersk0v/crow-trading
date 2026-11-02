"""Reusable synthetic market and account harness for offline tests."""

import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from crow_trading.backtesting.models import Costs, Settings
from crow_trading.data.models import Candle, Dataset, Metadata, Request
from crow_trading.data.normalize import schedule
from crow_trading.paper.config import Account, Asset, Config
from crow_trading.paper.engine import step
from crow_trading.paper.state import State
from crow_trading.scanner.config import Instrument, Watchlist, definition
from crow_trading.scanner.engine import scan
from crow_trading.strategies import MeanReversion, Momentum

D = Decimal
T0 = datetime(2025, 1, 25, 0, 16, tzinfo=UTC)


class Harness:
    def __init__(
        self, root: Path, count: int = 1, *, costs: Costs | None = None
    ) -> None:
        root.mkdir(parents=True, exist_ok=True)
        self.root = root
        self.costs = costs or Costs(D("0.01"), D(1), D(0), D(0), D(1), D(0), D(0))
        self.items = tuple(
            Instrument(
                f"asset{i}",
                "crypto",
                "snapshot",
                "synthetic",
                f"ASSET{i}/EUR",
                "EUR",
                "24/7",
                False,
                6,
                3,
                90,
                900,
                64800,
                (Momentum(2, 2), MeanReversion(2)),
                root / f"asset{i}.parquet",
            )
            for i in range(count)
        )
        self.watchlist = Watchlist(root / "scanner", self.items, 1, 0)
        self.prices: dict[date, tuple[float, float]] = {
            date(2025, 1, 24): (11, 11),
            date(2025, 1, 25): (12, 12),
            date(2025, 1, 26): (10, 10),
            date(2025, 1, 27): (13, 13),
            date(2025, 1, 28): (15, 9),
        }
        self.fx = root / "fx.json"
        self.fx.write_text(
            json.dumps({"base": "EUR", "source": "identity fixture", "rates": {}})
        )
        settings = Settings(
            "EUR", D(1000), D(50), D(50), D(1), D(1), D(1), 0, 96, self.costs
        )
        self.account = Account(
            "momentum",
            definition(Momentum(2, 2)),
            settings,
            10,
            345600,
            64800,
            tuple(Asset(i, self.costs) for i in self.items),
        )
        self.config = Config(
            root / "paper", self.watchlist, "fixture", self.fx, (self.account,)
        )

    def fetch(self, item: Instrument, request: Request, now: datetime) -> Dataset:
        expected, timezone = schedule(request)
        candles = []
        for opened, closed in expected.items():
            price, close = self.prices.get(opened.date(), (10.0, 10.0))
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
        return Dataset(
            tuple(candles),
            Metadata(
                request,
                "synthetic",
                item.venue,
                timezone,
                now,
                "hand-defined paper fixture",
                "1",
            ),
        )

    def scan(self, day: int = 0) -> None:
        result = scan(
            self.watchlist, clock=lambda: T0 + timedelta(days=day), fetcher=self.fetch
        )
        assert result.exit_code == 0

    def step(self, day: int = 0) -> dict[str, Any]:
        return step(self.config, T0 + timedelta(days=day))

    def rows(self, table: str) -> list[dict[str, Any]]:
        assert table in (
            "accounts",
            "orders",
            "fills",
            "positions",
            "decisions",
            "events",
            "valuations",
        )
        with State(self.config.state_dir) as state:
            return state.rows(f"SELECT * FROM {table} ORDER BY rowid")
