"""Transactional simulated orders, shared cash/risk, and restart reconciliation."""

import json
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from decimal import ROUND_DOWN, Decimal
from typing import Any

from crow_trading.backtesting.ledger import Portfolio
from crow_trading.backtesting.models import ZERO, Settings, number
from crow_trading.backtesting.risk import entry_rejection, loss_stop
from crow_trading.data.models import DataError, utc
from crow_trading.paper.config import PAPER_VERSION, Account, Asset, Config
from crow_trading.paper.source import DailyBarAdapter, Quote, Source
from crow_trading.paper.state import State
from crow_trading.scanner.config import digest
from crow_trading.scanner.datasets import context_hash


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
        "fill_policy": "next_closed_daily_open",
        "version": PAPER_VERSION,
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


def eligibility(
    record: dict[str, Any],
    account: Account,
    asset: Asset,
    source: Source,
    now: datetime,
    mode: str,
) -> str | None:
    if record["classification"] != "fresh":
        return "non_fresh_signal"
    if (record["data_mode"] == "snapshot") != (mode == "fixture"):
        return "fixture_mode_mismatch"
    if (
        record["identity"] != asset.instrument.identity()
        or record["strategy"] != account.strategy
    ):
        return "signal_identity_mismatch"
    opened = utc(datetime.fromisoformat(record["candle_timestamp"]))
    available = utc(datetime.fromisoformat(record["available_at"]))
    evaluated = utc(datetime.fromisoformat(record["evaluated_as_of"]))
    if not opened < available <= evaluated <= now:
        return "invalid_signal_timing"
    expires = min(
        utc(datetime.fromisoformat(record["expires_at"])),
        available + timedelta(seconds=account.signal_max_age_seconds),
    )
    if now >= expires:
        return "expired_signal"
    logical = (
        record["instrument_key"],
        record["candle_timestamp"],
        record["strategy_key"],
    )
    if source.latest_signals[logical] != record["id"]:
        return "superseded_signal"
    data = source.data(
        asset, path=record["snapshot_path"], expected_fp=record["dataset_fingerprint"]
    )
    index = next(
        (i for i, candle in enumerate(data.candles) if candle.timestamp == opened), None
    )
    if index is None or data.candles[index].closed_at != available:
        return "signal_candle_mismatch"
    selected = next(
        s
        for s in asset.instrument.strategies
        if digest(
            {"id": s.strategy_id, "version": s.version, "parameters": s.parameters()}
        )
        == record["strategy_key"]
    )
    history = data.candles[max(0, index + 1 - selected.history_required) : index + 1]
    if context_hash(asset.instrument, history) != record["signal_input_fingerprint"]:
        return "signal_input_mismatch"
    expected = selected.evaluate(asset.instrument.symbol, history, as_of=available)
    if (
        expected is None
        or expected.intent != record["intent"]
        or expected.reason != record["reason"]
    ):
        return "signal_output_mismatch"
    if (
        digest(
            [
                asset.instrument.key,
                opened.isoformat(),
                record["strategy_key"],
                record["signal_input_fingerprint"],
            ]
        )
        != record["id"]
    ):
        return "signal_id_mismatch"
    return None


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
    quote = source.mark(asset, now, current=True, max_age=account.mark_max_age_seconds)
    book = source.fx(account)
    rate = book.rate(asset.instrument.currency, settings.account_currency, now)
    eur = book.rate("EUR", settings.account_currency, now)
    target = settings.target_eur * eur
    raw = number(quote.candle.close)
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
        raw_quote=raw,
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


def fill_order(
    state: State,
    account: Account,
    asset: Asset,
    order: dict[str, Any],
    quote: Quote,
    source: Source,
    now: datetime,
) -> None:
    balance = state.balance(account.name)
    quantity, basis = position(state, account.name, asset.instrument.key)
    settings = replace(account.settings, costs=asset.costs)
    stamp = quote.candle.timestamp
    book = source.fx(account)
    rate = book.rate(asset.instrument.currency, settings.account_currency, stamp)
    eur = book.rate("EUR", settings.account_currency, stamp)
    raw = number(quote.candle.open)
    fill = price(raw, order["side"], settings)
    reason: str | None = None
    if order["side"] == "buy":
        if quantity:
            reason = "already_long"
        elif balance.stopped or balance.risk_halted:
            reason = "account_stopped"
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
                raw_quote=raw,
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
    impact = quantity * abs(fill - raw) * rate
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
        "dataset_fingerprint": quote.dataset_fingerprint,
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


def initialize(config: Config, now: datetime) -> dict[str, Any]:
    now = utc(now)
    with State(config.state_dir, create=True) as state:
        for account in config.accounts:
            state.ensure(config, account, now, initialize=True)
        state.commit()
    return {
        "status": "initialized",
        "mode": config.mode,
        "accounts": [a.name for a in config.accounts],
    }


