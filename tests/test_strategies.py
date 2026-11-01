"""Hand-checkable baselines and causal replay/latest contracts; no downloads."""

import json
import socket
import subprocess
import sys
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

import pytest

from crow_trading.data.models import Candle, DataError, Dataset, Metadata, Request
from crow_trading.data.normalize import schedule
from crow_trading.data.storage import fingerprint, save
from crow_trading.strategies import MeanReversion, Momentum, Strategy
from crow_trading.strategies.runner import latest, replay, report

NOW = datetime(2025, 6, 1, tzinfo=UTC)


def history(*closes: float) -> tuple[Candle, ...]:
    start = datetime(2025, 1, 1, tzinfo=UTC)
    return tuple(
        Candle(
            start + timedelta(days=i),
            start + timedelta(days=i + 1),
            close,
            close + 1,
            close - 1,
            close,
            100.0,
        )
        for i, close in enumerate(closes)
    )


def make_dataset(
    closes: list[float], kind: Literal["crypto", "stock"] = "crypto"
) -> Dataset:
    req = Request(
        "BTC/EUR" if kind == "crypto" else "AAPL",
        date(2025, 1, 2),
        date(2025, 4, 30),
        "EUR" if kind == "crypto" else "USD",
        kind,
        "24/7" if kind == "crypto" else "XNYS",
    )
    expected, timezone = schedule(req)
    intervals = list(expected.items())[: len(closes)]
    last_date = intervals[-1][0].astimezone(ZoneInfo(timezone)).date()
    req = replace(req, end=last_date + timedelta(days=1))
    candles = tuple(
        Candle(opened, closed, price, price + 1, price - 1, price, 100.0)
        for (opened, closed), price in zip(intervals, closes, strict=True)
    )
    return Dataset(
        candles,
        Metadata(
            req, "fixture", "synthetic", timezone, NOW, "hand-defined fixture", "1"
        ),
    )


@pytest.mark.parametrize(
    "close,intent",
    [
        (13.0, "enter_long"),
        (12.0, "hold"),
        (10.0, "hold"),
        (8.0, "hold"),
        (7.0, "exit_long"),
    ],
)
def test_momentum_exact_boundaries(close: float, intent: str) -> None:
    candles = history(10.0, 10.0, close)
    candles = (
        candles[0],
        replace(candles[1], high=12.0, low=8.0),
        replace(candles[2], high=max(1000.0, close)),
    )
    result = Momentum(2, 2).evaluate("BTC/EUR", candles, as_of=NOW)
    assert result is not None
    assert result.intent == intent
    assert result.available_at == candles[-1].closed_at
    assert result.candle_timestamp == candles[-1].timestamp
    assert result.strategy_id == "momentum" and result.strategy_version == "1"


def test_momentum_distinct_entry_and_exit_windows() -> None:
    candles = history(5.0, 10.0, 7.0)
    result = Momentum(2, 1).evaluate("BTC/EUR", candles, as_of=NOW)
    assert result is not None and result.intent == "exit_long"  # prior low is 9, not 4


@pytest.mark.parametrize(
    "close,intent",
    [
        (94.0, "enter_long"),
        (95.0, "hold"),
        (99.0, "hold"),
        (100.0, "exit_long"),
        (101.0, "exit_long"),
    ],
)
def test_mean_reversion_exact_boundaries(close: float, intent: str) -> None:
    result = MeanReversion(2, 0.05).evaluate(
        "AAPL", history(100, 100, close), as_of=NOW
    )
    assert result is not None
    assert result.intent == intent
    assert result.strategy_id == "mean-reversion" and result.strategy_version == "1"


def test_mean_excludes_current_close() -> None:
    # Prior mean=100 and threshold=95: 94 enters. Including 94 in the mean
    # would lower the threshold and incorrectly suppress the entry.
    result = MeanReversion(2, 0.05).evaluate("AAPL", history(100, 100, 94), as_of=NOW)
    assert result is not None and result.intent == "enter_long"


