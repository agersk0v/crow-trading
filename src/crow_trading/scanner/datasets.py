"""Validated overlap merges create lineage without mutating research caches."""

import json
from dataclasses import replace
from datetime import datetime

from crow_trading.data.models import Candle, DataError, Dataset
from crow_trading.data.normalize import validate
from crow_trading.data.storage import fingerprint
from crow_trading.scanner.config import Instrument, digest


def check_source(data: Dataset, item: Instrument, now: datetime) -> None:
    validate(data, as_of=now)
    request = data.metadata.request
    if (
        request.symbol != item.symbol
        or request.kind != item.kind
        or request.currency != item.currency
        or request.calendar != item.calendar
        or request.adjusted != item.adjusted
    ):
        raise DataError("provider returned a different market identity")
    if item.provider == "ccxt" and (
        data.metadata.source != "ccxt" or data.metadata.venue != item.venue
    ):
        raise DataError("CCXT source/venue mismatch")
    if item.provider == "yfinance" and data.metadata.source != "yfinance":
        raise DataError("stock source mismatch")
    if item.provider == "snapshot" and data.metadata.venue != item.venue:
        raise DataError("snapshot venue mismatch")


def merge(
    previous: Dataset | None, fresh: Dataset, now: datetime
) -> tuple[Dataset, list[str]]:
    if previous is None:
        return fresh, []
    if (
        previous.metadata.venue != fresh.metadata.venue
        or previous.metadata.timezone != fresh.metadata.timezone
    ):
        raise DataError(
            "source venue/timezone changed; use a reviewed new market identity"
        )
    old = {c.timestamp: c for c in previous.candles}
    new = {c.timestamp: c for c in fresh.candles}
    revised = [
        stamp.isoformat()
        for stamp in old.keys() & new.keys()
        if old[stamp] != new[stamp]
    ]
    combined = old | new
    if (
        combined == old
        and fresh.metadata.request.start >= previous.metadata.request.start
    ):
        return previous, []
    request = replace(
        fresh.metadata.request,
        start=min(previous.metadata.request.start, fresh.metadata.request.start),
    )
    meta = replace(
        fresh.metadata,
        request=request,
        source="scanner-merge",
        retrieved_at=now,
        source_version="scanner-merge-v1",
        provenance=json.dumps(
            {"previous": fingerprint(previous), "fetched": fingerprint(fresh)},
            sort_keys=True,
        ),
    )
    result = Dataset(tuple(combined[stamp] for stamp in sorted(combined)), meta)
    validate(result, as_of=now)
    return result, sorted(revised)


def context_hash(item: Instrument, candles: tuple[Candle, ...]) -> str:
    # Parquet normalizes numeric values to float64; equivalent int/float inputs
    # must retain the same identity before and after a cache round trip.
    return digest(
        {
            "instrument": item.identity(),
            "candles": [
                [
                    c.timestamp.isoformat(),
                    c.closed_at.isoformat(),
                    *[
                        float(value)
                        for value in (c.open, c.high, c.low, c.close, c.volume)
                    ],
                ]
                for c in candles
            ],
        }
    )
