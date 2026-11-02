"""Entry rejection and close-observed risk stops."""

from decimal import Decimal

from crow_trading.backtesting.models import Settings


def entry_rejection(
    *,
    cash: Decimal,
    quantity: Decimal,
    price_quote: Decimal,
    rate: Decimal,
    fee: Decimal,
    raw_quote: Decimal,
    position_cap: Decimal,
    settings: Settings,
    equity_before: Decimal | None = None,
) -> str | None:
    costs = settings.costs
    if quantity <= 0 or quantity < costs.minimum_quantity:
        return "below_minimum_quantity"
    notional = quantity * price_quote
    if notional < costs.minimum_notional_quote:
        return "below_minimum_notional"
    if notional * rate > position_cap:
        return "position_limit"
    if notional * rate + fee > cash:
        return "insufficient_cash"
    market_value = quantity * raw_quote * rate
    equity_after = (
        (cash if equity_before is None else equity_before)
        - notional * rate
        - fee
        + market_value
    )
    if market_value > settings.max_exposure * equity_after:
        return "exposure_limit"
    return None


def loss_stop(equity: Decimal, peak: Decimal, settings: Settings) -> str | None:
    if equity <= settings.initial_cash * (1 - settings.max_loss):
        return "loss_limit"
    if equity <= peak * (1 - settings.max_drawdown):
        return "drawdown_limit"
    return None
