"""Sequential, restartable cycles; no changes to strategy or fill semantics."""

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
from crow_trading.runner.reporting import activity, check_accounts, snapshot
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
        cursor = snapshot(config).cursor
        emit(
            f"{now().isoformat()} Runner started: simulated funds only; "
            f"interval={interval:g}s."
        )
        while True:
            # Revalidate immutable policies, clock, and ledger before market work.
            try:
                initialize(config, now())
                check_accounts(config)
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
                summary = (
                    result.report.get("failure_summary") if result.report else None
                )
                detail = (
                    f" failures={summary['occurrences']} since={summary['first_seen']}"
                    if summary
                    else ""
                )
                emit(f"{now().isoformat()} SCAN {status}{detail}")
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
                report = snapshot(config, cursor)
                activity(config, report, emit, now(), paper_result)
                cursor = report.cursor
            except Busy as exc:
                degraded = True
                emit(f"{now().isoformat()} ERROR account summary unavailable: {exc}")
            emit(
                f"{now().isoformat()} Cycle {'degraded' if degraded else 'complete'}; "
                f"next in {interval:g}s."
            )
            wait(interval)
