"""Deterministic single-asset, next-open simulations with isolated cash state."""

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import ROUND_DOWN, Decimal
from typing import Any
from zoneinfo import ZoneInfo

from crow_trading.backtesting.ledger import Portfolio
from crow_trading.backtesting.market import FXBook, check_actions
from crow_trading.backtesting.models import ZERO, Settings, Window, number
from crow_trading.backtesting.risk import entry_rejection, loss_stop
from crow_trading.data.models import DataError, Dataset
from crow_trading.data.normalize import validate
from crow_trading.strategies.base import Strategy
from crow_trading.strategies.runner import replay


@dataclass(frozen=True)
class Pending:
    intent: str
    reason: str
    available_at: datetime
    candle_timestamp: datetime
    index: int
    forced: bool = False


@dataclass(frozen=True)
class Simulation:
    window: Window
    ledger: tuple[dict[str, str], ...]
    equity: tuple[dict[str, str], ...]
    closed_pnls: tuple[Decimal, ...]
    fees: Decimal
    impact: Decimal
    turnover: Decimal


def simulate(
    dataset: Dataset,
    strategy: Strategy,
    settings: Settings,
    window: Window,
    fx: FXBook,
    *,
    buy_and_hold: bool = False,
    actions: dict[str, Any] | None = None,
) -> Simulation:
    validate(dataset)
    check_actions(dataset, actions)
    req = dataset.metadata.request
    if not req.start <= window.start < window.end <= req.end:
        raise DataError("evaluation window lies outside cached coverage")
    timezone = ZoneInfo(dataset.metadata.timezone)
    indices = [
        i
        for i, c in enumerate(dataset.candles)
        if window.start <= c.timestamp.astimezone(timezone).date() < window.end
    ]
    if not indices:
        raise DataError(f"no sessions in {window.name} window")
    # Fail on missing/stale FX even if the strategy makes no trades.
    for i in indices:
        for stamp in (dataset.candles[i].timestamp, dataset.candles[i].closed_at):
            fx.rate(req.currency, settings.account_currency, stamp)
            fx.rate("EUR", settings.account_currency, stamp)
    signals = {s.candle_timestamp: s for s in replay(dataset, strategy)}
    portfolio = Portfolio(settings.initial_cash)
    peak = settings.initial_cash
    halted = False
    pending: list[Pending] = []
    ledger: list[dict[str, str]] = []
    equity: list[dict[str, str]] = []
    pnls: list[Decimal] = []
    turnover = ZERO
    delay = timedelta(seconds=settings.delivery_delay_seconds)

    def log(
        stamp: datetime,
        order: Pending,
        status: str,
        reason: str,
        *,
        old_cash: Decimal,
        old_quantity: Decimal,
        quantity: Decimal = ZERO,
        raw: Decimal = ZERO,
        price: Decimal = ZERO,
        rate: Decimal = ZERO,
        fee: Decimal = ZERO,
        pnl: Decimal = ZERO,
        impact: Decimal = ZERO,
    ) -> None:
        ledger.append(
            {
                "timestamp": stamp.isoformat(),
                "symbol": req.symbol,
                "quote_currency": req.currency,
                "account_currency": settings.account_currency,
                "signal_timestamp": order.candle_timestamp.isoformat(),
                "available_at": order.available_at.isoformat(),
                "intent": order.intent,
                "status": status,
                "reason": reason,
                "order_quantity": str(quantity),
                "raw_price_quote": str(raw),
                "fill_price_quote": str(price),
                "quote_to_account_fx": str(rate),
                "fee_account": str(fee),
                "execution_impact_account": str(impact),
                "realized_pnl_delta": str(pnl),
                "cash_delta": str(portfolio.cash - old_cash),
                "quantity_delta": str(portfolio.quantity - old_quantity),
                "cash_after": str(portfolio.cash),
                "quantity_after": str(portfolio.quantity),
                "cost_basis_account": str(portfolio.basis),
            }
        )

    first = dataset.candles[indices[0]]
    equity.append(
        {
            "timestamp": first.timestamp.isoformat(),
            "cash": str(portfolio.cash),
            "quantity": "0",
            "holding_value": "0",
            "equity": str(portfolio.cash),
            "realized_pnl": "0",
            "unrealized_pnl": "0",
            "drawdown": "0",
            "exposed": "0",
            "kind": "initial",
        }
    )
    if buy_and_hold:
        pending.append(
            Pending(
                "enter_long",
                "benchmark_initial_allocation",
                first.timestamp - delay,
                first.timestamp,
                indices[0] - 1,
            )
        )
    for index in indices:
        candle = dataset.candles[index]
        raw = number(candle.open)
        rate = fx.rate(req.currency, settings.account_currency, candle.timestamp)
        eur_to_account = fx.rate("EUR", settings.account_currency, candle.timestamp)
        remaining: list[Pending] = []
        for order in pending:
            if index <= order.index or candle.timestamp < order.available_at + delay:
                remaining.append(order)
                continue
            old_cash, old_quantity = portfolio.cash, portfolio.quantity
            reason: str | None = None
            fee = pnl = impact = quantity = ZERO
            price = raw
            if order.intent == "enter_long":
                if halted:
                    reason = "risk_halted"
                elif portfolio.quantity:
                    reason = "already_long"
                else:
                    price = raw * (
                        1
                        + settings.costs.spread_bps / 20000
                        + settings.costs.slippage_bps / 10000
                    )
                    target = settings.target_eur * eur_to_account
                    step = settings.costs.quantity_step
                    quantity = (target / (price * rate) / step).to_integral_value(
                        rounding=ROUND_DOWN
                    ) * step
                    fee = (
                        quantity * price * settings.costs.fee_rate
                        + settings.costs.fixed_fee_quote
                    ) * rate
                    reason = entry_rejection(
                        cash=portfolio.cash,
                        quantity=quantity,
                        price_quote=price,
                        rate=rate,
                        fee=fee,
                        raw_quote=raw,
                        position_cap=settings.max_position_eur * eur_to_account,
                        settings=settings,
                    )
                    if reason is None:
                        impact = quantity * (price - raw) * rate
                        portfolio.buy(quantity, price * rate, fee, impact)
            elif order.intent == "exit_long":
                if not portfolio.quantity:
                    reason = "not_long"
                else:
                    quantity = portfolio.quantity
                    price = raw * (
                        1
                        - settings.costs.spread_bps / 20000
                        - settings.costs.slippage_bps / 10000
                    )
                    fee = (
                        quantity * price * settings.costs.fee_rate
                        + settings.costs.fixed_fee_quote
                    ) * rate
                    if (
                        quantity < settings.costs.minimum_quantity
                        or quantity * price < settings.costs.minimum_notional_quote
                    ):
                        reason = "exit_below_market_minimum"
                    elif portfolio.cash + quantity * price * rate < fee:
                        reason = "insufficient_cash_for_exit_fee"
                    else:
                        impact = quantity * (raw - price) * rate
                        pnl = portfolio.sell(price * rate, fee, impact)
                        pnls.append(pnl)
            else:
                raise DataError(f"unknown execution intent: {order.intent}")
            if reason is None:
                turnover += quantity * price * rate
                log(
                    candle.timestamp,
                    order,
                    "filled",
                    order.reason,
                    old_cash=old_cash,
                    old_quantity=old_quantity,
                    quantity=quantity,
                    raw=raw,
                    price=price,
                    rate=rate,
                    fee=fee,
                    pnl=pnl,
                    impact=impact,
                )
            else:
                log(
                    candle.timestamp,
                    order,
                    "rejected",
                    reason,
                    old_cash=old_cash,
                    old_quantity=old_quantity,
                    quantity=quantity,
                    raw=raw,
                    price=price,
                    rate=rate,
                )
            if portfolio.cash < 0 or portfolio.quantity < 0:
                raise DataError("accounting invariant violated: negative cash/quantity")
        pending = remaining
        close_rate = fx.rate(req.currency, settings.account_currency, candle.closed_at)
        mark = number(candle.close) * close_rate
        current_equity = portfolio.equity(mark)
        peak = max(peak, current_equity)
        drawdown = (peak - current_equity) / peak
        equity.append(
            {
                "timestamp": candle.closed_at.isoformat(),
                "cash": str(portfolio.cash),
                "quantity": str(portfolio.quantity),
                "holding_value": str(portfolio.quantity * mark),
                "equity": str(current_equity),
                "realized_pnl": str(portfolio.realized),
                "unrealized_pnl": str(portfolio.quantity * mark - portfolio.basis),
                "drawdown": str(drawdown),
                "exposed": str(int(portfolio.quantity > 0)),
                "kind": "close",
            }
        )
        if buy_and_hold:
            continue
        stop = loss_stop(current_equity, peak, settings)
        if stop:
            halted = True
        exposure_stop = (
            portfolio.quantity * mark > settings.max_exposure * current_equity
        )
        forced_reason = stop or ("exposure_limit" if exposure_stop else None)
        if forced_reason:
            retained: list[Pending] = []
            for order in pending:
                if order.forced:
                    retained.append(order)
                else:
                    log(
                        candle.closed_at,
                        order,
                        "cancelled",
                        "cancelled_by_risk",
                        old_cash=portfolio.cash,
                        old_quantity=portfolio.quantity,
                    )
            pending = retained
            if portfolio.quantity and not pending:
                pending.append(
                    Pending(
                        "exit_long",
                        forced_reason,
                        candle.closed_at,
                        candle.timestamp,
                        index,
                        True,
                    )
                )
        risk_exit_pending = any(order.forced for order in pending)
        signal = signals.get(candle.timestamp)
        if signal is not None:
            order = Pending(
                signal.intent,
                signal.reason,
                signal.available_at,
                signal.candle_timestamp,
                index,
            )
            if signal.intent == "hold" or risk_exit_pending or halted:
                log(
                    candle.closed_at,
                    order,
                    "skipped",
                    "risk_halted"
                    if halted
                    else ("risk_exit_pending" if risk_exit_pending else signal.reason),
                    old_cash=portfolio.cash,
                    old_quantity=portfolio.quantity,
                )
            else:
                pending.append(order)
    final = dataset.candles[indices[-1]]
    for order in pending:
        log(
            final.closed_at,
            order,
            "unfilled",
            "no_eligible_open_in_window",
            old_cash=portfolio.cash,
            old_quantity=portfolio.quantity,
        )
    return Simulation(
        window,
        tuple(ledger),
        tuple(equity),
        tuple(pnls),
        portfolio.fees,
        portfolio.impact,
        turnover,
    )