@pytest.mark.parametrize("strategy", [Momentum(), MeanReversion()])
def test_defaults_and_warmup(strategy: Strategy) -> None:
    assert strategy.history_required == 21
    assert strategy.evaluate("AAPL", (), as_of=NOW) is None
    for count in (1, 19, 20):
        result = strategy.evaluate("AAPL", history(*([100.0] * count)), as_of=NOW)
        assert result is not None
        assert result.intent == "hold" and result.reason == "insufficient_history"
    result = strategy.evaluate("AAPL", history(*([100.0] * 21)), as_of=NOW)
    assert result is not None and result.reason != "insufficient_history"


@pytest.mark.parametrize("value", [0, -1, True, 1.5])
def test_invalid_lookbacks(value: int) -> None:
    for constructor in (
        lambda: Momentum(value, 2),
        lambda: Momentum(2, value),
        lambda: MeanReversion(value),
    ):
        with pytest.raises(DataError, match="positive integer"):
            constructor()


@pytest.mark.parametrize("value", [0, -0.1, 1, 2, float("nan"), float("inf"), True])
def test_invalid_deviation(value: float) -> None:
    with pytest.raises(DataError, match="entry_deviation"):
        MeanReversion(entry_deviation=value)


@pytest.mark.parametrize("strategy", [Momentum(2, 2), MeanReversion(2)])
def test_closed_candle_boundary_and_naive_clock(strategy: Strategy) -> None:
    candles = history(100, 100, 90)
    close = candles[-1].closed_at
    with pytest.raises(DataError, match="unclosed"):
        strategy.evaluate("AAPL", candles, as_of=close - timedelta(microseconds=1))
    assert strategy.evaluate("AAPL", candles, as_of=close) is not None
    with pytest.raises(DataError, match="timezone"):
        strategy.evaluate("AAPL", candles, as_of=close.replace(tzinfo=None))


@pytest.mark.parametrize(
    "case", ["duplicate", "unordered", "nan", "bounds", "interval"]
)
def test_strategy_rejects_invalid_history(case: str) -> None:
    candles = history(100, 100, 90)
    malformed = {
        "duplicate": (*candles, candles[-1]),
        "unordered": tuple(reversed(candles)),
        "nan": (*candles[:-1], replace(candles[-1], close=float("nan"))),
        "bounds": (*candles[:-1], replace(candles[-1], low=200)),
        "interval": (
            *candles[:-1],
            replace(candles[-1], closed_at=candles[-1].timestamp),
        ),
    }[case]
    with pytest.raises(DataError):
        Momentum(2, 2).evaluate("AAPL", malformed, as_of=NOW)


@pytest.mark.parametrize("strategy", [Momentum(3, 2), MeanReversion(3)])
@pytest.mark.parametrize("kind", ["crypto", "stock"])
def test_replay_latest_prefix_and_future_invariance(
    strategy: Strategy, kind: Literal["crypto", "stock"]
) -> None:
    prices = [100.0, 101.0, 99.0, 110.0, 90.0, 105.0, 100.0, 80.0, 120.0]
    original = make_dataset(prices, kind)
    full = replay(original, strategy)
    assert len(full) == len(prices)
    assert replay(original, strategy) == full
    for count in range(1, len(prices) + 1):
        prefix = make_dataset(prices[:count], kind)
        expected = full[:count]
        assert replay(prefix, strategy) == expected
        assert latest(prefix, strategy) == expected[-1]
        cutoff = original.candles[count - 1].closed_at
        assert replay(original, strategy, as_of=cutoff) == expected
        assert latest(original, strategy, as_of=cutoff) == expected[-1]
        assert (
            strategy.evaluate(
                prefix.metadata.request.symbol, prefix.candles, as_of=cutoff
            )
            == expected[-1]
        )
        changed = make_dataset(prices[:count] + [500.0] * (len(prices) - count), kind)
        assert replay(changed, strategy)[:count] == expected
    before_first_close = original.candles[0].closed_at - timedelta(microseconds=1)
    assert replay(original, strategy, as_of=before_first_close) == ()
    assert latest(original, strategy, as_of=before_first_close) is None


