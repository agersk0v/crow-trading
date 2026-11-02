"""Simulated order creation, prospective full fills, and pending reconciliation."""

import json
from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from crow_trading.backtesting.ledger import Portfolio
from crow_trading.backtesting.models import ZERO, number
from crow_trading.backtesting.risk import entry_rejection, loss_stop
from crow_trading.data.models import DataError
from crow_trading.paper.config import PAPER_VERSION, Account, Asset
from crow_trading.paper.portfolio import assets, pending, position, price, size, value
from crow_trading.paper.quotes import QUOTE_VERSION, MarketQuote, archive_quote
from crow_trading.paper.source import DailyBarAdapter, Quote, Source
from crow_trading.paper.state import State
from crow_trading.scanner.config import digest


def defer(state: State, name: str, now: datetime, key: object, error: str) -> None:
    state.event(["deferred", key, error], name, now, "deferred", {"reason": error})


def create_order(
    state: State,
    account: Account,
    asset: Asset,
    now: datetime,
    side: str,
    reason: str,
    identity: str,
    signal_id: str | None,
    reserve: Decimal = ZERO,
) -> str:
    order_id = digest([account.name, identity, side])
    if state.rows("SELECT 1 FROM orders WHERE id=?", (order_id,)):
        return order_id
    policy = {
        "delay_seconds": account.settings.delivery_delay_seconds,
        "fill_policy": account.fill_policy,
        "version": QUOTE_VERSION if account.quote_policy else PAPER_VERSION,
    }
    state.connection.execute(
        "INSERT INTO orders VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            order_id,
            account.name,
            asset.instrument.key,
            side,
            now.isoformat(),
            signal_id,
            reason,
            "pending",
            str(reserve),
            None,
            json.dumps(policy, sort_keys=True),
        ),
    )
    state.reserve(account.name)
    state.event(
        [order_id, "accepted"],
        account.name,
        now,
        "order_accepted",
        {
            "order_id": order_id,
            "signal_id": signal_id,
            "side": side,
            "reserved": str(reserve),
            "instrument_key": asset.instrument.key,
        },
    )
    return order_id


