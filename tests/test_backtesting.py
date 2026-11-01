"""Hand-calculated historical accounting and guarded experiment workflows."""

import json
import subprocess
import sys
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, ClassVar, Literal, cast
from zoneinfo import ZoneInfo

import pytest

from crow_trading.backtesting.engine import simulate
from crow_trading.backtesting.experiment import freeze, read_plan
from crow_trading.backtesting.market import FXBook, check_actions
from crow_trading.backtesting.metrics import metrics
from crow_trading.backtesting.models import (
    Costs,
    Settings,
    Window,
    number,
    parse_windows,
)
from crow_trading.data.models import Candle, DataError, Dataset, Metadata, Request
from crow_trading.data.normalize import schedule
from crow_trading.strategies.base import Intent, Signal

D = Decimal
NOW = datetime(2025, 6, 1, tzinfo=UTC)


def settings(**changes: Any) -> Settings:
    base = Settings(
        "EUR",
        D(1000),
        D(50),
        D(50),
        D(1),
        D(1),
        D(1),
        0,
        1000,
        Costs(D(0), D(0), D(0), D(0), D(1), D(0), D(0)),
    )
    return replace(base, **changes)


def dataset(
    opens: list[float],
    *,
    closes: list[float] | None = None,
    kind: Literal["crypto", "stock"] = "crypto",
    currency: str = "EUR",
) -> Dataset:
    req = Request(
        "BTC/" + currency if kind == "crypto" else "AAPL",
        date(2025, 1, 2),
        date(2025, 4, 1),
        currency,
        kind,
        "24/7" if kind == "crypto" else "XNYS",
    )
    expected, timezone = schedule(req)
    intervals = list(expected.items())[: len(opens)]
    last_date = intervals[-1][0].astimezone(ZoneInfo(timezone)).date()
    req = replace(req, end=last_date + timedelta(days=1))
    closes = closes or opens
    candles = tuple(
        Candle(
            opened,
            closed,
            price,
            max(price, close) + 1,
            min(price, close) / 2,
            close,
            100.0,
        )
        for (opened, closed), price, close in zip(intervals, opens, closes, strict=True)
    )
    return Dataset(
        candles,
        Metadata(
            req,
            "synthetic",
            "fixture",
            timezone,
            NOW,
            "synthetic hand-calculated example",
            "1",
        ),
    )


def window(data: Dataset) -> Window:
    return Window("test", data.metadata.request.start, data.metadata.request.end)


def fx(points: list[tuple[str, str]] | None = None) -> FXBook:
    return FXBook(
        {
            "base": "EUR",
            "source": "synthetic rates",
            "rates": {
                "USD": [
                    {"available_at": stamp, "eur_per_unit": rate}
                    for stamp, rate in points
                ]
            }
            if points
            else {},
        },
        1000,
    )


def actions(data: Dataset) -> dict[str, Any]:
    return {
        "source": "synthetic no-action fixture",
        "symbol": data.metadata.request.symbol,
        "start": str(data.metadata.request.start),
        "end": str(data.metadata.request.end),
        "coverage_complete": True,
        "prices_unadjusted": True,
        "events": [],
    }


@dataclass(frozen=True)
class Script:
    intents: dict[datetime, str]
    strategy_id: ClassVar[str] = "scripted-test"
    version: ClassVar[str] = "1"
    history_required: ClassVar[int] = 1

    def parameters(self) -> dict[str, int | float]:
        return {}

    def evaluate(
        self, symbol: str, candles: tuple[Candle, ...], *, as_of: datetime
    ) -> Signal | None:
        if not candles:
            return None
        candle = candles[-1]
        assert candle.closed_at <= as_of
        intent = self.intents.get(candle.timestamp, "hold")
        if intent not in ("enter_long", "exit_long", "hold"):
            raise AssertionError(intent)
        return Signal(
            symbol,
            self.strategy_id,
            self.version,
            candle.timestamp,
            candle.closed_at,
            cast(Intent, intent),
            "script",
        )


