"""Single-asset cash and cost-basis accounting; no external broker state."""

from dataclasses import dataclass
from decimal import Decimal

from crow_trading.backtesting.models import ZERO


@dataclass
class Portfolio:
    cash: Decimal
    quantity: Decimal = ZERO
    basis: Decimal = ZERO
    realized: Decimal = ZERO
    fees: Decimal = ZERO
    impact: Decimal = ZERO

    def equity(self, mark_account: Decimal) -> Decimal:
        return self.cash + self.quantity * mark_account

    def buy(
        self, quantity: Decimal, price_account: Decimal, fee: Decimal, impact: Decimal
    ) -> None:
        cost = quantity * price_account + fee
        self.cash -= cost
        self.quantity = quantity
        self.basis = cost
        self.fees += fee
        self.impact += impact

    def sell(self, price_account: Decimal, fee: Decimal, impact: Decimal) -> Decimal:
        pnl = self.quantity * price_account - fee - self.basis
        self.cash += self.quantity * price_account - fee
        self.realized += pnl
        self.quantity = ZERO
        self.basis = ZERO
        self.fees += fee
        self.impact += impact
        return pnl
