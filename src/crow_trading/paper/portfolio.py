"""Shared portfolio queries, price arithmetic, and timestamped valuations."""

import json
from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_DOWN, Decimal
from typing import Any

from crow_trading.backtesting.models import ZERO, Settings, number
from crow_trading.paper.config import Account, Asset
from crow_trading.paper.source import Source
from crow_trading.paper.state import State


@dataclass(frozen=True)
class Valuation:
    equity: Decimal
    holdings: Decimal
    unrealized: Decimal
    marks: dict[str, Any]


def assets(account: Account) -> dict[str, Asset]:
    return {a.instrument.key: a for a in account.assets}


def pending(state: State, name: str) -> list[dict[str, Any]]:
    return state.rows(
        "SELECT * FROM orders WHERE account_id=? AND status='pending' "
        "ORDER BY created_at,id",
        (name,),
    )


def position(state: State, name: str, key: str) -> tuple[Decimal, Decimal]:
    rows = state.rows(
        "SELECT * FROM positions WHERE account_id=? AND instrument_key=?", (name, key)
    )
    return (
        (number(rows[0]["quantity"]), number(rows[0]["basis"]))
        if rows
        else (ZERO, ZERO)
    )


def price(raw: Decimal, side: str, settings: Settings) -> Decimal:
    impact = settings.costs.spread_bps / 20000 + settings.costs.slippage_bps / 10000
    return raw * (1 + impact if side == "buy" else 1 - impact)


def size(target: Decimal, fill: Decimal, rate: Decimal, step: Decimal) -> Decimal:
    return (target / (fill * rate) / step).to_integral_value(rounding=ROUND_DOWN) * step


def value(
    state: State, account: Account, source: Source, when: datetime, *, current: bool
) -> Valuation:
    holdings = basis = ZERO
    marks: dict[str, Any] = {}
    configured = assets(account)
    book = source.fx(account)
    for row in state.rows(
        "SELECT * FROM positions WHERE account_id=?", (account.name,)
    ):
        quantity = number(row["quantity"])
        if not quantity:
            continue
        asset = configured[row["instrument_key"]]
        quote = source.mark(
            asset, when, current=current, max_age=account.mark_max_age_seconds
        )
        rate = book.rate(
            asset.instrument.currency, account.settings.account_currency, when
        )
        amount = (
            quantity
            * number(
                quote.mark_price if quote.mark_price is not None else quote.candle.close
            )
            * rate
        )
        holdings += amount
        basis += number(row["basis"])
        marks[asset.instrument.key] = {
            "symbol": asset.instrument.symbol,
            "quantity": str(quantity),
            "value": str(amount),
            "basis": row["basis"],
            "quote_to_account_fx": str(rate),
            "candle_timestamp": quote.candle.timestamp.isoformat(),
            "price_time": quote.mark_time.isoformat()
            if quote.mark_time
            else quote.candle.closed_at.isoformat(),
            "dataset_fingerprint": quote.dataset_fingerprint,
        }
    cash = state.balance(account.name).cash
    return Valuation(cash + holdings, holdings, holdings - basis, marks)


def record_valuation(
    state: State, account: Account, source: Source, now: datetime, valuation: Valuation
) -> None:
    closed_fills = [
        json.loads(row["payload"])
        for row in state.rows(
            "SELECT f.payload FROM fills f JOIN orders o ON o.id=f.order_id "
            "WHERE o.account_id=? ORDER BY f.rowid",
            (account.name,),
        )
    ]
    payload = {
        "realized_pnl": str(
            sum((number(f["realized_pnl"]) for f in closed_fills), ZERO)
        ),
        "fees_paid": str(sum((number(f["fee_account"]) for f in closed_fills), ZERO)),
        "execution_impact_paid": str(
            sum(
                (number(f["execution_impact_account"]) for f in closed_fills),
                ZERO,
            )
        ),
        "net_pnl": str(valuation.equity - account.settings.initial_cash),
        "equity": str(valuation.equity),
        "cash": str(state.balance(account.name).cash),
        "holdings": str(valuation.holdings),
        "unrealized_pnl": str(valuation.unrealized),
        "marks": valuation.marks,
        "fx_fingerprint": source.fx_fingerprint,
    }
    state.event(
        [account.name, "valuation", payload],
        account.name,
        now,
        "valuation",
        payload,
    )
    state.connection.execute(
        "INSERT OR REPLACE INTO valuations VALUES (?,?)",
        (
            account.name,
            json.dumps({**payload, "as_of": now.isoformat()}, sort_keys=True),
        ),
    )