def script(data: Dataset, *intents: str) -> Script:
    return Script(
        {c.timestamp: intent for c, intent in zip(data.candles, intents, strict=False)}
    )


def fills(run: Any) -> list[dict[str, str]]:
    return [row for row in run.ledger if row["status"] == "filled"]


def test_hand_calculated_round_trip_fees_and_rejections() -> None:
    data = dataset([10, 10, 12, 15, 15])
    cfg = settings(
        costs=replace(settings().costs, fee_rate=D("0.01"), fixed_fee_quote=D(1))
    )
    strategy = script(
        data, "enter_long", "enter_long", "exit_long", "exit_long", "enter_long"
    )
    run = simulate(data, strategy, cfg, window(data), fx())
    trades = fills(run)
    assert len(trades) == 2
    buy, sell = trades
    assert D(buy["order_quantity"]) == 5
    assert D(buy["fee_account"]) == D("1.5")
    assert D(buy["cash_after"]) == D("948.5")
    assert D(sell["fee_account"]) == D("1.75")
    assert D(sell["realized_pnl_delta"]) == D("21.75")
    assert D(sell["cash_after"]) == D("1021.75")
    assert buy["timestamp"] == data.candles[1].timestamp.isoformat()
    assert sell["timestamp"] == data.candles[3].timestamp.isoformat()
    assert {r["reason"] for r in run.ledger if r["status"] == "rejected"} == {
        "already_long",
        "not_long",
    }
    assert run.ledger[-1]["reason"] == "no_eligible_open_in_window"
    for row in run.ledger:
        if row["status"] != "filled":
            assert D(row["cash_delta"]) == D(row["quantity_delta"]) == 0
    assert sum((D(r["cash_delta"]) for r in run.ledger), D(0)) == D("21.75")
    assert sum((D(r["quantity_delta"]) for r in run.ledger), D(0)) == 0
    result = metrics(run)
    assert D(str(result["net_pnl"])) == D("21.75")
    assert D(str(result["gross_pnl_cost_addback"])) == 25
    assert result["wins"] == 1 and result["closed_trades"] == 1
    assert result["profit_factor"] is None and result["annualized_return"] is None


def test_spread_slippage_direction_and_quantity_rounding() -> None:
    data = dataset([10, 10, 20, 20])
    cfg = settings(
        costs=replace(settings().costs, spread_bps=D(100), slippage_bps=D(50))
    )
    run = simulate(
        data, script(data, "enter_long", "exit_long"), cfg, window(data), fx()
    )
    buy, sell = fills(run)
    assert D(buy["fill_price_quote"]) == D("10.1")
    assert D(buy["order_quantity"]) == 4
    assert D(sell["fill_price_quote"]) == D("19.8")
    assert D(run.equity[-1]["equity"]) == D("1038.8")
    assert run.impact == D("1.2")


@pytest.mark.parametrize(
    "change,reason",
    [
        ({"initial_cash": D(49)}, "insufficient_cash"),
        ({"max_position_eur": D(40)}, "position_limit"),
        ({"max_exposure": D("0.04")}, "exposure_limit"),
    ],
)
def test_entry_limits_do_not_change_balances(
    change: dict[str, Any], reason: str
) -> None:
    data = dataset([10, 10, 10])
    cfg = settings(**change)
    run = simulate(data, script(data, "enter_long"), cfg, window(data), fx())
    assert not fills(run)
    assert any(row["reason"] == reason for row in run.ledger)
    assert D(run.equity[-1]["equity"]) == cfg.initial_cash


@pytest.mark.parametrize(
    "cost_change,reason",
    [
        ({"quantity_step": D(100)}, "below_minimum_quantity"),
        ({"minimum_quantity": D(10)}, "below_minimum_quantity"),
        ({"minimum_notional_quote": D(100)}, "below_minimum_notional"),
    ],
)
def test_market_minimums(cost_change: dict[str, Decimal], reason: str) -> None:
    data = dataset([10, 10, 10])
    cfg = settings(costs=replace(settings().costs, **cost_change))
    run = simulate(data, script(data, "enter_long"), cfg, window(data), fx())
    assert not fills(run)
    assert reason in [r["reason"] for r in run.ledger]


