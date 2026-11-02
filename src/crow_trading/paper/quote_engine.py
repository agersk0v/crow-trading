"""Reserve → observe outside SQLite → revalidate and fill; no broker submission."""

import json
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

from crow_trading.data.models import DataError, utc
from crow_trading.paper.config import Account, Config
from crow_trading.paper.orders import defer, fill_order
from crow_trading.paper.portfolio import (
    Valuation,
    assets,
    pending,
    record_valuation,
    value,
)
from crow_trading.paper.quote_source import QuoteSource
from crow_trading.paper.quotes import MarketQuote, PublicQuotes, QuoteFetcher
from crow_trading.paper.risk import risk
from crow_trading.paper.signals import process_signals
from crow_trading.paper.state import State
from crow_trading.scanner.config import Instrument


def observe(
    config: Config, fetcher: QuoteFetcher, clock: Callable[[], datetime]
) -> tuple[dict[str, MarketQuote], list[str]]:
    quotes: dict[str, MarketQuote] = {}
    errors: list[str] = []
    items: dict[str, Instrument] = {
        a.instrument.key: a.instrument for c in config.accounts for a in c.assets
    }
    assert config.quote_policy is not None
    for key, item in items.items():
        try:
            quote = fetcher(item)
            quote.validate(item, clock(), config.quote_policy, config.mode)
            quotes[key] = quote
        except (DataError, OSError) as exc:
            errors.append(str(exc))
    return quotes, errors


def cancel_revisions(
    state: State, account: Account, source: QuoteSource, now: datetime
) -> None:
    signals = {r["id"]: r for r in source.signals}
    for order in pending(state, account.name):
        original = signals.get(order["signal_id"])
        if original is not None:
            logical = (
                original["instrument_key"],
                original["candle_timestamp"],
                original["strategy_key"],
            )
            if source.latest_signals[logical] != original["id"]:
                state.terminal(order, "cancelled", "signal_superseded", now)


def mark_and_risk(
    state: State,
    account: Account,
    source: QuoteSource,
    now: datetime,
    errors: list[str],
) -> Valuation | None:
    try:
        valuation = value(state, account, source, now, current=True)
        risk(state, account, valuation, now)
        return valuation
    except DataError as exc:
        errors.append(str(exc))
        defer(
            state,
            account.name,
            now,
            ["quote_valuation", source.fx_fingerprint],
            str(exc),
        )
        return None


def quote_step(
    config: Config, clock: Callable[[], datetime], fetcher: QuoteFetcher | None
) -> dict[str, Any]:
    if config.quote_policy is None:
        raise DataError("quote policy required")
    if any(
        a.quote_policy != config.quote_policy
        or any(asset.costs.spread_bps != 0 for asset in a.assets)
        for a in config.accounts
    ):
        raise DataError("account quote policy mismatch or nonzero synthetic spread")
    raw_clock = clock
    previous: datetime | None = None

    def checked_clock() -> datetime:
        nonlocal previous
        instant = utc(raw_clock())
        if previous is not None and instant < previous:
            raise DataError("paper clock moved backwards")
        previous = instant
        return instant

    clock = checked_clock
    if config.mode == "fixture" and fetcher is None:
        raise DataError("fixture execution requires an explicit offline quote fetcher")
    fetcher = fetcher or PublicQuotes(clock)
    # Validate policy/ledger before any network work; initialize remains explicit.
    with State(config.state_dir) as state:
        for account in config.accounts:
            state.ensure(config, account, utc(clock()), initialize=False)
    quotes, initial_errors = observe(config, fetcher, clock)
    source = QuoteSource(config, utc(clock()), quotes)
    errors = {a.name: list(initial_errors) for a in config.accounts}
    with State(config.state_dir) as state:
        now = utc(clock())
        for account in config.accounts:
            state.ensure(config, account, now, initialize=False)
            cancel_revisions(state, account, source, now)
            valuation = mark_and_risk(state, account, source, now, errors[account.name])
            errors[account.name].extend(
                process_signals(state, account, source, now, config.mode, valuation)
            )
            if valuation is not None:
                record_valuation(state, account, source, now, valuation)
            state.connection.execute(
                "UPDATE accounts SET last_clock=? WHERE id=?",
                (now.isoformat(), account.name),
            )
            state.reconcile(account.name)
        needs_execution = any(
            pending(state, account.name) for account in config.accounts
        )
        # This durable boundary permits safe stop/cancel while HTTP is in flight.
        state.commit()
    later_errors: list[str] = []
    if needs_execution:
        quotes, later_errors = observe(config, fetcher, clock)
    # Refresh committed scanner signals after HTTP, including revisions/exits.
    source = QuoteSource(config, utc(clock()), quotes)
    result: list[dict[str, Any]] = []
    with State(config.state_dir) as state:
        now = utc(clock())
        for account in config.accounts:
            state.ensure(config, account, now, initialize=False)
            errors[account.name].extend(later_errors)
            cancel_revisions(state, account, source, now)
            valuation = mark_and_risk(state, account, source, now, errors[account.name])
            errors[account.name].extend(
                process_signals(state, account, source, now, config.mode, valuation)
            )
            for order in sorted(
                pending(state, account.name),
                key=lambda o: (o["side"] == "buy", o["created_at"], o["id"]),
            ):
                when = utc(clock())
                policy = json.loads(order["policy_json"])
                threshold = datetime.fromisoformat(order["created_at"]) + timedelta(
                    seconds=policy["delay_seconds"]
                )
                if when <= threshold:
                    continue
                asset = assets(account)[order["instrument_key"]]
                try:
                    quote = source.quotes.get(asset.instrument.key)
                    if quote is None:
                        raise DataError("fresh execution quote unavailable")
                    fill_order(
                        state,
                        account,
                        asset,
                        order,
                        None,
                        source,
                        when,
                        execution=quote,
                    )
                except DataError as exc:
                    errors[account.name].append(str(exc))
                    defer(
                        state, account.name, when, ["quote_fill", order["id"]], str(exc)
                    )
            when = utc(clock())
            valuation = mark_and_risk(
                state, account, source, when, errors[account.name]
            )
            if valuation is not None:
                record_valuation(state, account, source, when, valuation)
            state.connection.execute(
                "UPDATE accounts SET last_clock=? WHERE id=?",
                (when.isoformat(), account.name),
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
                    "errors": sorted(set(errors[account.name])),
                    "valuation_as_of": when.isoformat()
                    if valuation is not None
                    else None,
                }
            )
        state.commit()
    return {
        "status": "degraded" if any(r["errors"] for r in result) else "success",
        "mode": config.mode,
        "as_of": when.isoformat(),
        "accounts": result,
    }
