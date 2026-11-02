"""Read committed scanner signals/snapshots without changing scanner state."""

import hashlib
import json
import os
import sqlite3
import tempfile
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from crow_trading.backtesting.market import FXBook, check_actions
from crow_trading.backtesting.models import number
from crow_trading.data.models import Candle, DataError, Dataset, Request
from crow_trading.data.normalize import schedule, validate
from crow_trading.data.storage import fingerprint, load, save
from crow_trading.paper.config import Account, Asset, Config
from crow_trading.scanner.timing import due_sessions


def archive(root: Path, path: Path) -> tuple[dict[str, Any], str]:
    try:
        content = path.read_bytes()
    except OSError as exc:
        raise DataError(f"input is unavailable: {path}") from exc
    identity = hashlib.sha256(content).hexdigest()
    target = root / "inputs" / (identity + ".json")
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(dir=target.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, target)
        except FileExistsError:
            if target.read_bytes() != content:
                raise DataError("archived input conflict") from None
    finally:
        Path(temporary).unlink(missing_ok=True)
    return json.loads(content), identity


@dataclass(frozen=True)
class Quote:
    candle: Candle
    dataset_fingerprint: str
    mark_price: float | None = None
    mark_time: datetime | None = None


@dataclass(frozen=True)
class PriceMark:
    price: Decimal
    evidence: dict[str, Any]


class Source:
    def __init__(self, config: Config, now: datetime) -> None:
        self.config, self.now = config, now
        path = config.watchlist.state_dir / "scanner.sqlite3"
        db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)
        try:
            db.row_factory = sqlite3.Row
            db.execute("BEGIN")
            application = db.execute("PRAGMA application_id").fetchone()[0]
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if application != 1129477971 or version not in (1, 2):
                raise DataError("unsupported scanner database")
            self.signals = [
                json.loads(row[0])
                for row in db.execute("SELECT record_json FROM signals ORDER BY rowid")
            ]
            self.progress = {
                row["identity"]: dict(row)
                for row in db.execute("SELECT * FROM instruments")
            }
        finally:
            db.close()
        self.latest_signals = {
            (r["instrument_key"], r["candle_timestamp"], r["strategy_key"]): r["id"]
            for r in self.signals
        }
        self.datasets: dict[str, Dataset] = {}
        self.fx_raw, self.fx_fingerprint = archive(config.state_dir, config.fx_snapshot)
        self.actions: dict[str, dict[str, Any]] = {}

    def data(
        self, asset: Asset, *, path: str | None = None, expected_fp: str | None = None
    ) -> Dataset:
        item = asset.instrument
        if path is None:
            if item.key not in self.progress:
                raise DataError(f"scanner has no snapshot for {item.name}")
            progress = self.progress[item.key]
            path, expected_fp = (
                progress["snapshot_path"],
                progress["snapshot_fingerprint"],
            )
        absolute = (self.config.watchlist.state_dir / path).resolve()
        if not absolute.is_relative_to(self.config.watchlist.state_dir.resolve()):
            raise DataError("snapshot path escapes scanner state")
        key = str(absolute)
        if key not in self.datasets:
            self.datasets[key] = load(absolute)
        data = self.datasets[key]
        validate(data, as_of=self.now)
        req = data.metadata.request
        if (
            fingerprint(data) != expected_fp
            or req.symbol != item.symbol
            or req.kind != item.kind
            or req.currency != item.currency
            or req.calendar != item.calendar
            or req.adjusted
        ):
            raise DataError("scanner snapshot identity/fingerprint mismatch")
        if item.kind == "stock":
            if asset.corporate_actions is None:
                raise DataError(
                    "stock paper trading requires verified action/price coverage"
                )
            action, identity = archive(self.config.state_dir, asset.corporate_actions)
            if (
                date.fromisoformat(action["start"]) > req.start
                or date.fromisoformat(action["end"]) < req.end
            ):
                raise DataError(
                    "corporate-action snapshot does not cover cached prices"
                )
            check_actions(
                data, {**action, "start": str(req.start), "end": str(req.end)}
            )
            self.actions[identity] = action
        target = self.config.state_dir / "inputs" / (fingerprint(data) + ".parquet")
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.link(absolute, target)
        except OSError:
            # Existing archives are verified by save; cross-filesystem links fall back
            # to an independent immutable copy with the same integrity checks.
            save(data, target)
        return data

    def fx(self, account: Account) -> FXBook:
        return FXBook(self.fx_raw, account.settings.fx_max_age_hours)

    def entry_price(self, asset: Asset, when: datetime, max_age: int) -> Decimal:
        return number(
            self.mark(asset, when, current=True, max_age=max_age).candle.close
        )

    def decision_evidence(self, asset: Asset) -> dict[str, Any]:
        return {}

    def valuation_mark(
        self, asset: Asset, when: datetime, *, current: bool, max_age: int
    ) -> PriceMark:
        quote = self.mark(asset, when, current=current, max_age=max_age)
        return PriceMark(
            number(
                quote.mark_price if quote.mark_price is not None else quote.candle.close
            ),
            {
                "candle_timestamp": quote.candle.timestamp.isoformat(),
                "price_time": (quote.mark_time or quote.candle.closed_at).isoformat(),
                "dataset_fingerprint": quote.dataset_fingerprint,
            },
        )

    def mark(
        self, asset: Asset, when: datetime, *, current: bool, max_age: int
    ) -> Quote:
        data = self.data(asset)
        if not current:
            opening = next((c for c in data.candles if c.timestamp == when), None)
            if opening is not None:
                return Quote(opening, fingerprint(data), opening.open, when)
        candidates = [c for c in data.candles if c.closed_at <= when]
        if not candidates:
            raise DataError("no closed price available for valuation")
        candle = candidates[-1]
        if when - candle.closed_at > timedelta(seconds=max_age):
            raise DataError("stale valuation price")
        if current and candle.timestamp != due_sessions(asset.instrument, when).latest:
            raise DataError("scanner price is behind the expected completed session")
        return Quote(candle, fingerprint(data), candle.close, candle.closed_at)


class DailyBarAdapter:
    """Full simulated fills only; observations arrive after the eligible bar closes."""

    def next_quote(
        self, order: dict[str, Any], asset: Asset, source: Source
    ) -> Quote | None:
        data = source.data(asset)
        submitted = datetime.fromisoformat(order["created_at"])
        policy = json.loads(order["policy_json"])
        threshold = submitted + timedelta(seconds=policy["delay_seconds"])
        day = threshold.astimezone(ZoneInfo(data.metadata.timezone)).date()
        item = asset.instrument
        request = Request(
            item.symbol,
            day,
            source.now.date() + timedelta(days=14),
            item.currency,
            item.kind,
            item.calendar,
        )
        expected, _ = schedule(request)
        candidates = [
            (opened, closed)
            for opened, closed in expected.items()
            if opened > threshold
        ]
        if not candidates:
            return None
        opened, closed = candidates[0]
        if closed + timedelta(seconds=item.finalization_delay_seconds) > source.now:
            return None
        candle = next((c for c in data.candles if c.timestamp == opened), None)
        if candle is None:
            raise DataError(
                "first eligible execution bar is missing; refusing a later substitute"
            )
        return Quote(candle, fingerprint(data))
