"""Focused runner summaries and session-local activity cursors.

Full ledger reconciliation is retained; reporting does not copy audit tables.
Cursors describe console delivery only and never acknowledge trading signals.
"""

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from crow_trading.paper.config import Config
from crow_trading.paper.state import State


@dataclass(frozen=True)
class Cursor:
    order: int
    fill: int


@dataclass(frozen=True)
class Snapshot:
    accounts: list[dict[str, Any]]
    valuations: list[dict[str, Any]]
    orders: list[dict[str, Any]]
    fills: list[dict[str, Any]]
    cursor: Cursor


def reconcile_accounts(state: State) -> None:
    # Include historical/unconfigured accounts, as full inspection did previously.
    for row in state.rows("SELECT id FROM accounts ORDER BY rowid"):
        state.reconcile(row["id"])


def check_accounts(config: Config) -> None:
    with State(config.state_dir) as state:
        reconcile_accounts(state)


def snapshot(config: Config, cursor: Cursor | None = None) -> Snapshot:
    with State(config.state_dir) as state:
        reconcile_accounts(state)
        high = Cursor(
            state.connection.execute(
                "SELECT COALESCE(MAX(rowid),0) FROM orders"
            ).fetchone()[0],
            state.connection.execute(
                "SELECT COALESCE(MAX(rowid),0) FROM fills"
            ).fetchone()[0],
        )
        names: tuple[object, ...] = tuple(a.name for a in config.accounts)
        placeholders = ",".join("?" for _ in names)
        accounts = state.rows(
            "SELECT a.id,a.cash,a.reserved,a.stopped,a.risk_halted,"
            "(SELECT COUNT(*) FROM orders o WHERE o.account_id=a.id "
            "AND o.status='pending') AS pending_orders "
            f"FROM accounts a WHERE a.id IN ({placeholders}) ORDER BY a.rowid",
            names,
        )
        valuations = state.rows(
            f"SELECT * FROM valuations WHERE account_id IN ({placeholders}) "
            "ORDER BY rowid",
            names,
        )
        # None means startup: ignore existing history without loading its payloads.
        orders = (
            state.rows(
                "SELECT id,account_id,instrument_key,side,status FROM orders "
                f"WHERE rowid>? AND rowid<=? AND account_id IN ({placeholders}) "
                "ORDER BY rowid",
                (cursor.order, high.order, *names),
            )
            if cursor is not None
            else []
        )
        fills = (
            state.rows(
                "SELECT f.payload FROM fills f JOIN orders o ON o.id=f.order_id "
                "WHERE f.rowid>? AND f.rowid<=? "
                f"AND o.account_id IN ({placeholders}) ORDER BY f.rowid",
                (cursor.fill, high.fill, *names),
            )
            if cursor is not None
            else []
        )
        return Snapshot(accounts, valuations, orders, fills, high)


def activity(
    config: Config,
    snapshot: Snapshot,
    emit: Callable[[str], None],
    now: datetime,
    paper_result: dict[str, Any] | None,
) -> None:
    prefix = now.isoformat()
    symbols = {
        a.instrument.key: a.instrument.symbol for c in config.accounts for a in c.assets
    }
    new_fills = [json.loads(r["payload"]) for r in snapshot.fills]
    for order in snapshot.orders:
        emit(
            f"{prefix} ORDER {order['account_id']} {order['side'].upper()} "
            f"{symbols.get(order['instrument_key'], order['instrument_key'])} "
            f"status={order['status']} id={order['id']} "
            "(simulated order, not a fill)"
        )
    for fill in new_fills:
        quote_detail = (
            f" quote_at={fill['quote_observed_at']} "
            f"basis={fill['quote_timestamp_basis']}"
            if "quote_fingerprint" in fill
            else ""
        )
        emit(
            f"{prefix} FILL {fill['account_id']} {fill['side'].upper()} "
            f"{fill['quantity']} {fill['symbol']} at {fill['fill_price_quote']} "
            f"{fill['quote_currency']} fee={fill['fee_account']} "
            f"{fill['account_currency']} "
            f"effective_at={fill['effective_at']} "
            f"recorded_at={fill['recorded_at']}{quote_detail}"
        )
    emit(f"{prefix} Trades: {len(new_fills)} new simulated fills.")
    valuations = {
        r["account_id"]: json.loads(r["payload"]) for r in snapshot.valuations
    }
    results = {r["account"]: r for r in (paper_result or {}).get("accounts", [])}
    for account in snapshot.accounts:
        name = account["id"]
        valuation = valuations.get(name)
        current = bool(
            valuation
            and valuation["as_of"]
            == results.get(name, {}).get(
                "valuation_as_of", (paper_result or {}).get("as_of")
            )
        )
        value = (
            f"holdings={valuation['holdings']} total={valuation['equity']} "
            f"valuation_at={valuation['as_of']} {'current' if current else 'stale'}"
            if valuation
            else "holdings=unavailable total=unavailable valuation=unavailable"
        )
        if valuation:
            quote_marks = [
                m for m in valuation["marks"].values() if "quote_fingerprint" in m
            ]
            if quote_marks:
                bases = ",".join(
                    sorted({m["quote_timestamp_basis"] for m in quote_marks})
                )
                value += (
                    f" quote_at={min(m['price_time'] for m in quote_marks)}"
                    f" basis={bases}"
                    " mark_side=bid"
                )
        currency = next(
            a.settings.account_currency for a in config.accounts if a.name == name
        )
        pending = account["pending_orders"]
        emit(
            f"{prefix} ACCOUNT {name} ({currency}) cash={account['cash']} "
            f"reserved={account['reserved']} {value} pending={pending} "
            f"stopped={bool(account['stopped'])} "
            f"risk_halted={bool(account['risk_halted'])}"
        )
        for error in results.get(name, {}).get("errors", []):
            emit(f"{prefix} ERROR {name}: {error}")
