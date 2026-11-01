"""One recoverable watchlist scan; progress and report payloads commit together."""

import json
import sqlite3
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from crow_trading.data.models import DataError, Dataset, Request, utc
from crow_trading.data.storage import fingerprint, load, metadata_dict, save
from crow_trading.scanner.config import (
    SCANNER_VERSION,
    Instrument,
    Watchlist,
    definition,
    digest,
)
from crow_trading.scanner.datasets import check_source, context_hash, merge
from crow_trading.scanner.providers import Fetcher, Providers, fetch_retry
from crow_trading.scanner.reports import markdown
from crow_trading.scanner.state import State
from crow_trading.scanner.timing import Due, due_sessions, request_window


@dataclass(frozen=True)
class ScanResult:
    report: dict[str, Any] | None
    instruments: tuple[dict[str, Any], ...]
    report_files: tuple[str, ...]
    exit_code: int
    as_of: datetime


def expiry(item: Instrument, due: Due, index: int) -> datetime:
    stamp = due.opens[index]
    expires = due.closes[stamp] + timedelta(seconds=item.max_signal_age_seconds)
    next_ready = (
        due.closes[due.opens[index + 1]]
        + timedelta(seconds=item.finalization_delay_seconds)
        if index + 1 < len(due.opens)
        else due.next_ready_at
    )
    return min(expires, next_ready) if next_ready else expires


def process(
    state: State,
    item: Instrument,
    due: Due,
    now: datetime,
    fetcher: Fetcher,
    attempts: int,
    sleep: Callable[[float], None],
    refresh: bool,
) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any] | None]:
    previous = state.previous(item.key)
    last = datetime.fromisoformat(previous["last_timestamp"]) if previous else None
    status: dict[str, Any] = {
        "instrument": item.name,
        "identity": item.identity(),
        "policy_hash": item.policy_hash,
        "expected_candle": due.latest.isoformat(),
        "expires_at": expiry(item, due, len(due.opens) - 1).isoformat(),
        "last_successful_candle": last.isoformat() if last else None,
        "freshness": "fresh"
        if now < expiry(item, due, len(due.opens) - 1)
        else "stale",
    }
    if (
        previous
        and last == due.latest
        and previous["policy_hash"] == item.policy_hash
        and not refresh
    ):
        return {**status, "status": "not_due"}, [], None
    if last is not None and last > due.latest:
        raise DataError("clock/publication policy is earlier than committed progress")
    old: Dataset | None = None
    if previous:
        old = load(state.root / previous["snapshot_path"])
        if fingerprint(old) != previous["snapshot_fingerprint"]:
            raise DataError("committed snapshot fingerprint mismatch")
    request = request_window(
        item, due, last, old.metadata.request.start if old else None
    )
    fresh = fetch_retry(fetcher, item, request, now, attempts, sleep)
    if fresh.metadata.request != request:
        raise DataError("provider did not return the exact requested coverage contract")
    check_source(fresh, item, now)
    fetched_fp = fingerprint(fresh)
    save(fresh, state.root / "snapshots" / f"{fetched_fp}.parquet")
    data, revised = merge(old, fresh, now)
    current_fp = fingerprint(data)
    relative_path = f"snapshots/{current_fp}.parquet"
    save(data, state.root / relative_path)
    candles = data.candles
    indices = {c.timestamp: i for i, c in enumerate(candles)}
    due_indices = {stamp: i for i, stamp in enumerate(due.opens)}
    targets: set[datetime] = {
        c.timestamp for c in candles if last is not None and c.timestamp > last
    }
    if last is None or (previous and previous["policy_hash"] != item.policy_hash):
        targets.add(due.latest)
    if revised:
        known = state.known_candles(item.key)
        maximum = max(s.history_required for s in item.strategies)
        for changed in revised:
            changed_index = indices[datetime.fromisoformat(changed)]
            for candle in candles[changed_index : changed_index + maximum]:
                if candle.timestamp.isoformat() in known:
                    targets.add(candle.timestamp)
    # Explicit refresh checks the latest context, even when no rows changed.
    if refresh:
        targets.add(due.latest)
    records: list[dict[str, Any]] = []
    for stamp in sorted(targets):
        index = indices[stamp]
        for strategy in item.strategies:
            history = candles[max(0, index + 1 - strategy.history_required) : index + 1]
            signal = strategy.evaluate(item.symbol, history, as_of=due.closes[stamp])
            assert signal is not None
            input_fp = context_hash(item, history)
            spec = definition(strategy)
            strategy_key = digest(spec)
            identity = digest([item.key, stamp.isoformat(), strategy_key, input_fp])
            expires_at = expiry(item, due, due_indices[stamp])
            classification = (
                "revision"
                if last is not None and stamp <= last
                else (
                    "fresh" if stamp == due.latest and now < expires_at else "catchup"
                )
            )
            record = {
                **asdict(signal),
                "id": identity,
                "instrument": item.name,
                "instrument_key": item.key,
                "identity": item.identity(),
                "strategy": spec,
                "strategy_key": strategy_key,
                "candle_timestamp": signal.candle_timestamp.isoformat(),
                "available_at": signal.available_at.isoformat(),
                "expires_at": expires_at.isoformat(),
                "classification": classification,
                "data_mode": item.provider,
                "signal_input_fingerprint": input_fp,
                "dataset_fingerprint": current_fp,
                "snapshot_path": relative_path,
                "evaluated_as_of": now.isoformat(),
            }
            if state.add_signal(record):
                records.append(record)
    state.progress(
        item.key, item.policy_hash, due.latest.isoformat(), relative_path, current_fp
    )
    snapshot_changed = (
        previous is None or current_fp != previous["snapshot_fingerprint"]
    )
    policy_changed = previous is None or previous["policy_hash"] != item.policy_hash
    update = (
        {
            "instrument": item.name,
            "dataset_fingerprint": current_fp,
            "previous_dataset_fingerprint": previous["snapshot_fingerprint"]
            if previous
            else None,
            "fetched_dataset_fingerprint": fetched_fp,
            "revised_candles": revised,
            "data_changed": snapshot_changed,
            "policy_changed": policy_changed,
            "policy_hash": item.policy_hash,
            "strategy_definitions": [definition(s) for s in item.strategies],
            "finalization_delay_seconds": item.finalization_delay_seconds,
            "max_signal_age_seconds": item.max_signal_age_seconds,
            "metadata": metadata_dict(data),
        }
        if snapshot_changed or policy_changed
        else None
    )
    return (
        {
            **status,
            "status": "updated" if snapshot_changed or records else "checked_unchanged",
            "last_successful_candle": due.latest.isoformat(),
        },
        records,
        update,
    )