@pytest.mark.parametrize("strategy", [Momentum(2, 2), MeanReversion(2)])
def test_stock_and_crypto_share_calculations(strategy: Strategy) -> None:
    prices = [100.0, 100.0, 110.0, 90.0, 95.0, 100.0]
    crypto = replay(make_dataset(prices, "crypto"), strategy)
    stocks = replay(make_dataset(prices, "stock"), strategy)
    assert [(s.intent, s.reason) for s in crypto] == [
        (s.intent, s.reason) for s in stocks
    ]
    assert any(
        (right.candle_timestamp - left.candle_timestamp).days > 1
        for left, right in zip(stocks, stocks[1:], strict=False)
    )


def test_pure_strategies_have_no_file_or_network_side_effects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("strategy attempted I/O")

    candles = history(100, 100, 90)
    with monkeypatch.context() as guard:
        guard.setattr("builtins.open", forbidden)
        guard.setattr(Path, "open", forbidden)
        guard.setattr(socket.socket, "connect", forbidden)
        guard.setattr(socket, "create_connection", forbidden)
        for strategy in (Momentum(2, 2), MeanReversion(2)):
            assert strategy.evaluate("AAPL", candles, as_of=NOW) is not None


def test_report_provenance_and_parameters() -> None:
    dataset = make_dataset([100.0, 100.0, 90.0])
    strategy = MeanReversion(2, 0.05)
    output = report(dataset, strategy)
    assert output["dataset_fingerprint"] == fingerprint(dataset)
    assert output["strategy"] == {
        "id": "mean-reversion",
        "version": "1",
        "parameters": {"lookback": 2, "entry_deviation": 0.05},
    }
    assert report(dataset, strategy, mode="latest")["signals"] == output["signals"][-1:]
    assert output == report(dataset, strategy)


def run_cli(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "crow_trading.strategies", *args],
        capture_output=True,
        text=True,
    )


@pytest.mark.parametrize("name", ["momentum", "mean-reversion"])
def test_cli_offline_replay_latest_and_immutable_input(
    tmp_path: Path, name: str
) -> None:
    dataset = make_dataset([100.0] * 21 + [90.0, 110.0])
    path, output = tmp_path / "source.parquet", tmp_path / "signals.json"
    save(dataset, path)
    before = path.read_bytes()
    replayed = run_cli(str(path), "--strategy", name, "--output", str(output))
    assert replayed.returncode == 0, replayed.stderr
    content = output.read_bytes()
    result = json.loads(content)
    repeated = run_cli(str(path), "--strategy", name)
    assert repeated.returncode == 0
    assert repeated.stdout.encode() == content
    last = run_cli(str(path), "--strategy", name, "--mode", "latest")
    assert last.returncode == 0
    assert json.loads(last.stdout)["signals"] == result["signals"][-1:]
    refused = run_cli(str(path), "--strategy", name, "--output", str(path))
    assert refused.returncode == 1
    assert path.read_bytes() == before
    refused = run_cli(str(path), "--strategy", name, "--output", str(output))
    assert refused.returncode == 1
    assert output.read_bytes() == content


@pytest.mark.parametrize(
    "options,exit_code",
    [
        (["--strategy", "momentum", "--entry-lookback", "0"], 1),
        (["--strategy", "mean-reversion", "--entry-deviation", "nan"], 1),
        (["--strategy", "momentum", "--lookback", "2"], 2),
        (["--strategy", "mean-reversion", "--exit-lookback", "2"], 2),
        (["--strategy", "momentum", "--as-of", "2025-01-01"], 1),
    ],
)
def test_cli_invalid_options(
    tmp_path: Path, options: list[str], exit_code: int
) -> None:
    path = tmp_path / "source.parquet"
    save(make_dataset([100.0] * 3), path)
    result = run_cli(str(path), *options)
    assert result.returncode == exit_code
    assert "error:" in result.stderr
