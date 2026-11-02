"""Transaction boundaries for initialization and one paper-processing step."""

from datetime import UTC, datetime
from typing import Any

from crow_trading.data.models import DataError, utc
from crow_trading.paper.config import Config
from crow_trading.paper.orders import defer, reconcile_orders
from crow_trading.paper.portfolio import Valuation, pending, record_valuation, value
from crow_trading.paper.risk import risk
from crow_trading.paper.signals import process_signals
from crow_trading.paper.source import Source
from crow_trading.paper.state import State


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
            errors = reconcile_orders(state, account, source, now)
            state.reconcile(account.name)
            valuation: Valuation | None = None
            try:
                valuation = value(state, account, source, now, current=True)
                risk(state, account, valuation, now)
                record_valuation(state, account, source, now, valuation)
            except DataError as exc:
                errors.append(str(exc))
                defer(
                    state,
                    account.name,
                    now,
                    ["valuation", source.fx_fingerprint],
                    str(exc),
                )
            errors.extend(
                process_signals(state, account, source, now, config.mode, valuation)
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
