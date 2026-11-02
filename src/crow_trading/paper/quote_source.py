"""Fresh bid marks/ask entry prices without changing daily signal inputs."""

from datetime import datetime
from decimal import Decimal
from typing import Any

from crow_trading.data.models import DataError
from crow_trading.paper.config import Asset, Config
from crow_trading.paper.quotes import MarketQuote, archive_quote
from crow_trading.paper.source import PriceMark, Source


class QuoteSource(Source):
    def __init__(
        self, config: Config, now: datetime, quotes: dict[str, MarketQuote]
    ) -> None:
        super().__init__(config, now)
        self.quotes = quotes
        self.used: dict[str, str] = {}

    def quote(self, asset: Asset, when: datetime) -> MarketQuote:
        policy = self.config.quote_policy
        if policy is None:
            raise DataError("quote policy is required")
        quote = self.quotes.get(asset.instrument.key)
        if quote is None:
            raise DataError(f"fresh quote unavailable for {asset.instrument.symbol}")
        quote.validate(asset.instrument, when, policy, self.config.mode)
        self.used[asset.instrument.key] = archive_quote(self.config.state_dir, quote)
        return quote

    def entry_price(self, asset: Asset, when: datetime, max_age: int) -> Decimal:
        return self.quote(asset, when).ask

    def decision_evidence(self, asset: Asset) -> dict[str, Any]:
        identity = self.used.get(asset.instrument.key)
        return {"quote_fingerprint": identity} if identity else {}

    def valuation_mark(
        self, asset: Asset, when: datetime, *, current: bool, max_age: int
    ) -> PriceMark:
        quote = self.quote(asset, when)
        return PriceMark(
            quote.bid,
            {
                "price_time": quote.received_at.isoformat(),
                "valuation_side": "bid",
                "quote_fingerprint": quote.fingerprint,
                "quote_timestamp_basis": quote.timestamp_basis,
                "provider_at": quote.provider_at.isoformat()
                if quote.provider_at
                else None,
            },
        )
