"""Auditable as-of FX conversion and explicit corporate-action support limits."""

from bisect import bisect_right
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from crow_trading.backtesting.models import ONE, number
from crow_trading.data.models import DataError, Dataset, utc


@dataclass(frozen=True)
class FXRate:
    available_at: datetime
    eur_per_unit: Decimal


class FXBook:
    def __init__(self, raw: dict[str, Any], max_age_hours: int) -> None:
        if not raw.get("source") or raw.get("base") != "EUR":
            raise DataError("FX snapshot needs source and base=EUR")
        self.source = str(raw["source"])
        self.max_age = timedelta(hours=max_age_hours)
        self.rates: dict[str, tuple[FXRate, ...]] = {}
        for currency, points in raw["rates"].items():
            if currency == "EUR":
                raise DataError(
                    "EUR is the identity currency; do not provide EUR rates"
                )
            rates = tuple(
                FXRate(
                    utc(datetime.fromisoformat(p["available_at"])),
                    number(p["eur_per_unit"]),
                )
                for p in points
            )
            if not rates or any(r.eur_per_unit <= 0 for r in rates):
                raise DataError("FX rates must be positive and nonempty")
            if any(
                b.available_at <= a.available_at
                for a, b in zip(rates, rates[1:], strict=False)
            ):
                raise DataError("FX timestamps must be unique and ordered")
            self.rates[currency] = rates

    def eur_rate(self, currency: str, timestamp: datetime) -> Decimal:
        timestamp = utc(timestamp)
        if currency == "EUR":
            return ONE
        rates = self.rates.get(currency, ())
        index = bisect_right([p.available_at for p in rates], timestamp) - 1
        if index < 0:
            raise DataError(
                f"no FX rate available for {currency} at {timestamp.isoformat()}"
            )
        point = rates[index]
        if timestamp - point.available_at > self.max_age:
            raise DataError(f"stale FX rate for {currency} at {timestamp.isoformat()}")
        return point.eur_per_unit

    def rate(self, source: str, target: str, timestamp: datetime) -> Decimal:
        if source == target:
            return ONE
        return self.eur_rate(source, timestamp) / self.eur_rate(target, timestamp)


def check_actions(dataset: Dataset, raw: dict[str, Any] | None) -> None:
    req = dataset.metadata.request
    if req.kind == "crypto":
        if raw is not None:
            raise DataError("corporate-action snapshots apply to stocks only")
        return
    if req.adjusted:
        raise DataError("adjusted stock prices are unsupported for executable fills")
    if raw is None:
        raise DataError("stocks require a corporate-action coverage snapshot")
    if (
        not raw.get("source")
        or raw.get("symbol") != req.symbol
        or raw.get("start") != str(req.start)
        or raw.get("end") != str(req.end)
        or raw.get("coverage_complete") is not True
    ):
        raise DataError(
            "stock action snapshot must attest complete source/symbol/date coverage"
        )
    if raw.get("prices_unadjusted") is not True:
        raise DataError(
            "stocks require verified unadjusted price basis in the action snapshot"
        )
    if not isinstance(raw.get("events"), list):
        raise DataError("stock action snapshot requires an explicit events list")
    if raw["events"]:
        raise DataError(
            "splits and dividends are unsupported; select a verified no-action window"
        )
