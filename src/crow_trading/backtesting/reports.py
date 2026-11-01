"""Standalone CSV/JSON/SVG artifacts for successful, fully evaluated experiments."""

import csv
import json
from datetime import datetime
from pathlib import Path
from typing import Any

from crow_trading.backtesting.engine import Simulation, simulate
from crow_trading.backtesting.experiment import read_plan
from crow_trading.backtesting.metrics import metrics


def csv_file(path: Path, rows: tuple[dict[str, str], ...], fields: list[str]) -> None:
    with path.open("x", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def chart(
    path: Path, run: Simulation, benchmark: Simulation, cash: str, currency: str
) -> None:
    # Headless Figure API avoids GUI state and user-facing server processes.
    import matplotlib as mpl
    from matplotlib.backends.backend_svg import FigureCanvasSVG
    from matplotlib.figure import Figure

    with mpl.rc_context({"svg.hashsalt": "crow-backtest-v1"}):
        figure = Figure(figsize=(9, 6), layout="constrained")
        FigureCanvasSVG(figure)
        top, bottom = figure.subplots(2, 1, sharex=True)
        times = [datetime.fromisoformat(row["timestamp"]) for row in run.equity]
        top.plot(times, [float(row["equity"]) for row in run.equity], label="Strategy")
        top.plot(
            times,
            [float(row["equity"]) for row in benchmark.equity],
            label="Buy and hold",
        )
        top.axhline(float(cash), color="gray", linestyle="--", label="Cash")
        top.set(ylabel=f"Equity ({currency})", title=run.window.name)
        top.legend()
        bottom.plot(times, [-100 * float(row["drawdown"]) for row in run.equity])
        bottom.set(ylabel="Drawdown (%)", xlabel="UTC")
        figure.savefig(path, format="svg", metadata={"Date": None})


def execute(plan_path: Path, destination: Path) -> dict[str, Any]:
    """Destination is exclusively reserved by the CLI; compute before exporting."""
    plan, prepared = read_plan(plan_path)
    runs: list[tuple[Simulation, Simulation]] = []
    for window in prepared.windows:
        strategy = simulate(
            prepared.dataset,
            prepared.strategy,
            prepared.settings,
            window,
            prepared.fx,
            actions=prepared.actions,
        )
        benchmark = simulate(
            prepared.dataset,
            prepared.strategy,
            prepared.settings,
            window,
            prepared.fx,
            actions=prepared.actions,
            buy_and_hold=True,
        )
        runs.append((strategy, benchmark))
    results: dict[str, Any] = {}
    for strategy, benchmark in runs:
        prefix = strategy.window.name
        results[prefix] = {
            "window": {
                "start": str(strategy.window.start),
                "end": str(strategy.window.end),
            },
            "strategy": metrics(strategy),
            "buy_and_hold": metrics(benchmark),
            "cash": {
                "final_equity": str(prepared.settings.initial_cash),
                "net_return": "0",
            },
        }
        for name, run in (("strategy", strategy), ("buy-hold", benchmark)):
            # A no-trade run still exports a valid header-only fill/ledger file.
            ledger_fields = (
                list(run.ledger[0])
                if run.ledger
                else [
                    "timestamp",
                    "symbol",
                    "status",
                    "reason",
                    "cash_delta",
                    "quantity_delta",
                ]
            )
            csv_file(
                destination / f"{prefix}-{name}-ledger.csv", run.ledger, ledger_fields
            )
            fills = tuple(r for r in run.ledger if r["status"] == "filled")
            csv_file(destination / f"{prefix}-{name}-trades.csv", fills, ledger_fields)
            csv_file(
                destination / f"{prefix}-{name}-equity.csv",
                run.equity,
                list(run.equity[0]),
            )
        chart(
            destination / f"{prefix}-equity-drawdown.svg",
            strategy,
            benchmark,
            str(prepared.settings.initial_cash),
            prepared.settings.account_currency,
        )
    # Report is written only after all splits and exports succeed.
    summary = {
        "status": "success",
        "plan": plan,
        "results": results,
        "assumptions": {
            "portfolio": "one symbol; each split and benchmark starts with fresh cash",
            "terminal": (
                "mark open positions at last close/available FX; no forced liquidation"
            ),
            "benchmark": (
                "one target-EUR allocation at first open; "
                "same entry limits/costs; no risk exits"
            ),
            "cash": "constant account-currency cash; no interest or tax",
            "gross": (
                "same-path fee and paid execution-impact "
                "addback; not a frictionless rerun"
            ),
            "corporate_actions": (
                "unadjusted stock windows attested to contain no actions only"
            ),
            "fx": (
                "as-of snapshot rates, EUR triangulation; "
                "no extra FX conversion commission"
            ),
            "fill": (
                "next bar open at/after signal availability plus delay; full fills only"
            ),
            "point_in_time": (
                "snapshot hashes ensure repeatability, "
                "not historical provider availability"
            ),
        },
    }
    with (destination / "report.json").open(
        "x", encoding="utf-8", newline="\n"
    ) as stream:
        stream.write(json.dumps(summary, sort_keys=True, indent=2) + "\n")
    return summary
