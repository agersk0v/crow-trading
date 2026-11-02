"""Public bid/ask observations; never exchange orders or invented timestamps."""

import json
import os
import tempfile
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal, Protocol

import ccxt

from crow_trading.backtesting.models import number
from crow_trading.data.models import DataError, utc
from crow_trading.scanner.config import Instrument, digest

QUOTE_VERSION = "2"
QUOTE_FILL_POLICY = "public_bid_ask_v1"


def clock() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class QuotePolicy:
    max_age_seconds: int = 30
    max_request_seconds: int = 10
    allow_receipt_timestamp: bool = True

    def __post_init__(self) -> None:
        for name, maximum in (("max_age_seconds", 300), ("max_request_seconds", 60)):
            value = getattr(self, name)
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or not 1 <= value <= maximum
            ):
                raise DataError(f"{name} must be an integer from 1 to {maximum}")
        if not isinstance(self.allow_receipt_timestamp, bool):
            raise DataError("allow_receipt_timestamp must be boolean")


@dataclass(frozen=True)
class MarketQuote:
    symbol: str
    venue: str
    bid: Decimal
    ask: Decimal
    requested_at: datetime
    received_at: datetime
    provider_at: datetime | None = None
    mode: Literal["paper", "fixture"] = "paper"
    provider: str = "ccxt"

    @property
    def timestamp_basis(self) -> str:
        return "provider" if self.provider_at is not None else "receipt"

    def payload(self) -> dict[str, Any]:
        return {
            **asdict(self),
            "schema_version": 1,
            "quote_policy_version": QUOTE_VERSION,
            "bid": str(self.bid),
            "ask": str(self.ask),
            "requested_at": self.requested_at.isoformat(),
            "received_at": self.received_at.isoformat(),
            "provider_at": self.provider_at.isoformat() if self.provider_at else None,
            "timestamp_basis": self.timestamp_basis,
        }

    @property
    def fingerprint(self) -> str:
        return digest(self.payload())

    def validate(
        self,
        item: Instrument,
        now: datetime,
        policy: QuotePolicy,
        mode: str,
        *,
        after: datetime | None = None,
    ) -> None:
        if (self.symbol, self.venue, self.provider, self.mode) != (
            item.symbol,
            item.venue,
            "ccxt",
            mode,
        ):
            raise DataError("quote identity/mode mismatch")
        if item.symbol != "BTC/EUR" or item.venue != "kraken" or item.kind != "crypto":
            raise DataError("quote execution supports Kraken BTC/EUR only")
        if (
            not self.bid.is_finite()
            or not self.ask.is_finite()
            or not 0 < self.bid <= self.ask
        ):
            raise DataError("quote bid/ask must be positive finite and uncrossed")
        now, requested, received = (
            utc(now),
            utc(self.requested_at),
            utc(self.received_at),
        )
        if not requested <= received <= now:
            raise DataError("invalid/future quote request or receipt time")
        if (received - requested).total_seconds() > policy.max_request_seconds:
            raise DataError("quote request latency exceeded")
        if after is not None and requested <= utc(after):
            raise DataError(
                "quote request must follow order creation and delivery delay"
            )
        if self.provider_at is None:
            if not policy.allow_receipt_timestamp:
                raise DataError("provider quote timestamp is required")
            observed = received
        else:
            observed = utc(self.provider_at)
            if observed > received:
                raise DataError("future provider quote timestamp")
        if (now - received).total_seconds() > policy.max_age_seconds or (
            now - observed
        ).total_seconds() > policy.max_age_seconds:
            raise DataError("stale quote")


class QuoteFetcher(Protocol):
    def __call__(self, item: Instrument) -> MarketQuote: ...


class PublicQuotes:
    """Synchronous public Kraken ticker, with bounded receipt-time fallback.

    Kraken ticker commonly omits an exchange timestamp. Receipt freshness bounds
    local age/request latency; it does not establish exchange-side quote age.
    """

    def __init__(self, now: Callable[[], datetime] = clock) -> None:
        self.now = now

    def __call__(self, item: Instrument) -> MarketQuote:
        if (item.provider, item.venue, item.symbol) != ("ccxt", "kraken", "BTC/EUR"):
            raise DataError("unsupported public quote instrument")
        client = ccxt.kraken(
            {"enableRateLimit": True, "timeout": 10000, "requests_trust_env": True}
        )
        try:
            client.load_markets()
            requested = utc(self.now())
            ticker = client.fetch_ticker(item.symbol)
            received = utc(self.now())
            stamp = ticker.get("timestamp")
            return MarketQuote(
                ticker["symbol"],
                client.id,
                number(ticker["bid"]),
                number(ticker["ask"]),
                requested,
                received,
                datetime.fromtimestamp(float(number(stamp) / 1000), UTC)
                if stamp is not None
                else None,
            )
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            raise DataError(f"invalid public quote payload: {exc}") from exc
        except ccxt.BaseError as exc:
            raise DataError(f"public quote request failed: {exc}") from exc
        finally:
            client.session.close()


def archive_quote(root: Path, quote: MarketQuote) -> str:
    content = (json.dumps(quote.payload(), sort_keys=True) + "\n").encode()
    identity = quote.fingerprint
    path = root / "inputs" / "quotes" / f"{identity}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_bytes() != content:
            raise DataError("archived quote conflict")
        return identity
    descriptor, temporary = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            if path.read_bytes() != content:
                raise DataError("archived quote conflict") from None
    finally:
        Path(temporary).unlink(missing_ok=True)
    return identity
