"""Public CCXT OHLCV adapter with bounded retries and pagination."""

import time
from collections.abc import Callable
from datetime import UTC, datetime
from functools import partial
from typing import Protocol, cast

import ccxt

from crow_trading.data.models import Candle, DataError, Dataset, Metadata, Request, utc
from crow_trading.data.normalize import schedule, validate


class Exchange(Protocol):
    id: str
    has: dict[str, object]
    timeframes: dict[str, object]
    markets: dict[str, object]

    def load_markets(self) -> object: ...

    def fetch_ohlcv(
        self, symbol: str, timeframe: str, since: int, limit: int
    ) -> list[list[float]]: ...


def retry(
    operation: Callable[[], object], sleep: Callable[[float], None], attempts: int
) -> object:
    for attempt in range(attempts):
        try:
            return operation()
        except (ccxt.NetworkError, ccxt.RateLimitExceeded) as exc:
            if attempt == attempts - 1:
                raise DataError(
                    f"provider failed after {attempts} attempts: {exc}"
                ) from exc
            sleep(2.0**attempt)
        except ccxt.BaseError as exc:
            raise DataError(f"exchange request failed: {exc}") from exc
    raise DataError("attempts must be positive")


def open_exchange(venue: str) -> Exchange:
    """Construct a public client honoring the process proxy/CA configuration."""
    if venue not in ccxt.exchanges:
        raise DataError(f"unknown exchange: {venue}")
    return cast(
        Exchange,
        getattr(ccxt, venue)(
            {
                "enableRateLimit": True,
                "timeout": 10000,
                "requests_trust_env": True,
            }
        ),
    )


def fetch_crypto(
    request: Request,
    venue: str,
    *,
    exchange: Exchange | None = None,
    as_of: datetime | None = None,
    sleep: Callable[[float], None] = time.sleep,
    attempts: int = 3,
) -> Dataset:
    if request.kind != "crypto":
        raise DataError("crypto adapter requires a crypto request")
    if request.symbol.split("/")[-1] != request.currency:
        raise DataError("quote currency must match the CCXT symbol")
    if attempts < 1:
        raise DataError("attempts must be positive")
    expected, timezone = schedule(request)
    now = utc(as_of or datetime.now(UTC))
    if max(expected.values()) > now:
        raise DataError("requested window includes an incomplete or future candle")
    if exchange is None:
        exchange = open_exchange(venue)
    if exchange.id != venue:
        raise DataError("exchange identity does not match requested venue")
    retry(exchange.load_markets, sleep, attempts)
    if not exchange.has.get("fetchOHLCV") or "1d" not in exchange.timeframes:
        raise DataError(f"{venue} does not support daily OHLCV")
    if request.symbol not in exchange.markets:
        raise DataError(f"unsupported symbol on {venue}: {request.symbol}")
    cursor = int(min(expected).timestamp() * 1000)
    end_ms = int(max(expected.values()).timestamp() * 1000)
    candles: list[Candle] = []
    while cursor < end_ms:
        page = cast(
            list[list[float]],
            retry(
                partial(exchange.fetch_ohlcv, request.symbol, "1d", cursor, 500),
                sleep,
                attempts,
            ),
        )
        if not page:
            break
        last_ms = cursor - 1
        previous_ms: float | None = None
        try:
            for row in page:
                if len(row) != 6:
                    raise DataError("expected timestamp and five OHLCV values")
                stamp_ms = row[0]
                if previous_ms is not None and stamp_ms <= previous_ms:
                    raise DataError(
                        "provider page has duplicate or unordered timestamps"
                    )
                previous_ms = stamp_ms
                last_ms = max(last_ms, int(stamp_ms))
                stamp = datetime.fromtimestamp(stamp_ms / 1000, UTC)
                if min(expected) <= stamp < max(expected.values()):
                    closed = expected.get(stamp)
                    if closed is None:
                        raise DataError(
                            "provider returned a non-daily candle timestamp"
                        )
                    candles.append(Candle(stamp, closed, *map(float, row[1:])))
        except (TypeError, ValueError, OverflowError, OSError) as exc:
            raise DataError(f"invalid crypto data: {exc}") from exc
        if last_ms < cursor:
            raise DataError(
                "provider pagination made no progress; history may be limited"
            )
        cursor = last_ms + 86_400_000
    result = Dataset(
        tuple(candles),
        Metadata(
            request,
            "ccxt",
            venue,
            timezone,
            now,
            f"public fetch_ohlcv; symbol={request.symbol}; enableRateLimit=True",
            ccxt.__version__,
        ),
    )
    validate(result, as_of=now)
    return result