@pytest.mark.parametrize("step,quantity", [("1", "1"), ("0.1", "1.6")])
def test_whole_and_fractional_quantity(step: str, quantity: str) -> None:
    data = dataset([30, 30, 30])
    cfg = settings(costs=replace(settings().costs, quantity_step=D(step)))
    run = simulate(data, script(data, "enter_long"), cfg, window(data), fx())
    assert D(fills(run)[0]["order_quantity"]) == D(quantity)


def test_timestamped_fx_and_fx_pnl() -> None:
    data = dataset([10, 10, 12, 15, 15], currency="USD")
    book = fx([("2025-01-01T00:00:00Z", "0.5"), ("2025-01-04T00:00:00Z", "0.6")])
    cfg = settings(
        costs=replace(settings().costs, fee_rate=D("0.01"), fixed_fee_quote=D(2))
    )
    run = simulate(
        data, script(data, "enter_long", "hold", "exit_long"), cfg, window(data), book
    )
    buy, sell = fills(run)
    assert D(buy["quote_to_account_fx"]) == D("0.5")
    assert D(buy["order_quantity"]) == 10
    assert D(buy["fee_account"]) == D("1.5")
    assert D(sell["quote_to_account_fx"]) == D("0.6")
    assert D(sell["realized_pnl_delta"]) == D("36.4")
    assert D(run.equity[-1]["equity"]) == D("1036.4")


def test_non_eur_account_target_and_fees() -> None:
    data = dataset([10, 10, 10])
    cfg = settings(
        account_currency="USD",
        initial_cash=D(2000),
        costs=replace(settings().costs, fixed_fee_quote=D(1)),
    )
    run = simulate(
        data,
        script(data, "enter_long"),
        cfg,
        window(data),
        fx([("2025-01-01T00:00:00Z", "0.5")]),
    )
    buy = fills(run)[0]
    assert D(buy["order_quantity"]) == 5
    assert D(buy["fee_account"]) == 2
    assert D(run.equity[-1]["equity"]) == 1998


def test_fx_rejects_future_only_and_stale_rates() -> None:
    future = fx([("2025-02-01T00:00:00Z", "0.5")])
    with pytest.raises(DataError, match="no FX rate"):
        future.eur_rate("USD", datetime(2025, 1, 1, tzinfo=UTC))
    stale = FXBook(
        {
            "base": "EUR",
            "source": "fixture",
            "rates": {
                "USD": [{"available_at": "2025-01-01T00:00:00Z", "eur_per_unit": "0.5"}]
            },
        },
        24,
    )
    with pytest.raises(DataError, match="stale FX"):
        stale.eur_rate("USD", datetime(2025, 1, 3, tzinfo=UTC))
    data = dataset([10, 10], currency="USD")
    with pytest.raises(DataError, match="no FX rate"):
        simulate(data, script(data), settings(), window(data), fx())


def test_loss_stop_halts_entries_and_exits_later() -> None:
    data = dataset([10, 10, 6, 6], closes=[10, 6, 6, 6])
    cfg = settings(max_loss=D("0.01"))
    run = simulate(data, script(data, *("enter_long",) * 4), cfg, window(data), fx())
    buy, sell = fills(run)
    assert sell["reason"] == "loss_limit"
    assert sell["timestamp"] == data.candles[2].timestamp.isoformat()
    assert D(run.equity[-1]["equity"]) == 980
    assert any(r["reason"] == "risk_halted" for r in run.ledger)


