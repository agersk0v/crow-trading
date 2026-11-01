"""Explicit Decimal-based settings and chronological evaluation windows."""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

from crow_trading.data.models import DataError

ZERO = Decimal("0")
ONE = Decimal("1")


def number(value: object) -> Decimal:
    try:
        result = Decimal(str(value))
    except InvalidOperation as exc:
        raise DataError(f"invalid decimal: {value}") from exc
    if not result.is_finite():
        raise DataError("numbers must be finite")
    return result


@dataclass(frozen=True)
class Costs:
    fee_rate: Decimal
    fixed_fee_quote: Decimal
    spread_bps: Decimal
    slippage_bps: Decimal
    quantity_step: Decimal
    minimum_quantity: Decimal
    minimum_notional_quote: Decimal

    def __post_init__(self) -> None:
        for value in vars(self).values():
            if not isinstance(value, Decimal) or not value.is_finite() or value < ZERO:
                raise DataError(
                    "costs and quantity limits must be finite nonnegative decimals"
                )
        if self.quantity_step <= ZERO or self.fee_rate >= ONE:
            raise DataError("quantity_step must be positive and fee_rate below 1")
        if self.spread_bps / 2 + self.slippage_bps >= 10000:
            raise DataError("execution impact must be below 100%")


@dataclass(frozen=True)
class Settings:
    account_currency: str
    initial_cash: Decimal
    target_eur: Decimal
    max_position_eur: Decimal
    max_exposure: Decimal
    max_loss: Decimal
    max_drawdown: Decimal
    delivery_delay_seconds: int
    fx_max_age_hours: int
    costs: Costs

    def __post_init__(self) -> None:
        if not self.account_currency or not self.account_currency.isalpha():
            raise DataError("account currency must be a currency code")
        for value in (self.initial_cash, self.target_eur, self.max_position_eur):
            if not value.is_finite() or value <= ZERO:
                raise DataError("cash and EUR position limits must be positive")
        for value in (self.max_exposure, self.max_loss, self.max_drawdown):
            if not value.is_finite() or not ZERO < value <= ONE:
                raise DataError("risk fractions must be in (0, 1]")
        if (
            isinstance(self.delivery_delay_seconds, bool)
            or not isinstance(self.delivery_delay_seconds, int)
            or not 0 <= self.delivery_delay_seconds <= 3600
        ):
            raise DataError("delivery delay must be an integer from 0 to 3600 seconds")
        if (
            isinstance(self.fx_max_age_hours, bool)
            or not isinstance(self.fx_max_age_hours, int)
            or self.fx_max_age_hours <= 0
        ):
            raise DataError("FX max age must be a positive integer")


@dataclass(frozen=True)
class Window:
    name: str
    start: date
    end: date


def parse_settings(raw: dict[str, Any]) -> Settings:
    values = dict(raw)
    costs = Costs(
        **{name: number(value) for name, value in values.pop("costs").items()}
    )
    for key in (
        "initial_cash",
        "target_eur",
        "max_position_eur",
        "max_exposure",
        "max_loss",
        "max_drawdown",
    ):
        values[key] = number(values[key])
    return Settings(**values, costs=costs)


def parse_windows(raw: list[dict[str, str]]) -> tuple[Window, ...]:
    windows = tuple(
        Window(
            item["name"],
            date.fromisoformat(item["start"]),
            date.fromisoformat(item["end"]),
        )
        for item in raw
    )
    if tuple(w.name for w in windows) != ("training", "validation", "test"):
        raise DataError(
            "define training, validation, test windows in chronological order"
        )
    for index, window in enumerate(windows):
        if window.start >= window.end:
            raise DataError("evaluation windows must be nonempty [start, end)")
        if index and window.start < windows[index - 1].end:
            raise DataError("evaluation windows must not overlap")
    return windows
