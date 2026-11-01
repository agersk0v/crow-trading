"""Immutable account policy and explicit live-data versus fixture separation."""

import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal

from crow_trading.backtesting.models import Costs, Settings, number, parse_settings
from crow_trading.data.models import DataError
from crow_trading.scanner.config import (
    Instrument,
    Watchlist,
    definition,
    digest,
    integer,
    read_watchlist,
    strategy,
)

PAPER_VERSION = "1"


@dataclass(frozen=True)
class Asset:
    instrument: Instrument
    costs: Costs
    corporate_actions: Path | None = None


@dataclass(frozen=True)
class Account:
    name: str
    strategy: dict[str, Any]
    settings: Settings
    max_positions: int
    mark_max_age_seconds: int
    signal_max_age_seconds: int
    assets: tuple[Asset, ...]

    def policy(self, mode: str) -> dict[str, Any]:
        # Input file locations may move/update; actual bytes are archived per use.
        return {
            "paper_version": PAPER_VERSION,
            "mode": mode,
            "name": self.name,
            "strategy": self.strategy,
            "settings": json.loads(json.dumps(asdict(self.settings), default=str)),
            "max_positions": self.max_positions,
            "mark_max_age_seconds": self.mark_max_age_seconds,
            "signal_max_age_seconds": self.signal_max_age_seconds,
            "assets": [
                {
                    "identity": a.instrument.identity(),
                    "costs": {k: str(v) for k, v in asdict(a.costs).items()},
                }
                for a in self.assets
            ],
        }


@dataclass(frozen=True)
class Config:
    state_dir: Path
    watchlist: Watchlist
    mode: Literal["paper", "fixture"]
    fx_snapshot: Path
    accounts: tuple[Account, ...]


def read_config(path: Path) -> Config:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if raw["schema_version"] != 1 or raw["fill_policy"] != "next_closed_daily_open":
        raise DataError(
            "only schema 1 and next_closed_daily_open full fills are supported"
        )
    if raw["mode"] not in ("paper", "fixture"):
        raise DataError("mode must be paper or fixture")
    watchlist = read_watchlist(path.parent / raw["watchlist"])
    instruments = {item.name: item for item in watchlist.instruments}
    accounts: list[Account] = []
    for item in raw["accounts"]:
        if not re.fullmatch(r"[A-Za-z0-9_-]+", item["name"]):
            raise DataError("account names must contain letters, numbers, _ or -")
        spec = definition(strategy(item["strategy"]))
        settings = parse_settings(item["settings"])
        assets: list[Asset] = []
        for value in item["assets"]:
            instrument = instruments[value["name"]]
            if digest(spec) not in {
                digest(definition(s)) for s in instrument.strategies
            }:
                raise DataError(
                    "account strategy must match the scanner's version/parameters"
                )
            if (raw["mode"] == "fixture") != (instrument.provider == "snapshot"):
                raise DataError("fixture and public-data accounts cannot be mixed")
            if instrument.adjusted:
                raise DataError("paper fills require unadjusted price inputs")
            costs = Costs(**{key: number(val) for key, val in value["costs"].items()})
            actions = value.get("corporate_actions")
            assets.append(
                Asset(
                    instrument,
                    costs,
                    (path.parent / actions).resolve() if actions else None,
                )
            )
        if not assets or len({a.instrument.key for a in assets}) != len(assets):
            raise DataError("account assets must be nonempty and unique")
        accounts.append(
            Account(
                item["name"],
                spec,
                settings,
                integer(item["max_positions"], "max_positions", 1, 100),
                integer(item["mark_max_age_seconds"], "mark age", 1, 604800),
                integer(item["signal_max_age_seconds"], "signal age", 1, 604800),
                tuple(assets),
            )
        )
    if not accounts or len({a.name for a in accounts}) != len(accounts):
        raise DataError("account names must be nonempty and unique")
    return Config(
        (path.parent / raw["state_dir"]).resolve(),
        watchlist,
        raw["mode"],
        (path.parent / raw["fx_snapshot"]).resolve(),
        tuple(accounts),
    )