def test_delayed_risk_exit_keeps_original_availability() -> None:
    data = dataset([10, 10, 10, 6, 6], closes=[10, 10, 6, 6, 6])
    cfg = settings(max_loss=D("0.01"), delivery_delay_seconds=1)
    run = simulate(data, script(data, *("enter_long",) * 5), cfg, window(data), fx())
    buy, sell = fills(run)
    assert buy["timestamp"] == data.candles[2].timestamp.isoformat()
    assert sell["timestamp"] == data.candles[4].timestamp.isoformat()
    assert sell["available_at"] == data.candles[2].closed_at.isoformat()
    assert any(r["status"] == "cancelled" for r in run.ledger)


def test_peak_drawdown_and_exposure_exits() -> None:
    data = dataset([10, 10, 20, 10, 10], closes=[10, 20, 10, 10, 10])
    run = simulate(
        data,
        script(data, "enter_long"),
        settings(max_drawdown=D("0.03")),
        window(data),
        fx(),
    )
    assert fills(run)[1]["reason"] == "drawdown_limit"
    data = dataset([10, 10, 100, 100], closes=[10, 100, 100, 100])
    run = simulate(
        data,
        script(data, "enter_long"),
        settings(max_exposure=D("0.1")),
        window(data),
        fx(),
    )
    assert fills(run)[1]["reason"] == "exposure_limit"


def test_open_terminal_position_and_unfilled_last_signal() -> None:
    data = dataset([10, 10, 20])
    run = simulate(
        data,
        script(data, "enter_long", "hold", "exit_long"),
        settings(),
        window(data),
        fx(),
    )
    assert len(fills(run)) == 1
    assert D(run.equity[-1]["quantity"]) == 5
    assert D(run.equity[-1]["unrealized_pnl"]) == 50
    assert D(run.equity[-1]["equity"]) == 1050
    assert run.ledger[-1]["status"] == "unfilled"


def test_stock_calendar_and_corporate_action_gates() -> None:
    data = dataset([10] * 5, kind="stock")
    with pytest.raises(DataError, match="coverage snapshot"):
        simulate(data, script(data, "enter_long"), settings(), window(data), fx())
    run = simulate(
        data,
        script(data, "hold", "enter_long"),
        settings(),
        window(data),
        fx(),
        actions=actions(data),
    )
    assert fills(run)[0]["timestamp"] == data.candles[2].timestamp.isoformat()
    assert data.candles[1].timestamp.weekday() == 4
    assert data.candles[2].timestamp.weekday() == 0
    for event in ({"type": "split", "ratio": "2"}, {"type": "dividend", "amount": "1"}):
        with pytest.raises(DataError, match="unsupported"):
            check_actions(data, {**actions(data), "events": [event]})
    with pytest.raises(DataError, match="adjusted"):
        check_actions(
            replace(
                data,
                metadata=replace(
                    data.metadata, request=replace(data.metadata.request, adjusted=True)
                ),
            ),
            actions(data),
        )
    with pytest.raises(DataError, match="coverage"):
        check_actions(data, {**actions(data), "coverage_complete": False})
    with pytest.raises(DataError, match="unadjusted price basis"):
        check_actions(data, {**actions(data), "prices_unadjusted": False})


def test_missing_data_empty_windows_and_isolated_cash() -> None:
    data = dataset([10] * 6)
    with pytest.raises(DataError, match="missing candles"):
        simulate(
            replace(data, candles=data.candles[1:]),
            script(data),
            settings(),
            window(data),
            fx(),
        )
    selected = Window("test", date(2025, 1, 4), date(2025, 1, 7))
    run = simulate(data, script(data, *("enter_long",) * 6), settings(), selected, fx())
    assert fills(run)[0]["timestamp"] == data.candles[3].timestamp.isoformat()
    assert run == simulate(
        data, script(data, *("enter_long",) * 6), settings(), selected, fx()
    )
    assert D(fills(run)[0]["cash_after"]) == 950
    stock = dataset([10] * 5, kind="stock")
    with pytest.raises(DataError, match="no sessions"):
        simulate(
            stock,
            script(stock),
            settings(),
            Window("test", date(2025, 1, 4), date(2025, 1, 6)),
            fx(),
            actions=actions(stock),
        )


