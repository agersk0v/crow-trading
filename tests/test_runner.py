"""Foreground orchestration with real offline scanner/paper state and fake waits."""

import sqlite3
import subprocess
import sys
from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from crow_trading.data.models import DataError
from crow_trading.paper.engine import initialize, step
from crow_trading.paper.operations import control, inspect
from crow_trading.runner.__main__ import main
from crow_trading.runner.engine import ownership, run
from crow_trading.scanner.engine import ScanResult, scan
from crow_trading.scanner.state import Busy
from tests.test_paper import T0, Harness


class Timeline:
    def __init__(self, cycles: int, advance: timedelta = timedelta()) -> None:
        self.instant = T0
        self.cycles = cycles
        self.advance = advance
        self.waits: list[float] = []

    def now(self) -> datetime:
        return self.instant

    def wait(self, seconds: float) -> None:
        self.waits.append(seconds)
        if len(self.waits) >= self.cycles:
            raise KeyboardInterrupt
        self.instant += self.advance


def execute(h: Harness, timeline: Timeline, **kwargs: Any) -> list[str]:
    lines: list[str] = []
    with pytest.raises(KeyboardInterrupt):
        run(
            h.config,
            now=timeline.now,
            wait=timeline.wait,
            emit=lines.append,
            fetcher=h.fetch,
            **kwargs,
        )
    return lines


