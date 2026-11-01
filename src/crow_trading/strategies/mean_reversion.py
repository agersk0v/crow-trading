"""Version 1 prior-close mean reversion baseline."""

import math
from dataclasses import dataclass
from datetime import datetime
from statistics import fmean
from typing import ClassVar

from crow_trading.data.models import Candle, DataError
from crow_trading.strategies.base import (
    Signal,
    check_history,
    positive_integer,
    signal,
)


@dataclass(frozen=True)
class MeanReversion:
    lookback: int = 20
    entry_deviation: float = 0.05
    strategy_id: ClassVar[str] = "mean-reversion"
    version: ClassVar[str] = "1"

    def __post_init__(self) -> None:
        positive_integer(self.lookback, "lookback")
        if (
            isinstance(self.entry_deviation, bool)
            or not math.isfinite(self.entry_deviation)
            or not 0 < self.entry_deviation < 1
        ):
            raise DataError("entry_deviation must be finite and between 0 and 1")

    @property
    def history_required(self) -> int:
        return self.lookback + 1

    def parameters(self) -> dict[str, int | float]:
        return {"lookback": self.lookback, "entry_deviation": self.entry_deviation}

    def evaluate(
        self, symbol: str, candles: tuple[Candle, ...], *, as_of: datetime
    ) -> Signal | None:
        check_history(candles, as_of=as_of)
        if not candles:
            return None
        current = candles[-1]
        if len(candles) < self.history_required:
            return signal(self, symbol, current, "hold", "insufficient_history")
        mean = fmean(c.close for c in candles[-self.lookback - 1 : -1])
        if current.close < mean * (1 - self.entry_deviation):
            return signal(
                self, symbol, current, "enter_long", "close_below_mean_threshold"
            )
        if current.close >= mean:
            return signal(self, symbol, current, "exit_long", "close_at_or_above_mean")
        return signal(self, symbol, current, "hold", "between_entry_threshold_and_mean")