def test_no_trades_and_buy_hold_allocation() -> None:
    data = dataset([10, 10, 20])
    no_trades = simulate(data, script(data), settings(), window(data), fx())
    assert not fills(no_trades)
    assert metrics(no_trades)["closed_trades"] == 0
    assert metrics(no_trades)["win_rate"] is None
    benchmark = simulate(
        data, script(data), settings(), window(data), fx(), buy_and_hold=True
    )
    assert len(fills(benchmark)) == 1
    assert fills(benchmark)[0]["timestamp"] == data.candles[0].timestamp.isoformat()
    assert D(benchmark.equity[-1]["quantity"]) == 5
    assert D(benchmark.equity[-1]["equity"]) == 1050


@pytest.mark.parametrize(
    "key,value",
    [
        ("initial_cash", D(0)),
        ("target_eur", D(-1)),
        ("max_exposure", D("1.1")),
        ("max_loss", D(0)),
        ("max_drawdown", D("NaN")),
        ("delivery_delay_seconds", -1),
        ("delivery_delay_seconds", True),
        ("fx_max_age_hours", 0),
    ],
)
def test_invalid_settings(key: str, value: Any) -> None:
    with pytest.raises(DataError):
        settings(**{key: value})


@pytest.mark.parametrize("value", ["NaN", "Infinity", "nonsense", True])
def test_invalid_decimals(value: object) -> None:
    with pytest.raises(DataError):
        number(value)


def test_invalid_windows_and_fee_settings() -> None:
    with pytest.raises(DataError, match="training"):
        parse_windows([{"name": "test", "start": "2025-01-01", "end": "2025-02-01"}])
    with pytest.raises(DataError, match="quantity_step"):
        replace(settings().costs, quantity_step=D(0))
    with pytest.raises(DataError, match="nonnegative"):
        replace(settings().costs, fee_rate=D(-1))


def cli(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "crow_trading.backtesting", *args],
        capture_output=True,
        text=True,
    )


@pytest.fixture
def demo(tmp_path: Path) -> Path:
    directory = tmp_path / "fixtures"
    script_path = (
        Path(__file__).resolve().parents[1] / "examples/backtesting/make_demo.py"
    )
    subprocess.run(
        [sys.executable, str(script_path), "--output", str(directory)],
        capture_output=True,
        text=True,
        check=True,
    )
    return directory


def test_plan_hashes_reject_changed_inputs_and_parameters(demo: Path) -> None:
    plan = demo / "plan.json"
    freeze(demo / "crypto.json", plan)
    assert read_plan(plan)[0]["plan_hash"]
    config = json.loads(plan.read_text())
    config["configuration"]["settings"]["target_eur"] = "60"
    plan.write_text(json.dumps(config))
    with pytest.raises(DataError, match="plan hash mismatch"):
        read_plan(plan)
    second = demo / "second-plan.json"
    freeze(demo / "crypto.json", second)
    (demo / "fx.json").write_text((demo / "fx.json").read_text() + "\n")
    with pytest.raises(DataError, match="snapshot changed"):
        read_plan(second)


@pytest.mark.parametrize("kind", ["crypto", "stock"])
def test_cli_exports_reproducible_isolated_reports(demo: Path, kind: str) -> None:
    source = demo / f"{kind}.parquet"
    original = source.read_bytes()
    plan = demo / f"{kind}-plan.json"
    frozen = cli("freeze", str(demo / f"{kind}.json"), "--output", str(plan))
    assert frozen.returncode == 0, frozen.stderr
    first, second = demo / "run-a", demo / "run-b"
    for target in (first, second):
        result = cli("run", str(plan), "--output", str(target))
        assert result.returncode == 0, result.stderr
    files = {p.name: p.read_bytes() for p in first.iterdir()}
    assert files == {p.name: p.read_bytes() for p in second.iterdir()}
    report = json.loads(files["report.json"])
    assert set(report["results"]) == {"training", "validation", "test"}
    assert report["status"] == "success"
    for split in report["results"].values():
        assert D(split["strategy"]["initial_equity"]) == 1000
        assert D(split["buy_and_hold"]["initial_equity"]) == 1000
        assert D(split["cash"]["final_equity"]) == 1000
    assert b"<svg" in files["test-equity-drawdown.svg"]
    assert b"cash_delta" in files["test-strategy-ledger.csv"]
    assert source.read_bytes() == original
    refused = cli("run", str(plan), "--output", str(first))
    assert refused.returncode == 1
    assert files == {p.name: p.read_bytes() for p in first.iterdir()}


