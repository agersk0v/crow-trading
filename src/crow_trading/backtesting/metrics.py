"""Explicit account-currency metrics; no annualization of sub-year windows."""

from datetime import datetime
from decimal import Decimal

from crow_trading.backtesting.engine import Simulation
from crow_trading.backtesting.models import ZERO, number


def metrics(run: Simulation) -> dict[str, str | int | None]:
    initial = number(run.equity[0]["equity"])
    final = number(run.equity[-1]["equity"])
    elapsed = datetime.fromisoformat(
        run.equity[-1]["timestamp"]
    ) - datetime.fromisoformat(run.equity[0]["timestamp"])
    days = Decimal(str(elapsed.total_seconds())) / 86400
    gains = sum((p for p in run.closed_pnls if p > 0), ZERO)
    losses = -sum((p for p in run.closed_pnls if p < 0), ZERO)
    closes = run.equity[1:]
    return {
        "initial_equity": str(initial),
        "final_equity": str(final),
        "net_pnl": str(final - initial),
        "net_return": str(final / initial - 1),
        "gross_pnl_cost_addback": str(final - initial + run.fees + run.impact),
        "gross_return_cost_addback": str(
            (final - initial + run.fees + run.impact) / initial
        ),
        "fees_paid": str(run.fees),
        "execution_impact_paid": str(run.impact),
        "realized_pnl": run.equity[-1]["realized_pnl"],
        "unrealized_pnl": run.equity[-1]["unrealized_pnl"],
        "open_quantity": run.equity[-1]["quantity"],
        "max_drawdown": str(max(number(r["drawdown"]) for r in run.equity)),
        "bar_exposure": str(sum(number(r["exposed"]) for r in closes) / len(closes)),
        "turnover_over_initial_equity": str(run.turnover / initial),
        "filled_orders": sum(r["status"] == "filled" for r in run.ledger),
        "closed_trades": len(run.closed_pnls),
        "wins": sum(p > 0 for p in run.closed_pnls),
        "losses": sum(p < 0 for p in run.closed_pnls),
        "breakeven_trades": sum(p == 0 for p in run.closed_pnls),
        "win_rate": str(
            Decimal(sum(p > 0 for p in run.closed_pnls)) / len(run.closed_pnls)
        )
        if run.closed_pnls
        else None,
        "profit_factor": str(gains / losses) if losses else None,
        "profit_factor_undefined_reason": None if losses else "no_losing_closed_trades",
        "annualized_return": str((final / initial) ** (Decimal(365) / days) - 1)
        if days >= 365 and final > 0
        else None,
    }
