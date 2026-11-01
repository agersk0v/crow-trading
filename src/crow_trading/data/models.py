"""Typed records shared by providers, validation, and storage."""

from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Literal


class DataError(ValueError):
    """A request or dataset cannot safely be used for research."""


@dataclass(frozen=True)
class Request:
    symbol: str
    start: date
    end: date
    currency: str
    kind: Literal["crypto", "stock"]
    calendar: str = "24/7"
    timeframe: str = "1d"
    adjusted: bool = False

    def __post_init__(self) -> None:
        if self.start >= self.end:
            raise DataError("start must precede exclusive end")
        if self.timeframe != "1d":
            raise DataError("only daily (1d) candles are supported")
        if not self.symbol or not self.currency:
            raise DataError("symbol and quote currency are required")
        if self.kind not in ("crypto", "stock"):
            raise DataError("kind must be crypto or stock")
        if self.kind == "crypto" and (self.calendar != "24/7" or self.adjusted):
            raise DataError("crypto requires calendar=24/7 and adjusted=False")
        if self.kind == "stock" and self.calendar == "24/7":
            raise DataError("stock requests require an exchange calendar")


@dataclass(frozen=True)
class Candle:
    timestamp: datetime
    closed_at: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass(frozen=True)
class Metadata:
    request: Request
    source: str
    venue: str
    timezone: str
    retrieved_at: datetime
    provenance: str
    source_version: str
    # All rows are closed and validated; no missing candles are synthesized.
    normalization: str = "daily-v1: UTC interval opens; closed_at exclusive; no fills"
    schema_version: int = 1


@dataclass(frozen=True)
class Dataset:
    candles: tuple[Candle, ...]
    metadata: Metadata


def utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise DataError("timestamps must have an explicit timezone")
    return value.astimezone(UTC)