def test_complete_buy_sell_restart_and_no_refunding(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    lines = execute(h, Timeline(5, timedelta(days=1)))
    assert sum(" FILL " in line for line in lines) == 2
    assert any(" BUY " in line and " FILL " in line for line in lines)
    assert any(" SELL " in line and " FILL " in line for line in lines)
    assert any(
        "fee=" in line and "effective_at=" in line and "recorded_at=" in line
        for line in lines
    )
    assert any("cash=1021.75" in line and "total=1021.75" in line for line in lines)
    assert sum(" ORDER " in line for line in lines) == 2
    snapshot = inspect(h.config.state_dir)
    resumed = Timeline(2)
    resumed.instant = T0 + timedelta(days=4)
    restart_lines = execute(h, resumed)
    assert not any(" FILL " in line or " ORDER " in line for line in restart_lines)
    assert inspect(h.config.state_dir)["fills"] == snapshot["fills"]
    assert Decimal(inspect(h.config.state_dir)["accounts"][0]["cash"]) == Decimal(
        "1021.75"
    )
    seeds = [
        r for r in inspect(h.config.state_dir)["events"] if r["kind"] == "initial_cash"
    ]
    assert len(seeds) == 1


def test_immediate_sequential_ticks_no_change_and_fixed_delay(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    timeline = Timeline(2)
    calls: list[str] = []

    def scanner() -> ScanResult:
        calls.append("scan")
        return scan(h.watchlist, clock=timeline.now, fetcher=h.fetch)

    def paper() -> dict[str, Any]:
        calls.append("paper")
        # Simulate a tick taking longer than the configured interval.
        timeline.instant += timedelta(seconds=600)
        return step(h.config, timeline.now())

    lines = execute(h, timeline, scan_tick=scanner, paper_tick=paper)
    assert calls == ["scan", "paper", "scan", "paper"]
    assert timeline.waits == [300, 300]  # Full delay after completion; no catch-up.
    assert any("SCAN no_changes" in line for line in lines)
    assert any("reserved=51.50" in line and "total=1000" in line for line in lines)
    assert any("not a fill" in line for line in lines)
    assert all("Trades: 0" in line for line in lines if "Trades:" in line)


@pytest.mark.parametrize("failure", ["partial", "busy", "io"])
def test_degraded_scanner_still_processes_paper(tmp_path: Path, failure: str) -> None:
    h = Harness(tmp_path)
    h.scan()
    calls: list[str] = []

    def scanner() -> ScanResult:
        if failure == "busy":
            raise Busy("temporary scanner lock")
        if failure == "io":
            raise OSError("temporary export failure")
        return ScanResult(
            {"status": "partial_failure"},
            ({"instrument": "other", "status": "error", "error": "provider timeout"},),
            (),
            1,
            T0,
        )

    def paper() -> dict[str, Any]:
        calls.append("paper")
        return step(h.config, T0)

    lines = execute(h, Timeline(1), scan_tick=scanner, paper_tick=paper)
    assert calls == ["paper"]
    assert any("Cycle degraded" in line for line in lines)
    assert any(" ERROR " in line for line in lines)
    assert len(inspect(h.config.state_dir)["orders"]) == 1


def test_paper_operational_failure_retries_and_does_not_invent_equity(
    tmp_path: Path,
) -> None:
    h = Harness(tmp_path)
    calls = 0

    def paper() -> dict[str, Any]:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise sqlite3.OperationalError("database is locked")
        return step(h.config, T0)

    lines = execute(h, Timeline(2), paper_tick=paper)
    assert any("total=unavailable" in line for line in lines)
    assert any("total=1000" in line and "current" in line for line in lines)
    assert calls == 2


def test_old_valuation_labelled_stale_on_worker_failure(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    initialize(h.config, T0)
    h.scan()
    h.step()

    def failed() -> dict[str, Any]:
        raise OSError("disk unavailable")

    lines = execute(h, Timeline(1), paper_tick=failed)
    assert any("valuation_at=" in line and "stale" in line for line in lines)


def test_corrupt_account_fails_before_scan(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    initialize(h.config, T0)
    with sqlite3.connect(h.config.state_dir / "paper.sqlite3") as db:
        db.execute("UPDATE accounts SET cash='1234'")
    with pytest.raises(DataError):
        execute(h, Timeline(1), scan_tick=lambda: pytest.fail("must not scan"))


def test_policy_change_and_backwards_clock_fail_closed(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    initialize(h.config, T0)
    changed = replace(h.config, accounts=(replace(h.account, max_positions=1),))
    with pytest.raises(DataError, match="policy changed"):
        run(changed, now=lambda: T0)
    with pytest.raises(DataError, match="clock moved backwards"):
        run(h.config, now=lambda: T0 - timedelta(seconds=1))


def test_fatal_worker_failure_exits_and_releases_ownership(tmp_path: Path) -> None:
    h = Harness(tmp_path)

    def failed() -> dict[str, Any]:
        raise DataError("unsupported state")

    with pytest.raises(DataError, match="unsupported state"):
        execute(h, Timeline(1), paper_tick=failed)
    execute(h, Timeline(1))


@pytest.mark.parametrize("shared", ["paper", "scanner"])
def test_duplicate_runner_rejected_for_either_shared_root(
    tmp_path: Path, shared: str
) -> None:
    h = Harness(tmp_path)
    root = h.config.state_dir if shared == "paper" else h.watchlist.state_dir
    with ownership((root,)):
        with pytest.raises(Busy):
            run(h.config, now=lambda: T0)
    execute(h, Timeline(1))


def test_process_death_releases_runner_lock(tmp_path: Path) -> None:
    code = (
        "from pathlib import Path; from crow_trading.runner.engine import ownership; "
        "import sys;\nwith ownership((Path(sys.argv[1]),)):\n"
        " print('locked', flush=True)\n sys.stdin.read()\n"
    )
    process = subprocess.Popen(
        [sys.executable, "-c", code, str(tmp_path)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert process.stdout is not None
        assert process.stdout.readline().strip() == "locked"
        with pytest.raises(Busy):
            with ownership((tmp_path,)):
                pytest.fail("must be locked")
    finally:
        process.kill()
        process.communicate(timeout=10)
    with ownership((tmp_path,)):
        pass


def test_stop_resume_controls_still_work_between_cycles(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    initialize(h.config, T0)
    control(h.config.state_dir, "stop", h.account.name, now=T0)
    lines = execute(h, Timeline(1))
    assert any("stopped=True" in line for line in lines)
    assert not inspect(h.config.state_dir)["orders"]
    control(h.config.state_dir, "resume", h.account.name, now=T0)
    execute(h, Timeline(1))
    # Ignored old signals are not replayed on resume.
    assert not inspect(h.config.state_dir)["orders"]


@pytest.mark.parametrize("interval", [0, -1, float("nan"), float("inf")])
def test_invalid_interval_has_no_side_effects(tmp_path: Path, interval: float) -> None:
    h = Harness(tmp_path)
    with pytest.raises(DataError, match="finite positive"):
        run(h.config, interval)
    assert not h.config.state_dir.exists()


def test_cli_shutdown_and_error_codes(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(sys, "argv", ["runner", "config.json"])
    monkeypatch.setattr("crow_trading.runner.__main__.read_config", lambda path: None)

    def interrupted(*args: Any, **kwargs: Any) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr("crow_trading.runner.__main__.run", interrupted)
    assert main() == 0
    assert "Saved accounts" in capsys.readouterr().out

    def busy(*args: Any, **kwargs: Any) -> None:
        raise Busy("already running")

    monkeypatch.setattr("crow_trading.runner.__main__.run", busy)
    assert main() == 3
    assert "already running" in capsys.readouterr().err


def test_interrupt_inside_paper_transaction_rolls_back(tmp_path: Path) -> None:
    from crow_trading.paper.state import State

    h = Harness(tmp_path)

    def interrupted() -> dict[str, Any]:
        with State(h.config.state_dir) as state:
            state.connection.execute("UPDATE accounts SET cash='0'")
            raise KeyboardInterrupt

    execute(h, Timeline(1), paper_tick=interrupted)
    assert inspect(h.config.state_dir)["accounts"][0]["cash"] == "1000"
    execute(h, Timeline(1))  # No abandoned lock or damaged financial state.


def test_configurable_interval_and_degraded_account_errors(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    timeline = Timeline(1)

    def degraded() -> dict[str, Any]:
        result = step(h.config, T0)
        result["status"] = "degraded"
        result["accounts"][0]["errors"] = ["missing execution observation"]
        return result

    lines = execute(h, timeline, interval=60, paper_tick=degraded)
    assert timeline.waits == [60]
    assert any(
        "ERROR momentum: missing execution observation" in line for line in lines
    )
    assert any("Cycle degraded" in line for line in lines)


def test_busy_preflight_retries_without_scanning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = Harness(tmp_path)
    real = initialize
    calls = 0

    def initialize_once_busy(*args: Any, **kwargs: Any) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise Busy("manual control in progress")
        return real(*args, **kwargs)

    monkeypatch.setattr("crow_trading.runner.engine.initialize", initialize_once_busy)
    lines = execute(h, Timeline(2))
    assert sum(" SCAN " in line for line in lines) == 1
    assert any("state unavailable" in line for line in lines)


def test_cli_invalid_configuration_returns_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(sys, "argv", ["runner", "missing-config.json"])
    assert main() == 1
    assert "Runner stopped on error" in capsys.readouterr().err