def scan(
    watchlist: Watchlist,
    *,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    fetcher: Fetcher | None = None,
    sleep: Callable[[float], None] = time.sleep,
    refresh: bool = False,
) -> ScanResult:
    cutoff = utc(clock())
    fetcher = fetcher or Providers()
    statuses: list[dict[str, Any]] = []
    records: list[dict[str, Any]] = []
    updates: list[dict[str, Any]] = []
    requested_providers: set[tuple[str, str]] = set()
    report: dict[str, Any] | None = None
    with State(watchlist.state_dir) as state:
        files = state.export()  # Recover previously committed reports before new work.
        for item in watchlist.instruments:
            try:
                with state.asset():
                    due = due_sessions(item, cutoff)

                    # A limiter only delays actual provider calls, not no-op ticks.
                    def limited(
                        item: Instrument, request: Request, now: datetime
                    ) -> Dataset:
                        key = (item.provider, item.venue)
                        if (
                            key in requested_providers
                            and watchlist.provider_interval_seconds
                        ):
                            sleep(float(watchlist.provider_interval_seconds))
                        requested_providers.add(key)
                        return fetcher(item, request, now)

                    status, added, update = process(
                        state,
                        item,
                        due,
                        cutoff,
                        limited,
                        watchlist.attempts,
                        sleep,
                        refresh,
                    )
                    statuses.append(status)
                    records.extend(added)
                    if update:
                        updates.append(update)
            except sqlite3.Error:
                raise  # A state failure invalidates the batch transaction.
            except Exception as exc:
                old = state.previous(item.key)
                statuses.append(
                    {
                        "instrument": item.name,
                        "identity": item.identity(),
                        "status": "error",
                        "freshness": "unavailable",
                        "error": str(exc)[:1000],
                        "last_successful_candle": old["last_timestamp"]
                        if old
                        else None,
                    }
                )
        finished = utc(clock())
        if finished < cutoff:
            raise DataError("scan clock moved backwards")
        for status in statuses:
            if status.get("expires_at"):
                status["freshness"] = (
                    "fresh"
                    if finished < datetime.fromisoformat(status["expires_at"])
                    else "stale"
                )
        # A slow fetch must not leave expired matches in the fresh summary.
        for record in records:
            if (
                record["classification"] == "fresh"
                and datetime.fromisoformat(record["expires_at"]) <= finished
            ):
                record["classification"] = "catchup"
                state.connection.execute(
                    "UPDATE signals SET record_json=? WHERE id=?",
                    (json.dumps(record, sort_keys=True), record["id"]),
                )
        errors = any(s["status"] == "error" for s in statuses)
        if records or updates or errors:
            report = state.report(
                {
                    "schema_version": 1,
                    "scanner_version": SCANNER_VERSION,
                    "as_of": finished.isoformat(),
                    "cutoff_at": cutoff.isoformat(),
                    "status": "partial_failure"
                    if errors and updates
                    else ("failed" if errors else "success"),
                    "instruments": statuses,
                    "signals": records,
                    "updates": updates,
                    "fresh_entries": [
                        r
                        for r in records
                        if r["classification"] == "fresh"
                        and r["intent"] == "enter_long"
                        and r["data_mode"] != "snapshot"
                    ],
                    "fixture_entries": [
                        r
                        for r in records
                        if r["classification"] == "fresh"
                        and r["intent"] == "enter_long"
                        and r["data_mode"] == "snapshot"
                    ],
                },
                markdown,
            )
        state.commit()  # No progress without the associated report payload.
        files.extend(state.export())
    return ScanResult(report, tuple(statuses), tuple(files), int(errors), finished)