def step(config: Config, now: datetime | None = None) -> dict[str, Any]:
    now = utc(now or datetime.now(UTC))
    result: list[dict[str, Any]] = []
    with State(config.state_dir) as state:
        for account in config.accounts:
            state.ensure(config, account, now, initialize=False)
        source = Source(config, now)
        for account in config.accounts:
            configured = assets(account)
            errors = reconcile_orders(state, account, source, now)
            state.reconcile(account.name)
            valuation: Valuation | None = None
            try:
                valuation = value(state, account, source, now, current=True)
                risk(state, account, valuation, now)
                closed_fills = [
                    json.loads(row["payload"])
                    for row in state.rows("SELECT payload FROM fills ORDER BY rowid")
                    if json.loads(row["payload"])["account_id"] == account.name
                ]
                payload = {
                    "realized_pnl": str(
                        sum((number(f["realized_pnl"]) for f in closed_fills), ZERO)
                    ),
                    "fees_paid": str(
                        sum((number(f["fee_account"]) for f in closed_fills), ZERO)
                    ),
                    "execution_impact_paid": str(
                        sum(
                            (
                                number(f["execution_impact_account"])
                                for f in closed_fills
                            ),
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
                        json.dumps(
                            {**payload, "as_of": now.isoformat()}, sort_keys=True
                        ),
                    ),
                )
            except DataError as exc:
                errors.append(str(exc))
                defer(
                    state,
                    account.name,
                    now,
                    ["valuation", source.fx_fingerprint],
                    str(exc),
                )
            for record in source.signals:
                if record["instrument_key"] not in configured or record[
                    "strategy_key"
                ] != digest(account.strategy):
                    continue
                known = state.rows(
                    "SELECT status FROM decisions WHERE account_id=? AND signal_id=?",
                    (account.name, record["id"]),
                )
                if known and known[0]["status"] != "deferred":
                    continue
                asset = configured[record["instrument_key"]]
                reason: str | None = None
                order_id: str | None = None
                temporary = False
                try:
                    reason = eligibility(
                        record, account, asset, source, now, config.mode
                    )
                    if reason is None and record["intent"] == "hold":
                        reason = "hold:" + record["reason"]
                    existing = next(
                        (
                            o
                            for o in pending(state, account.name)
                            if o["instrument_key"] == asset.instrument.key
                        ),
                        None,
                    )
                    quantity, _ = position(state, account.name, asset.instrument.key)
                    if reason is None and record["intent"] == "enter_long":
                        if existing:
                            reason = "pending_order"
                        elif quantity:
                            reason = "already_long"
                        elif valuation is None:
                            reason = "valuation_unavailable"
                            temporary = True
                        else:
                            reserve, reason = entry_reservation(
                                state, account, asset, source, now, valuation
                            )
                            if reason is None:
                                order_id = create_order(
                                    state,
                                    account,
                                    asset,
                                    now,
                                    "buy",
                                    "strategy_entry",
                                    record["id"],
                                    record["id"],
                                    reserve,
                                )
                    elif reason is None and record["intent"] == "exit_long":
                        if existing and existing["side"] == "buy":
                            state.terminal(
                                existing, "cancelled", "strategy_exit_before_fill", now
                            )
                            reason = "pending_entry_cancelled"
                        elif existing:
                            reason = "pending_order"
                        elif not quantity:
                            reason = "not_long"
                        else:
                            order_id = create_order(
                                state,
                                account,
                                asset,
                                now,
                                "sell",
                                "strategy_exit",
                                record["id"],
                                record["id"],
                            )
                    elif reason is None:
                        reason = "unsupported_intent"
                except DataError as exc:
                    reason = "input_unavailable:" + str(exc)
                    temporary = True
                    errors.append(str(exc))
                decision_payload: dict[str, Any] = {
                    "signal": record,
                    "received_at": now.isoformat(),
                    "order_id": order_id,
                }
                state.connection.execute(
                    "INSERT OR REPLACE INTO decisions VALUES (?,?,?,?,?)",
                    (
                        account.name,
                        record["id"],
                        "accepted"
                        if order_id
                        else ("deferred" if temporary else "ignored"),
                        reason or "order_created",
                        json.dumps(decision_payload, sort_keys=True),
                    ),
                )
                state.event(
                    [
                        account.name,
                        "decision",
                        record["id"],
                        reason,
                        source.fx_fingerprint,
                    ],
                    account.name,
                    now,
                    "decision",
                    {
                        "signal_id": record["id"],
                        "reason": reason or "order_created",
                        "order_id": order_id,
                    },
                )
            state.connection.execute(
                "UPDATE accounts SET last_clock=? WHERE id=?",
                (now.isoformat(), account.name),
            )
            state.reconcile(account.name)
            balance = state.balance(account.name)
            result.append(
                {
                    "account": account.name,
                    "cash": str(balance.cash),
                    "reserved": str(balance.reserved),
                    "stopped": balance.stopped,
                    "risk_halted": balance.risk_halted,
                    "pending_orders": len(pending(state, account.name)),
                    "errors": sorted(set(errors)),
                }
            )
        state.commit()
    return {
        "status": "degraded" if any(r["errors"] for r in result) else "success",
        "mode": config.mode,
        "as_of": now.isoformat(),
        "accounts": result,
    }