def fill_order(
    state: State,
    account: Account,
    asset: Asset,
    order: dict[str, Any],
    quote: Quote | None,
    source: Source,
    now: datetime,
    *,
    execution: MarketQuote | None = None,
) -> None:
    balance = state.balance(account.name)
    quantity, basis = position(state, account.name, asset.instrument.key)
    settings = replace(account.settings, costs=asset.costs)
    evidence: dict[str, Any] = {}
    if execution is not None:
        if account.quote_policy is None:
            raise DataError("quote execution requires a quote-policy account")
        policy = json.loads(order["policy_json"])
        if (
            policy["fill_policy"] != account.fill_policy
            or policy["version"] != QUOTE_VERSION
        ):
            raise DataError("order execution policy mismatch")
        threshold = datetime.fromisoformat(order["created_at"]) + timedelta(
            seconds=policy["delay_seconds"]
        )
        execution.validate(
            asset.instrument,
            now,
            account.quote_policy,
            source.config.mode,
            after=threshold,
        )
        identity = archive_quote(source.config.state_dir, execution)
        evidence = {
            "fill_policy": account.fill_policy,
            "quote_fingerprint": identity,
            "quote_observed_at": execution.received_at.isoformat(),
            "quote_timestamp_basis": execution.timestamp_basis,
            "bid_quote": str(execution.bid),
            "ask_quote": str(execution.ask),
        }
        state.event(
            [order["id"], "execution_quote", identity],
            account.name,
            now,
            "execution_quote",
            {"order_id": order["id"], **evidence},
        )
        stamp = now
        raw = execution.ask if order["side"] == "buy" else execution.bid
        dataset_fingerprint = None
    else:
        if quote is None or account.quote_policy is not None:
            raise DataError("daily execution requires a daily-bar account and quote")
        stamp = quote.candle.timestamp
        raw = number(quote.candle.open)
        dataset_fingerprint = quote.dataset_fingerprint
    book = source.fx(account)
    rate = book.rate(asset.instrument.currency, settings.account_currency, stamp)
    eur = book.rate("EUR", settings.account_currency, stamp)
    fill = price(raw, order["side"], settings)
    reason: str | None = None
    if order["side"] == "buy":
        if quantity:
            reason = "already_long"
        elif balance.stopped or balance.risk_halted:
            reason = "account_stopped"
        if execution is not None:
            held = sum(
                number(row["quantity"]) > 0
                for row in state.rows(
                    "SELECT quantity FROM positions WHERE account_id=?", (account.name,)
                )
            )
            slots = held + sum(o["side"] == "buy" for o in pending(state, account.name))
            if slots > account.max_positions:
                reason = "position_count_limit"
        quantity = size(
            settings.target_eur * eur, fill, rate, settings.costs.quantity_step
        )
    elif not quantity:
        reason = "not_long"
    fee = (
        quantity * fill * settings.costs.fee_rate + settings.costs.fixed_fee_quote
    ) * rate
    if order["side"] == "buy" and reason is None:
        if quantity * fill * rate + fee > number(order["reserved"]):
            reason = "reservation_exceeded"
        else:
            valuation = value(state, account, source, stamp, current=False)
            if loss_stop(
                valuation.equity, max(balance.peak, valuation.equity), settings
            ):
                state.terminal(order, "rejected", "account_risk_limit", now)
                return
            reason = entry_rejection(
                cash=number(order["reserved"]),
                quantity=quantity,
                price_quote=fill,
                rate=rate,
                fee=fee,
                raw_quote=execution.bid if execution else raw,
                position_cap=settings.max_position_eur * eur,
                settings=settings,
                equity_before=valuation.equity,
            )
            if (
                valuation.holdings + balance.reserved
                > settings.max_exposure * valuation.equity
            ):
                reason = "shared_exposure_limit"
    if order["side"] == "sell":
        if (
            quantity < settings.costs.minimum_quantity
            or quantity * fill < settings.costs.minimum_notional_quote
        ):
            reason = "exit_below_market_minimum"
        elif balance.cash - balance.reserved + quantity * fill * rate < fee:
            reason = "insufficient_cash_for_exit_fee"
    if reason:
        state.terminal(order, "rejected", reason, now)
        return
    portfolio = Portfolio(
        balance.cash, *position(state, account.name, asset.instrument.key)
    )
    old_basis = basis
    old_quantity = portfolio.quantity
    pnl = ZERO
    reference = (execution.bid + execution.ask) / 2 if execution else raw
    impact = quantity * abs(fill - reference) * rate
    if order["side"] == "buy":
        portfolio.buy(quantity, fill * rate, fee, impact)
    else:
        pnl = portfolio.sell(fill * rate, fee, impact)
    payload = {
        "order_id": order["id"],
        "account_id": account.name,
        "instrument_key": asset.instrument.key,
        "symbol": asset.instrument.symbol,
        "side": order["side"],
        "effective_at": stamp.isoformat(),
        "recorded_at": now.isoformat(),
        "quantity": str(quantity),
        "quantity_delta": str(portfolio.quantity - old_quantity),
        "basis_delta": str(portfolio.basis - old_basis),
        "raw_price_quote": str(raw),
        "fill_price_quote": str(fill),
        "quote_currency": asset.instrument.currency,
        "account_currency": settings.account_currency,
        "quote_to_account_fx": str(rate),
        "fee_account": str(fee),
        "execution_impact_account": str(impact),
        "realized_pnl": str(pnl),
        "cash_delta": str(portfolio.cash - balance.cash),
        "cash_after": str(portfolio.cash),
        "dataset_fingerprint": dataset_fingerprint,
        **evidence,
        "fx_fingerprint": source.fx_fingerprint,
        "action_fingerprints": sorted(source.actions),
    }
    state.connection.execute(
        "INSERT INTO fills VALUES (?,?)",
        (order["id"], json.dumps(payload, sort_keys=True)),
    )
    state.connection.execute(
        "UPDATE orders SET status='filled',terminal_at=? WHERE id=?",
        (now.isoformat(), order["id"]),
    )
    state.connection.execute(
        "UPDATE accounts SET cash=? WHERE id=?", (str(portfolio.cash), account.name)
    )
    state.connection.execute(
        "INSERT OR REPLACE INTO positions VALUES (?,?,?,?)",
        (
            account.name,
            asset.instrument.key,
            str(portfolio.quantity),
            str(portfolio.basis),
        ),
    )
    state.reserve(account.name)
    state.event(
        [order["id"], "fill"],
        account.name,
        now,
        "fill",
        payload,
        portfolio.cash - balance.cash,
    )


def reconcile_orders(
    state: State, account: Account, source: Source, now: datetime
) -> list[str]:
    configured = assets(account)
    errors: list[str] = []
    ready: list[tuple[dict[str, Any], Quote]] = []
    by_id = {record["id"]: record for record in source.signals}
    for order in pending(state, account.name):
        try:
            original = by_id.get(order["signal_id"])
            if original:
                logical = (
                    original["instrument_key"],
                    original["candle_timestamp"],
                    original["strategy_key"],
                )
                if source.latest_signals[logical] != original["id"]:
                    state.terminal(order, "cancelled", "signal_superseded", now)
                    continue
            quote = DailyBarAdapter().next_quote(
                order, configured[order["instrument_key"]], source
            )
            if quote:
                ready.append((order, quote))
        except DataError as exc:
            errors.append(str(exc))
            defer(
                state, account.name, now, [order["id"], source.fx_fingerprint], str(exc)
            )
    for order, quote in sorted(
        ready,
        key=lambda pair: (
            pair[1].candle.timestamp,
            pair[0]["side"] == "buy",
            pair[0]["created_at"],
            pair[0]["id"],
        ),
    ):
        try:
            fill_order(
                state,
                account,
                configured[order["instrument_key"]],
                order,
                quote,
                source,
                now,
            )
        except DataError as exc:
            errors.append(str(exc))
            defer(
                state,
                account.name,
                now,
                [order["id"], quote.dataset_fingerprint, source.fx_fingerprint],
                str(exc),
            )
    return errors
