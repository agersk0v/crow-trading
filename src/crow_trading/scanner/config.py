"""Typed, versioned watchlists; strategy parameters are explicit inputs."""

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from crow_trading.data.models import DataError
from crow_trading.strategies import MeanReversion, Momentum, Strategy

SCANNER_VERSION = "1"


def digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()
    ).hexdigest()


def integer(value: object, name: str, minimum: int, maximum: int) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not minimum <= value <= maximum
    ):
        raise DataError(f"{name} must be an integer in [{minimum}, {maximum}]")
    return value


def strategy(raw: dict[str, Any]) -> Strategy:
    result: Strategy
    if raw["id"] == "momentum":
        result = Momentum(**raw["parameters"])
    elif raw["id"] == "mean-reversion":
        result = MeanReversion(**raw["parameters"])
    else:
        raise DataError("unknown scanner strategy")
    if raw["version"] != result.version:
        raise DataError("configured strategy version is not installed")
    return result


def definition(item: Strategy) -> dict[str, Any]:
    return {
        "id": item.strategy_id,
        "version": item.version,
        "parameters": item.parameters(),
    }


@dataclass(frozen=True)
class Instrument:
    name: str
    kind: Literal["crypto", "stock"]
    provider: Literal["ccxt", "yfinance", "snapshot"]
    venue: str
    symbol: str
    currency: str
    calendar: str
    adjusted: bool
    history_bars: int
    overlap_bars: int
    max_catchup_bars: int
    finalization_delay_seconds: int
    max_signal_age_seconds: int
    strategies: tuple[Strategy, ...]
    snapshot: Path | None = None

    def identity(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "provider": self.provider,
            "venue": self.venue,
            "symbol": self.symbol,
            "currency": self.currency,
            "calendar": self.calendar,
            "adjusted": self.adjusted,
            "timeframe": "1d",
        }

    @property
    def key(self) -> str:
        return digest(self.identity())

    @property
    def policy_hash(self) -> str:
        return digest(
            {
                **self.identity(),
                "scanner_version": SCANNER_VERSION,
                "history_bars": self.history_bars,
                "overlap_bars": self.overlap_bars,
                "max_catchup_bars": self.max_catchup_bars,
                "finalization_delay_seconds": self.finalization_delay_seconds,
                "max_signal_age_seconds": self.max_signal_age_seconds,
                "strategies": [definition(s) for s in self.strategies],
                "snapshot": str(self.snapshot) if self.snapshot else None,
            }
        )


@dataclass(frozen=True)
class Watchlist:
    state_dir: Path
    instruments: tuple[Instrument, ...]
    attempts: int
    provider_interval_seconds: int


def read_watchlist(path: Path) -> Watchlist:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if raw["schema_version"] != 1:
        raise DataError("unsupported watchlist schema")
    parsed: list[Instrument] = []
    for value in raw["instruments"]:
        value = dict(value)
        strategies = tuple(strategy(s) for s in value.pop("strategies"))
        if not strategies or len({s.strategy_id for s in strategies}) != len(
            strategies
        ):
            raise DataError("configure each strategy ID once per instrument")
        snapshot = value.pop("snapshot", None)
        item = Instrument(
            **value,
            strategies=strategies,
            snapshot=(path.parent / snapshot).resolve() if snapshot else None,
        )
        if not re.fullmatch(r"[A-Za-z0-9_-]+", item.name):
            raise DataError("instrument names must contain letters, numbers, _ or -")
        if item.kind not in ("crypto", "stock"):
            raise DataError("kind must be crypto or stock")
        if item.provider not in ("ccxt", "yfinance", "snapshot"):
            raise DataError("unsupported provider")
        if (item.provider == "ccxt" and item.kind != "crypto") or (
            item.provider == "yfinance" and item.kind != "stock"
        ):
            raise DataError("provider does not support the configured market kind")
        if item.provider == "snapshot" and item.snapshot is None:
            raise DataError("snapshot provider requires a local snapshot path")
        if item.provider != "snapshot" and item.snapshot is not None:
            raise DataError("snapshot paths apply only to the snapshot provider")
        if not item.venue or not item.symbol or not item.currency:
            raise DataError("venue, symbol, and currency are required")
        if not isinstance(item.adjusted, bool):
            raise DataError("adjusted must be boolean")
        if item.kind == "crypto" and (item.calendar != "24/7" or item.adjusted):
            raise DataError("crypto requires calendar=24/7 and adjusted=false")
        if item.kind == "stock" and item.calendar == "24/7":
            raise DataError("stocks require an exchange calendar")
        integer(
            item.history_bars,
            "history_bars",
            max(s.history_required for s in strategies),
            5000,
        )
        integer(item.overlap_bars, "overlap_bars", 1, item.history_bars)
        integer(item.max_catchup_bars, "max_catchup_bars", 1, 5000)
        integer(item.finalization_delay_seconds, "finalization_delay_seconds", 0, 86400)
        integer(item.max_signal_age_seconds, "max_signal_age_seconds", 1, 604800)
        parsed.append(item)
    if not parsed or len(parsed) > 100:
        raise DataError("watchlists require 1–100 instruments")
    if len({i.key for i in parsed}) != len(parsed) or len(
        {i.name for i in parsed}
    ) != len(parsed):
        raise DataError("instrument names and market identities must be unique")
    return Watchlist(
        (path.parent / raw["state_dir"]).resolve(),
        tuple(parsed),
        integer(raw["attempts"], "attempts", 1, 5),
        integer(raw["provider_interval_seconds"], "provider interval", 0, 60),
    )
