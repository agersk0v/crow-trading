"""Sequential, restartable cycles; no changes to strategy or fill semantics."""

import json
import math
import sqlite3
import time
from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from crow_trading.data.models import DataError
from crow_trading.paper.config import Config
from crow_trading.paper.engine import initialize, step
from crow_trading.paper.operations import inspect
from crow_trading.scanner.engine import ScanResult, scan
from crow_trading.scanner.providers import Fetcher
from crow_trading.scanner.state import Busy


def clock() -> datetime:
    return datetime.now(UTC)


@contextmanager
def ownership(roots: tuple[Path, ...]) -> Iterator[None]:
    """Separate SQLite locks survive neither normal exit nor process death.

    Lock both roots so different paper configs cannot share one scanner runner.
    Existing one-shot commands retain their short-lived accounting locks.
    """
    with ExitStack() as stack:
        for root in sorted({p.resolve() for p in roots}):
            root.mkdir(parents=True, exist_ok=True)
            db = sqlite3.connect(root / "runner-lock.sqlite3", timeout=0)
            stack.callback(db.close)
            try:
                db.execute("BEGIN IMMEDIATE")
            except sqlite3.OperationalError as exc:
                if "locked" in str(exc).lower():
                    raise Busy("another runner owns scanner or paper state") from exc
                raise
        yield


def checked_snapshot(config: Config) -> dict[str, Any]:
    snapshot = inspect(config.state_dir)
    if snapshot["reconciliation_errors"]:
        raise DataError(
            f"account reconciliation failed: {snapshot['reconciliation_errors']}"
        )
    return snapshot


def activity(
    config: Config,
    snapshot: dict[str, Any],
    seen_orders: set[str],
    seen_fills: set[str],
    emit: Callable[[str], None],
    now: datetime,
    paper_result: dict[str, Any] | None,
) -> None:
    prefix = now.isoformat()
    names = {a.name for a in config.accounts}
    symbols = {
        a.instrument.key: a.instrument.symbol for c in config.accounts for a in c.assets
    }
    orders = [r for r in snapshot["orders"] if r["account_id"] in names]
    fills = [json.loads(r["payload"]) for r in snapshot["fills"]]
    new_fills = [
        r for r in fills if r["account_id"] in names and r["order_id"] not in seen_fills
    ]
    for order in orders:
        if order["id"] not in seen_orders:
            emit(
                f"{prefix} ORDER {order['account_id']} {order['side'].upper()} "
                f"{symbols.get(order['instrument_key'], order['instrument_key'])} "
                f"status={order['status']} id={order['id']} "
                "(simulated order, not a fill)"
            )
    for fill in new_fills:
        emit(
            f"{prefix} FILL {fill['account_id']} {fill['side'].upper()} "
            f"{fill['quantity']} {fill['symbol']} at {fill['fill_price_quote']} "
            f"{fill['quote_currency']} fee={fill['fee_account']} "
            f"{fill['account_currency']} "
            f"effective_at={fill['effective_at']} recorded_at={fill['recorded_at']}"
        )
    emit(f"{prefix} Trades: {len(new_fills)} new simulated fills.")
    valuations = {
        r["account_id"]: json.loads(r["payload"]) for r in snapshot["valuations"]
    }
    results = {r["account"]: r for r in (paper_result or {}).get("accounts", [])}
    for account in snapshot["accounts"]:
        name = account["id"]
        if name not in names:
            continue
        valuation = valuations.get(name)
        current = bool(
            valuation and valuation["as_of"] == (paper_result or {}).get("as_of")
        )
        value = (
            f"holdings={valuation['holdings']} total={valuation['equity']} "
            f"valuation_at={valuation['as_of']} {'current' if current else 'stale'}"
            if valuation
            else "holdings=unavailable total=unavailable valuation=unavailable"
        )
        currency = next(
            a.settings.account_currency for a in config.accounts if a.name == name
        )
        pending = sum(
            r["account_id"] == name and r["status"] == "pending" for r in orders
        )
        emit(
            f"{prefix} ACCOUNT {name} ({currency}) cash={account['cash']} "
            f"reserved={account['reserved']} {value} pending={pending} "
            f"stopped={bool(account['stopped'])} "
            f"risk_halted={bool(account['risk_halted'])}"
        )
        for error in results.get(name, {}).get("errors", []):
            emit(f"{prefix} ERROR {name}: {error}")
    seen_orders.update(r["id"] for r in orders)
    seen_fills.update(r["order_id"] for r in fills)


def run(
    config: Config,
    interval: float = 300,
    *,
    now: Callable[[], datetime] = clock,
    wait: Callable[[float], None] = time.sleep,
    emit: Callable[[str], None] = print,
    fetcher: Fetcher | None = None,
    scan_tick: Callable[[], ScanResult] | None = None,
    paper_tick: Callable[[], dict[str, Any]] | None = None,
) -> None:
    """Immediate tick, then fixed delay after completion; never queue catch-up.

    Injectable clock/wait and tick functions allow deterministic offline tests.
    Console activity is session-based: existing history is not replayed at startup.
    SQLite accounting remains authoritative, not console delivery.
    """
    if not math.isfinite(interval) or interval <= 0:
        raise DataError("interval must be a finite positive number of seconds")
    with ownership((config.state_dir, config.watchlist.state_dir)):
        initialize(config, now())
        snapshot = checked_snapshot(config)
        seen_orders = {r["id"] for r in snapshot["orders"]}
        seen_fills = {r["order_id"] for r in snapshot["fills"]}
        emit(
            f"{now().isoformat()} Runner started: simulated funds only; "
            f"interval={interval:g}s."
        )
        while True:
            # Revalidate immutable policies, clock, and ledger before market work.
            try:
                initialize(config, now())
                checked_snapshot(config)
            except (Busy, OSError, sqlite3.OperationalError) as exc:
                emit(
                    f"{now().isoformat()} ERROR state unavailable: {exc}; "
                    f"retry in {interval:g}s."
                )
                wait(interval)
                continue
            degraded = False
            try:
                result = (
                    scan_tick()
                    if scan_tick
                    else scan(config.watchlist, clock=now, fetcher=fetcher)
                )
                status = result.report["status"] if result.report else "no_changes"
                emit(f"{now().isoformat()} SCAN {status}")
                degraded = bool(result.exit_code)
                for item in result.instruments:
                    if item.get("status") == "error":
                        emit(
                            f"{now().isoformat()} ERROR scanner "
                            f"{item.get('instrument', 'unknown')}: "
                            f"{item.get('error', 'unavailable')}"
                        )
            except (Busy, OSError, sqlite3.OperationalError) as exc:
                degraded = True
                emit(f"{now().isoformat()} ERROR scanner: {exc}")
            # No-change and degraded scans still allow safe pending-order processing.
            paper_result = None
            try:
                paper_result = paper_tick() if paper_tick else step(config, now())
                degraded = degraded or paper_result["status"] != "success"
            except (Busy, OSError, sqlite3.OperationalError) as exc:
                degraded = True
                emit(f"{now().isoformat()} ERROR paper: {exc}")
            # DataError/unknown failures intentionally propagate: no unsafe retries.
            try:
                snapshot = checked_snapshot(config)
                activity(
                    config, snapshot, seen_orders, seen_fills, emit, now(), paper_result
                )
            except Busy as exc:
                degraded = True
                emit(f"{now().isoformat()} ERROR account summary unavailable: {exc}")
            emit(
                f"{now().isoformat()} Cycle {'degraded' if degraded else 'complete'}; "
                f"next in {interval:g}s."
            )
            wait(interval)