def test_failed_run_has_rejected_status_and_no_success_report(demo: Path) -> None:
    plan = demo / "plan.json"
    freeze(demo / "crypto.json", plan)
    snapshot = demo / "fx.json"
    snapshot.write_text(snapshot.read_text() + "\n")
    target = demo / "rejected"
    result = cli("run", str(plan), "--output", str(target))
    assert result.returncode == 1
    assert json.loads((target / "status.json").read_text())["status"] == "rejected"
    assert not (target / "report.json").exists()


def test_exit_fee_and_minimum_rejections_do_not_overdraw_cash() -> None:
    data = dataset([10, 10, 0.1, 0.1])
    cfg = settings(costs=replace(settings().costs, fixed_fee_quote=D(500)))
    run = simulate(
        data, script(data, "enter_long", "exit_long"), cfg, window(data), fx()
    )
    assert len(fills(run)) == 1
    assert any(r["reason"] == "insufficient_cash_for_exit_fee" for r in run.ledger)
    assert all(D(row["cash"]) >= 0 for row in run.equity)
    cfg = settings(costs=replace(settings().costs, minimum_notional_quote=D(1)))
    run = simulate(
        data, script(data, "enter_long", "exit_long"), cfg, window(data), fx()
    )
    assert any(r["reason"] == "exit_below_market_minimum" for r in run.ledger)


def test_annual_metrics_and_mixed_closed_trades() -> None:
    req = Request("BTC/EUR", date(2024, 1, 2), date(2025, 1, 2), "EUR", "crypto")
    expected, timezone = schedule(req)
    prices = [10.0, 10.0, 12.0, 10.0, 9.0, 10.0, 10.0]
    prices += [10.0] * (len(expected) - len(prices))
    data = Dataset(
        tuple(
            Candle(opened, closed, price, price + 1, price / 2, price, 100.0)
            for (opened, closed), price in zip(expected.items(), prices, strict=True)
        ),
        Metadata(req, "synthetic", "fixture", timezone, NOW, "annual fixture", "1"),
    )
    run = simulate(
        data,
        script(
            data,
            "enter_long",
            "exit_long",
            "enter_long",
            "exit_long",
            "enter_long",
            "exit_long",
        ),
        settings(),
        window(data),
        fx(),
    )
    result = metrics(run)
    assert run.closed_pnls == (D(10), D(-5), D(0))
    assert result["closed_trades"] == 3
    assert result["wins"] == result["losses"] == result["breakeven_trades"] == 1
    assert D(str(result["profit_factor"])) == 2
    assert D(str(result["max_drawdown"])) == D(5) / 1010
    assert D(str(result["turnover_over_initial_equity"])) == D("0.305")
    assert D("0.00498") < D(str(result["annualized_return"])) < D("0.005")


def test_warmup_uses_prior_bars_without_prior_positions() -> None:
    from crow_trading.strategies import Momentum

    data = dataset([10, 12, 14, 16, 18, 20])
    strategy = Momentum(2, 2)
    selected = Window("test", date(2025, 1, 5), date(2025, 1, 8))
    # First in-window close (Jan 5) has a complete prior reference window.
    run = simulate(data, strategy, settings(), selected, fx())
    assert fills(run)[0]["signal_timestamp"] == data.candles[3].timestamp.isoformat()
    assert fills(run)[0]["timestamp"] == data.candles[4].timestamp.isoformat()
    assert D(run.equity[0]["quantity"]) == 0
    assert D(run.equity[0]["cash"]) == 1000
