"""Offline integration of Phase 1 datasets and pure strategy calculations."""

from dataclasses import asdict
from datetime import datetime
from typing import Any, Literal

from crow_trading.data.models import Candle, Dataset, utc
from crow_trading.data.normalize import validate
from crow_trading.data.storage import fingerprint, metadata_dict
from crow_trading.strategies.base import Signal, Strategy, positive_integer


def closed_history(
    dataset: Dataset, *, as_of: datetime | None = None
) -> tuple[tuple[Candle, ...], datetime]:
    validate(dataset)
    cutoff = utc(as_of) if as_of is not None else dataset.candles[-1].closed_at
    return tuple(c for c in dataset.candles if c.closed_at <= cutoff), cutoff


def replay(
    dataset: Dataset, strategy: Strategy, *, as_of: datetime | None = None
) -> tuple[Signal, ...]:
    candles, _ = closed_history(dataset, as_of=as_of)
    positive_integer(strategy.history_required, "history_required")
    result: list[Signal] = []
    for index, candle in enumerate(candles):
        start = max(0, index + 1 - strategy.history_required)
        intent = strategy.evaluate(
            dataset.metadata.request.symbol,
            candles[start : index + 1],
            as_of=candle.closed_at,
        )
        if intent is not None:
            result.append(intent)
    return tuple(result)


def latest(
    dataset: Dataset, strategy: Strategy, *, as_of: datetime | None = None
) -> Signal | None:
    candles, cutoff = closed_history(dataset, as_of=as_of)
    positive_integer(strategy.history_required, "history_required")
    return strategy.evaluate(
        dataset.metadata.request.symbol,
        candles[-strategy.history_required :],
        as_of=cutoff,
    )


def report(
    dataset: Dataset,
    strategy: Strategy,
    *,
    mode: Literal["replay", "latest"] = "replay",
    as_of: datetime | None = None,
) -> dict[str, Any]:
    _, cutoff = closed_history(dataset, as_of=as_of)
    signals: tuple[Signal, ...]
    if mode == "latest":
        last = latest(dataset, strategy, as_of=cutoff)
        signals = () if last is None else (last,)
    elif mode == "replay":
        signals = replay(dataset, strategy, as_of=cutoff)
    else:
        raise ValueError("mode must be replay or latest")
    records = [
        {
            **asdict(item),
            "candle_timestamp": item.candle_timestamp.isoformat(),
            "available_at": item.available_at.isoformat(),
        }
        for item in signals
    ]
    return {
        "schema_version": 1,
        "dataset_fingerprint": fingerprint(dataset),
        "request": metadata_dict(dataset)["request"],
        "strategy": {
            "id": strategy.strategy_id,
            "version": strategy.version,
            "parameters": strategy.parameters(),
        },
        "mode": mode,
        "as_of": cutoff.isoformat(),
        "signals": records,
    }
