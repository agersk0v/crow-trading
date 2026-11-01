"""Daily yfinance research adapter; never requests broker credentials."""

from datetime import UTC, datetime
from typing import Any, Protocol
from zoneinfo import ZoneInfo

import yfinance as yf

from crow_trading.data.models import Candle, DataError, Dataset, Metadata, Request, utc
from crow_trading.data.normalize import schedule, stock_session_open, validate


class Ticker(Protocol):
    def history(self, **kwargs: Any) -> Any: ...
    def get_history_metadata(self) -> dict[str, Any]: ...


def fetch_stock(
    request: Request, *, ticker: Ticker | None = None, as_of: datetime | None = None
) -> Dataset:
    if request.kind != "stock":
        raise DataError("stock adapter requires a stock request")
    expected, timezone = schedule(request)
    now = utc(as_of or datetime.now(UTC))
    if not expected:
        raise DataError("requested window contains no trading sessions")
    if max(expected.values()) > now:
        raise DataError("requested window includes an incomplete or future candle")
    client = ticker or yf.Ticker(request.symbol)
    try:
        history = client.history(
            start=str(request.start),
            end=str(request.end),
            interval="1d",
            auto_adjust=request.adjusted,
            actions=False,
            repair=False,
            raise_errors=True,
        )
        info = client.get_history_metadata()
    except Exception as exc:
        raise DataError(f"yfinance request failed: {exc}") from exc
    if info.get("currency") != request.currency:
        raise DataError("provider currency does not match requested currency")
    if info.get("exchangeTimezoneName") != timezone:
        raise DataError("provider timezone does not match requested calendar")
    if history.empty:
        raise DataError("provider returned no candles")
    opens = {stamp.astimezone(ZoneInfo(timezone)).date(): stamp for stamp in expected}
    candles: list[Candle] = []
    try:
        for label, row in history.iterrows():
            opened = stock_session_open(label, opens, timezone)
            if opened not in expected:
                raise DataError(
                    "provider returned a session outside the requested window"
                )
            candles.append(
                Candle(
                    opened,
                    expected[opened],
                    *(
                        float(row[column])
                        for column in ("Open", "High", "Low", "Close", "Volume")
                    ),
                )
            )
    except (KeyError, TypeError, ValueError) as exc:
        raise DataError(f"invalid stock data: {exc}") from exc
    result = Dataset(
        tuple(candles),
        Metadata(
            request,
            "yfinance",
            str(info.get("exchangeName") or request.calendar),
            timezone,
            now,
            "Yahoo daily research data; auto_adjust=" + str(request.adjusted),
            yf.__version__,
        ),
    )
    validate(result, as_of=now)
    return result
