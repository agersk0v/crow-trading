"""Offline contracts: providers, session validation, archives, and cache integrity."""

import json
from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import ccxt
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from crow_trading.data.archive import import_archive
from crow_trading.data.crypto import fetch_crypto
from crow_trading.data.models import Candle, DataError, Dataset, Metadata, Request
from crow_trading.data.normalize import schedule, validate
from crow_trading.data.stocks import fetch_stock
from crow_trading.data.storage import KEY, fingerprint, load, save

NOW = datetime(2025, 2, 1, tzinfo=UTC)


def crypto_request() -> Request:
    return Request("BTC/EUR", date(2025, 1, 1), date(2025, 1, 4), "EUR", "crypto")


def dataset(request: Request | None = None) -> Dataset:
    req = request or crypto_request()
    expected, timezone = schedule(req)
    return Dataset(
        tuple(
            Candle(opened, closed, 10.0, 12.0, 9.0, 11.0, 100.0)
            for opened, closed in expected.items()
        ),
        Metadata(req, "fixture", "test", timezone, NOW, "synthetic", "1"),
    )


class FakeExchange:
    id = "kraken"
    has: dict[str, object] = {"fetchOHLCV": True}
    timeframes: dict[str, object] = {"1d": "1d"}
    markets: dict[str, object] = {"BTC/EUR": {}}

    def __init__(self, pages: Sequence[list[list[float]] | Exception]) -> None:
        self.pages = list(pages)
        self.calls: list[int] = []
        self.loaded = False

    def load_markets(self) -> object:
        self.loaded = True
        return self.markets

    def fetch_ohlcv(
        self, symbol: str, timeframe: str, since: int, limit: int
    ) -> list[list[float]]:
        assert self.loaded and symbol == "BTC/EUR" and timeframe == "1d"
        assert limit == 500
        self.calls.append(since)
        item = self.pages.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def rows() -> list[list[float]]:
    return [
        [c.timestamp.timestamp() * 1000, c.open, c.high, c.low, c.close, c.volume]
        for c in dataset().candles
    ]


def test_crypto_pagination_and_retry() -> None:
    source = rows()
    exchange = FakeExchange(
        [
            ccxt.RateLimitExceeded("slow down"),
            source[:1],
            source[1:],
            [],
        ]
    )
    sleeps: list[float] = []
    result = fetch_crypto(
        crypto_request(), "kraken", exchange=exchange, as_of=NOW, sleep=sleeps.append
    )
    assert result.candles == dataset().candles
    assert sleeps == [1.0]
    assert exchange.calls[:2] == [int(source[0][0])] * 2
    assert exchange.calls[2] == int(source[0][0]) + 86_400_000
    assert result.metadata.venue == "kraken"


def test_crypto_retry_exhausted() -> None:
    exchange = FakeExchange([ccxt.NetworkError("offline")] * 3)
    sleeps: list[float] = []
    with pytest.raises(DataError, match="after 3 attempts"):
        fetch_crypto(
            crypto_request(),
            "kraken",
            exchange=exchange,
            as_of=NOW,
            sleep=sleeps.append,
        )
    assert sleeps == [1.0, 2.0]


def test_crypto_fatal_error_is_not_retried() -> None:
    exchange = FakeExchange([ccxt.BadSymbol("not found")])
    with pytest.raises(DataError, match="exchange request failed"):
        fetch_crypto(crypto_request(), "kraken", exchange=exchange, as_of=NOW)
    assert len(exchange.calls) == 1


@pytest.mark.parametrize(
    "pages, message",
    [
        ([[]], "no candles"),
        ([rows()[1:], []], "insufficient coverage"),
        ([[rows()[0], rows()[0]]], "duplicate"),
        ([[rows()[1], rows()[0]]], "unordered"),
        ([[rows()[0]], [rows()[0]]], "no progress"),
        ([[[1.0, 2.0]]], "five OHLCV"),
    ],
)
def test_bad_crypto_responses(pages: list[list[list[float]]], message: str) -> None:
    with pytest.raises(DataError, match=message):
        fetch_crypto(
            crypto_request(), "kraken", exchange=FakeExchange(pages), as_of=NOW
        )


