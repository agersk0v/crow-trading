"""Provider I/O and transient retries stay outside strategy code."""

import time
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime
from typing import Protocol
from zoneinfo import ZoneInfo

import ccxt
import requests
from yfinance.exceptions import YFRateLimitError

from crow_trading.data.crypto import Exchange, fetch_crypto, open_exchange
from crow_trading.data.models import DataError, Dataset, Request
from crow_trading.data.stocks import fetch_stock
from crow_trading.data.storage import load
from crow_trading.scanner.config import Instrument


class Fetcher(Protocol):
    def __call__(
        self, item: Instrument, request: Request, now: datetime
    ) -> Dataset: ...


class Providers:
    def __init__(self) -> None:
        self.exchanges: dict[str, Exchange] = {}

    def __call__(self, item: Instrument, request: Request, now: datetime) -> Dataset:
        if item.provider == "ccxt":
            if item.venue not in self.exchanges:
                self.exchanges[item.venue] = open_exchange(item.venue)
            return fetch_crypto(
                request,
                item.venue,
                exchange=self.exchanges[item.venue],
                as_of=now,
                attempts=1,
            )
        if item.provider == "yfinance":
            return fetch_stock(request, as_of=now)
        assert item.snapshot is not None
        source = load(item.snapshot)
        if (
            replace(source.metadata.request, start=request.start, end=request.end)
            != request
        ):
            raise DataError(
                "snapshot identity does not match the configured instrument"
            )
        candles = tuple(
            c
            for c in source.candles
            if request.start
            <= c.timestamp.astimezone(ZoneInfo(source.metadata.timezone)).date()
            < request.end
        )
        return Dataset(candles, replace(source.metadata, request=request))


def transient(error: BaseException) -> bool:
    visited: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in visited:
        visited.add(id(current))
        if isinstance(
            current,
            (
                ccxt.NetworkError,
                ccxt.RateLimitExceeded,
                requests.exceptions.Timeout,
                requests.exceptions.ConnectionError,
                YFRateLimitError,
                TimeoutError,
                ConnectionError,
            ),
        ):
            return True
        current = current.__cause__ or current.__context__
    return False


def fetch_retry(
    fetcher: Fetcher,
    item: Instrument,
    request: Request,
    now: datetime,
    attempts: int,
    sleep: Callable[[float], None] = time.sleep,
) -> Dataset:
    for attempt in range(attempts):
        try:
            return fetcher(item, request, now)
        except Exception as exc:
            if not transient(exc) or attempt == attempts - 1:
                raise
            sleep(float(2**attempt))
    raise AssertionError("attempts must be positive")
