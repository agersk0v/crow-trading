"""Version 1 prior-high breakout / prior-low exit baseline."""

from dataclasses import dataclass
from datetime import datetime
from typing import ClassVar

from crow_trading.data.models import Candle
from crow_trading.strategies.base import (
    Signal,
    check_history,
    positive_integer,
    signal,
)


@dataclass(frozen=True)
class Momentum:
    entry_lookback: int = 20
    exit_lookback: int = 10
    strategy_id: ClassVar[str] = "momentum"
    version: ClassVar[str] = "1"

    def __post_init__(self) -> None:
        positive_integer(self.entry_lookback, "entry_lookback")
        positive_integer(self.exit_lookback, "exit_lookback")

    @property
    def history_required(self) -> int:
        return max(self.entry_lookback, self.exit_lookback) + 1

    def parameters(self) -> dict[str, int | float]:
        return {
            "entry_lookback": self.entry_lookback,
            "exit_lookback": self.exit_lookback,
        }

    def evaluate(
        self, symbol: str, candles: tuple[Candle, ...], *, as_of: datetime
    ) -> Signal | None:
        check_history(candles, as_of=as_of)
        if not candles:
            return None
        current = candles[-1]
        if len(candles) < self.history_required:
            return signal(self, symbol, current, "hold", "insufficient_history")
        highs = candles[-self.entry_lookback - 1 : -1]
        lows = candles[-self.exit_lookback - 1 : -1]
        if current.close > max(c.high for c in highs):
            return signal(self, symbol, current, "enter_long", "close_above_prior_high")
        if current.close < min(c.low for c in lows):
            return signal(self, symbol, current, "exit_long", "close_below_prior_low")
        return signal(self, symbol, current, "hold", "inside_breakout_bounds")