def test_crypto_unsupported_symbol_and_timeframe() -> None:
    with pytest.raises(DataError, match="unsupported symbol"):
        fetch_crypto(
            replace(crypto_request(), symbol="NO/EUR"),
            "kraken",
            exchange=FakeExchange([]),
            as_of=NOW,
        )
    exchange = FakeExchange([])
    exchange.timeframes = {}
    with pytest.raises(DataError, match="daily OHLCV"):
        fetch_crypto(crypto_request(), "kraken", exchange=exchange, as_of=NOW)
    with pytest.raises(DataError, match="only daily"):
        replace(crypto_request(), timeframe="4h")


@pytest.mark.parametrize(
    "mutation, message",
    [
        ({"close": float("nan")}, "finite"),
        ({"volume": -1.0}, "nonnegative"),
        ({"open": 0.0}, "positive"),
        ({"high": 9.5}, "bounds"),
        ({"low": 11.5}, "bounds"),
        ({"timestamp": datetime(2025, 1, 1)}, "timezone"),
        ({"closed_at": datetime(2025, 1, 1, 12, tzinfo=UTC)}, "interval"),
    ],
)
def test_invalid_candles(mutation: dict[str, Any], message: str) -> None:
    original = dataset()
    altered = replace(original.candles[0], **mutation)
    with pytest.raises(DataError, match=message):
        validate(replace(original, candles=(altered, *original.candles[1:])), as_of=NOW)


@pytest.mark.parametrize(
    "case, message",
    [
        ("empty", "no candles"),
        ("duplicate", "duplicate"),
        ("reverse", "ordered"),
        ("gap", "missing candles"),
    ],
)
def test_invalid_coverage(case: str, message: str) -> None:
    original = dataset()
    candles = {
        "empty": (),
        "duplicate": (*original.candles, original.candles[-1]),
        "reverse": tuple(reversed(original.candles)),
        "gap": original.candles[1:],
    }[case]
    with pytest.raises(DataError, match=message):
        validate(replace(original, candles=candles), as_of=NOW)


def test_current_candle_is_rejected_before_provider_call() -> None:
    source = FakeExchange([])
    with pytest.raises(DataError, match="incomplete"):
        fetch_crypto(
            crypto_request(),
            "kraken",
            exchange=source,
            as_of=datetime(2025, 1, 3, 12, tzinfo=UTC),
        )
    assert not source.loaded


def test_retrieval_before_close_is_rejected() -> None:
    original = dataset()
    with pytest.raises(DataError, match="incomplete"):
        validate(
            replace(
                original,
                metadata=replace(
                    original.metadata, retrieved_at=datetime(2025, 1, 3, 12, tzinfo=UTC)
                ),
            ),
            as_of=NOW,
        )


def stock_request() -> Request:
    return Request(
        "AAPL",
        date(2025, 1, 17),
        date(2025, 1, 23),
        "USD",
        "stock",
        "XNYS",
        adjusted=True,
    )


class FakeTicker:
    def __init__(self) -> None:
        self.kwargs: dict[str, Any] = {}
        self.frame = pd.DataFrame(
            {
                "Open": [10.0] * 3,
                "High": [12.0] * 3,
                "Low": [9.0] * 3,
                "Close": [11.0] * 3,
                "Volume": [100.0] * 3,
            },
            index=pd.DatetimeIndex(
                ["2025-01-17", "2025-01-21", "2025-01-22"], tz="America/New_York"
            ),
        )
        self.info: dict[str, Any] = {
            "currency": "USD",
            "exchangeTimezoneName": "America/New_York",
            "exchangeName": "NMS",
        }

    def history(self, **kwargs: Any) -> Any:
        self.kwargs = kwargs
        return self.frame

    def get_history_metadata(self) -> dict[str, Any]:
        return self.info


