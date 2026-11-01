"""Pure strategy contracts and input guards; no I/O or clock access."""

import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal, Protocol

from crow_trading.data.models import Candle, DataError, utc

Intent = Literal["enter_long", "exit_long", "hold"]


@dataclass(frozen=True)
class Signal:
    symbol: str
    strategy_id: str
    strategy_version: str
    candle_timestamp: datetime
    available_at: datetime
    intent: Intent
    reason: str


class Strategy(Protocol):
    @property
    def strategy_id(self) -> str: ...

    @property
    def version(self) -> str: ...

    @property
    def history_required(self) -> int: ...

    def parameters(self) -> dict[str, int | float]: ...

    def evaluate(
        self, symbol: str, candles: tuple[Candle, ...], *, as_of: datetime
    ) -> Signal | None: ...


def positive_integer(value: int, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise DataError(f"{name} must be a positive integer")


def check_history(candles: tuple[Candle, ...], *, as_of: datetime) -> None:
    cutoff = utc(as_of)
    previous: Candle | None = None
    for candle in candles:
        opened, closed = utc(candle.timestamp), utc(candle.closed_at)
        if candle.timestamp.utcoffset() != timedelta(0):
            raise DataError("candle timestamps must be normalized to UTC")
        if candle.closed_at.utcoffset() != timedelta(0):
            raise DataError("candle close times must be normalized to UTC")
        if not opened < closed <= opened + timedelta(days=1):
            raise DataError("expected a daily candle interval")
        if closed > cutoff:
            raise DataError("strategy cannot evaluate an unclosed candle")
        if previous is not None and opened < previous.closed_at:
            raise DataError("candles must be ordered and non-overlapping")
        values = (candle.open, candle.high, candle.low, candle.close, candle.volume)
        if not all(math.isfinite(value) for value in values):
            raise DataError("OHLCV must be finite")
        if min(values[:4]) <= 0 or candle.volume < 0:
            raise DataError("prices must be positive and volume nonnegative")
        if not (
            candle.low
            <= min(candle.open, candle.close)
            <= max(candle.open, candle.close)
            <= candle.high
        ):
            raise DataError("invalid OHLC bounds")
        previous = candle


def signal(
    strategy: Strategy, symbol: str, candle: Candle, intent: Intent, reason: str
) -> Signal:
    if not symbol:
        raise DataError("symbol is required")
    return Signal(
        symbol,
        strategy.strategy_id,
        strategy.version,
        candle.timestamp,
        candle.closed_at,
        intent,
        reason,
    )
