"""Shared buying-power checks and persistent account risk exits."""

from dataclasses import replace
from datetime import datetime
from decimal import Decimal

from crow_trading.backtesting.models import ZERO, number
from crow_trading.backtesting.risk import entry_rejection, loss_stop
from crow_trading.paper.config import Account, Asset
from crow_trading.paper.orders import create_order
from crow_trading.paper.portfolio import (
    Valuation,
    assets,
    pending,
    position,
    price,
    size,
)
from crow_trading.paper.source import Source
from crow_trading.paper.state import State
from crow_trading.scanner.config import digest


def entry_reservation(
    state: State,
    account: Account,
    asset: Asset,
    source: Source,
    now: datetime,
    valuation: Valuation,
) -> tuple[Decimal, str | None]:
    balance = state.balance(account.name)
    if balance.stopped or balance.risk_halted:
        return ZERO, "account_stopped"
    orders = pending(state, account.name)
    if any(order["reason"].startswith("risk:") for order in orders):
        return ZERO, "risk_exit_pending"
    held = sum(
        number(row["quantity"]) > 0
        for row in state.rows(
            "SELECT quantity FROM positions WHERE account_id=?", (account.name,)
        )
    )
    if held + sum(order["side"] == "buy" for order in orders) >= account.max_positions:
        return ZERO, "position_count_limit"
    settings = replace(account.settings, costs=asset.costs)
    book = source.fx(account)
    rate = book.rate(asset.instrument.currency, settings.account_currency, now)
    eur = book.rate("EUR", settings.account_currency, now)
    target = settings.target_eur * eur
    raw = source.entry_price(asset, now, account.mark_max_age_seconds)
    fill = price(raw, "buy", settings)
    quantity = size(target, fill, rate, settings.costs.quantity_step)
    fee = (
        quantity * fill * settings.costs.fee_rate + settings.costs.fixed_fee_quote
    ) * rate
    # Reserve the full target/fee budget; no borrowing from later cash inflows.
    reserve = (
        target * (1 + settings.costs.fee_rate) + settings.costs.fixed_fee_quote * rate
    )
    reason = entry_rejection(
        cash=balance.cash - balance.reserved,
        quantity=quantity,
        price_quote=fill,
        rate=rate,
        fee=fee,
        raw_quote=(
            source.valuation_mark(
                asset, now, current=True, max_age=account.mark_max_age_seconds
            ).price
            if account.quote_policy
            else raw
        ),
        position_cap=settings.max_position_eur * eur,
        settings=settings,
        equity_before=valuation.equity,
    )
    if reason:
        return ZERO, reason
    if reserve > balance.cash - balance.reserved:
        return ZERO, "insufficient_unreserved_cash"
    if (
        valuation.holdings + balance.reserved + reserve
        > settings.max_exposure * valuation.equity
    ):
        return ZERO, "shared_exposure_limit"
    return reserve, None


def risk(state: State, account: Account, valuation: Valuation, now: datetime) -> None:
    balance = state.balance(account.name)
    peak = max(balance.peak, valuation.equity)
    stop = loss_stop(valuation.equity, peak, account.settings)
    if stop:
        state.connection.execute(
            "UPDATE accounts SET risk_halted=1,risk_reason=? WHERE id=?",
            (stop, account.name),
        )
    state.connection.execute(
        "UPDATE accounts SET peak=? WHERE id=?", (str(peak), account.name)
    )
    reason = (
        stop
        or balance.risk_reason
        or (
            "exposure_limit"
            if valuation.holdings > account.settings.max_exposure * valuation.equity
            else ""
        )
    )
    if not reason:
        return
    for order in pending(state, account.name):
        if order["side"] == "buy":
            state.terminal(order, "cancelled", "risk_halted", now)
    current = {order["instrument_key"] for order in pending(state, account.name)}
    for key, asset in assets(account).items():
        quantity, _ = position(state, account.name, key)
        if quantity and key not in current:
            identity = digest(
                ["risk", key, reason, valuation.marks, str(valuation.equity)]
            )
            create_order(
                state, account, asset, now, "sell", "risk:" + reason, identity, None
            )