def test_stock_holiday_timezone_and_adjustment() -> None:
    ticker = FakeTicker()
    result = fetch_stock(stock_request(), ticker=ticker, as_of=NOW)
    assert [c.timestamp.day for c in result.candles] == [17, 21, 22]
    assert result.candles[0].timestamp.hour == 14  # 09:30 EST
    assert result.candles[0].timestamp.minute == 30
    assert result.candles[0].closed_at.hour == 21
    assert result.metadata.request.adjusted
    assert ticker.kwargs["auto_adjust"] is True
    assert ticker.kwargs["end"] == "2025-01-23"


@pytest.mark.parametrize(
    "field,value",
    [
        ("currency", "EUR"),
        ("exchangeTimezoneName", "Europe/London"),
    ],
)
def test_stock_rejects_metadata_mismatch(field: str, value: str) -> None:
    ticker = FakeTicker()
    ticker.info[field] = value
    with pytest.raises(DataError, match="does not match"):
        fetch_stock(stock_request(), ticker=ticker, as_of=NOW)


def test_stock_missing_session() -> None:
    ticker = FakeTicker()
    ticker.frame = ticker.frame.iloc[1:]
    with pytest.raises(DataError, match="missing candles"):
        fetch_stock(stock_request(), ticker=ticker, as_of=NOW)


def test_stock_dst_and_half_day() -> None:
    req = replace(stock_request(), start=date(2025, 3, 7), end=date(2025, 3, 11))
    expected, _ = schedule(req)
    assert [opened.hour for opened in expected] == [14, 13]
    req = replace(req, start=date(2025, 11, 28), end=date(2025, 11, 29))
    expected, _ = schedule(req)
    assert next(iter(expected.values())).hour == 18  # Thanksgiving Friday early close


def test_archive_and_parquet_roundtrip(tmp_path: Path) -> None:
    archive = tmp_path / "candles.csv"
    archive.write_text(
        "timestamp,open,high,low,close,volume\n"
        + "".join(
            f"{c.timestamp.isoformat()},10,12,9,11,100\n" for c in dataset().candles
        )
    )
    original = import_archive(
        archive,
        crypto_request(),
        venue="kraken",
        provenance="https://example.org/synthetic.csv",
        as_of=NOW,
    )
    path = tmp_path / "candles.parquet"
    identity = save(original, path)
    assert load(path) == original
    assert load(path).candles == dataset().candles
    assert "file_sha256=" in original.metadata.provenance
    before = path.read_bytes()
    newer = replace(
        original,
        metadata=replace(original.metadata, retrieved_at=NOW + timedelta(days=1)),
    )
    assert save(newer, path) == identity
    assert path.read_bytes() == before  # original retrieval metadata retained
    changed = replace(
        original,
        candles=(replace(original.candles[0], close=10.5), *original.candles[1:]),
    )
    with pytest.raises(DataError, match="refusing to replace"):
        save(changed, path)
    assert load(path) == original


def test_archive_invalid_and_naive_timestamps(tmp_path: Path) -> None:
    archive = tmp_path / "bad.csv"
    archive.write_text(
        "timestamp,open,high,low,close,volume\n2025-01-01,10,12,9,11,100\n"
    )
    with pytest.raises(DataError, match="timezone"):
        import_archive(
            archive, crypto_request(), venue="kraken", provenance="fixture", as_of=NOW
        )


@pytest.mark.parametrize("corruption", ["price", "coverage", "fingerprint", "version"])
def test_cache_corruption(tmp_path: Path, corruption: str) -> None:
    path = tmp_path / "data.parquet"
    original = dataset()
    save(original, path)
    table = pq.read_table(path)
    meta = json.loads(table.schema.metadata[KEY])
    if corruption == "price":
        index = table.schema.get_field_index("close")
        table = table.set_column(
            index,
            table.schema.field(index),
            [pa.array([10.5, 11.0, 11.0])],
        )
    elif corruption == "coverage":
        meta["row_count"] = 999
    elif corruption == "version":
        meta["schema_version"] = 999
    else:
        meta["fingerprint"] = "bad"
    pq.write_table(
        table.replace_schema_metadata({KEY: json.dumps(meta).encode()}), path
    )
    with pytest.raises(DataError):
        load(path)


def test_fingerprint_changes_with_provenance() -> None:
    original = dataset()
    other = replace(original, metadata=replace(original.metadata, venue="other"))
    assert fingerprint(other) != fingerprint(original)


def test_public_exchange_respects_proxy_and_tls_settings() -> None:
    from crow_trading.data.crypto import open_exchange

    client: Any = open_exchange("kraken")
    assert client.session.trust_env is True
    assert client.enableRateLimit is True
    assert client.verify is True
    assert client.apiKey == ""
    with pytest.raises(DataError, match="unknown exchange"):
        open_exchange("nonexistent")


@pytest.mark.parametrize("stamp", [float("nan"), float("inf"), -1e100])
def test_malformed_crypto_timestamps(stamp: float) -> None:
    page = rows()[:1]
    page[0][0] = stamp
    with pytest.raises(DataError, match="invalid crypto data"):
        fetch_crypto(
            crypto_request(), "kraken", exchange=FakeExchange([page]), as_of=NOW
        )


def test_stock_empty_response_and_duplicate_session() -> None:
    ticker = FakeTicker()
    ticker.frame = ticker.frame.iloc[:0]
    with pytest.raises(DataError, match="no candles"):
        fetch_stock(stock_request(), ticker=ticker, as_of=NOW)
    ticker = FakeTicker()
    ticker.frame = pd.concat([ticker.frame, ticker.frame.iloc[-1:]])
    with pytest.raises(DataError, match="duplicate"):
        fetch_stock(stock_request(), ticker=ticker, as_of=NOW)


def test_stock_no_sessions_and_invalid_calendar() -> None:
    request = replace(stock_request(), start=date(2025, 1, 18), end=date(2025, 1, 21))
    with pytest.raises(DataError, match="no trading sessions"):
        fetch_stock(request, ticker=FakeTicker(), as_of=NOW)
    with pytest.raises(DataError, match="calendar"):
        schedule(replace(request, calendar="UNKNOWN"))


def test_stock_unadjusted_request() -> None:
    ticker = FakeTicker()
    result = fetch_stock(
        replace(stock_request(), adjusted=False), ticker=ticker, as_of=NOW
    )
    assert ticker.kwargs["auto_adjust"] is False
    assert result.metadata.request.adjusted is False


def test_archive_missing_bar(tmp_path: Path) -> None:
    path = tmp_path / "incomplete.csv"
    path.write_text(
        "timestamp,open,high,low,close,volume\n2025-01-01T00:00:00Z,10,12,9,11,100\n"
    )
    with pytest.raises(DataError, match="missing candles"):
        import_archive(
            path, crypto_request(), venue="kraken", provenance="fixture", as_of=NOW
        )


def test_cli_archive_import_and_offline_inspect(tmp_path: Path) -> None:
    import subprocess
    import sys

    archive = tmp_path / "single.csv"
    archive.write_text(
        "timestamp,open,high,low,close,volume\n2025-01-01T00:00:00Z,10,12,9,11,100\n"
    )
    output = tmp_path / "single.parquet"
    command = [sys.executable, "-m", "crow_trading.data"]
    completed = subprocess.run(
        [
            *command,
            "import-archive",
            "--kind",
            "crypto",
            "--symbol",
            "BTC/EUR",
            "--currency",
            "EUR",
            "--start",
            "2025-01-01",
            "--end",
            "2025-01-02",
            "--venue",
            "kraken",
            "--input",
            str(archive),
            "--provenance",
            "fixture",
            "--output",
            str(output),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    imported = json.loads(completed.stdout)
    completed = subprocess.run(
        [*command, "inspect", str(output)], capture_output=True, text=True, check=True
    )
    assert json.loads(completed.stdout) == imported
    assert imported["row_count"] == 1
    failed = subprocess.run(
        [*command, "inspect", str(tmp_path / "missing.parquet")],
        capture_output=True,
        text=True,
    )
    assert failed.returncode == 1
    assert "error:" in failed.stderr
