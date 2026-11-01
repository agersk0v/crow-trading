"""Offline calendar, snapshot, deduplication, and transactional recovery contracts."""

import json
import sqlite3
import subprocess
import sys
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

import pytest

from crow_trading.data.models import Candle, DataError, Dataset, Metadata, Request
from crow_trading.data.normalize import schedule
from crow_trading.data.storage import load
from crow_trading.scanner.config import Instrument, Watchlist, read_watchlist
from crow_trading.scanner.engine import scan
from crow_trading.scanner.state import Busy, State
from crow_trading.scanner.timing import due_sessions
from crow_trading.strategies import MeanReversion, Momentum
from crow_trading.strategies.runner import replay

NOW = datetime(2025, 1, 25, 0, 16, tzinfo=UTC)


def instrument(
    name: str = "btc", kind: Literal["crypto", "stock"] = "crypto"
) -> Instrument:
    return Instrument(
        name,
        kind,
        "ccxt" if kind == "crypto" else "yfinance",
        "kraken" if kind == "crypto" else "yahoo",
        ("BTC/EUR" if name == "btc" else "ETH/EUR") if kind == "crypto" else "AAPL",
        "EUR" if kind == "crypto" else "USD",
        "24/7" if kind == "crypto" else "XNYS",
        False,
        6,
        3,
        10,
        900 if kind == "crypto" else 1800,
        64800,
        (Momentum(2, 2), MeanReversion(2)),
    )


def watchlist(tmp_path: Path, *items: Instrument) -> Watchlist:
    return Watchlist(tmp_path / "state", items or (instrument(),), 3, 0)


class FakeProvider:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Request]] = []
        self.failures: dict[str, list[Exception]] = {}
        self.changes: dict[datetime, float] = {}
        self.truncate: set[str] = set()

    def __call__(self, item: Instrument, request: Request, now: datetime) -> Dataset:
        self.calls.append((item.name, request))
        if self.failures.get(item.name):
            raise self.failures[item.name].pop(0)
        expected, timezone = schedule(request)
        candles = []
        for opened, closed in expected.items():
            price = float(1000 + (opened.date() - datetime(2025, 1, 1).date()).days * 2)
            price += self.changes.get(opened, 0)
            candles.append(
                Candle(opened, closed, price, price + 1, price - 1, price, 100.0)
            )
        if item.name in self.truncate:
            candles.pop()
        return Dataset(
            tuple(candles),
            Metadata(
                request,
                "ccxt" if item.kind == "crypto" else "yfinance",
                item.venue if item.kind == "crypto" else "NMS",
                timezone,
                now,
                "synthetic provider fixture",
                "test-1",
            ),
        )


def count(root: Path, table: str) -> int:
    assert table in ("runs", "signals", "instruments")
    with sqlite3.connect(root / "scanner.sqlite3") as db:
        return int(db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def test_mixed_scan_replay_agreement_and_noop_restart(tmp_path: Path) -> None:
    config = watchlist(tmp_path, instrument(), instrument("aapl", "stock"))
    provider = FakeProvider()
    result = scan(config, clock=lambda: NOW, fetcher=provider)
    assert result.exit_code == 0 and result.report is not None
    assert len(provider.calls) == 2 and len(result.report["signals"]) == 4
    assert len(result.report["fresh_entries"]) == 2
    for record in result.report["signals"]:
        data = load(config.state_dir / record["snapshot_path"])
        strategy = next(
            s
            for i in config.instruments
            if i.name == record["instrument"]
            for s in i.strategies
            if s.strategy_id == record["strategy"]["id"]
        )
        assert replay(data, strategy)[-1].intent == record["intent"]
        assert len(data.candles) == 6
        assert data.candles[-1].closed_at <= NOW
    before = {p: p.read_bytes() for p in (config.state_dir / "reports").iterdir()}
    repeated = scan(config, clock=lambda: NOW + timedelta(minutes=1), fetcher=provider)
    assert repeated.report is None and repeated.exit_code == 0
    assert len(provider.calls) == 2 and count(config.state_dir, "runs") == 1
    assert all(p.read_bytes() == content for p, content in before.items())


def test_incremental_overlap_and_immutable_snapshots(tmp_path: Path) -> None:
    config, provider = watchlist(tmp_path), FakeProvider()
    first = scan(config, clock=lambda: NOW, fetcher=provider)
    assert first.report is not None
    old = config.state_dir / first.report["signals"][0]["snapshot_path"]
    original = old.read_bytes()
    second = scan(config, clock=lambda: NOW + timedelta(days=1), fetcher=provider)
    assert second.report is not None
    assert len(second.report["signals"]) == 2
    assert (
        len(schedule(provider.calls[-1][1])[0]) == 4
    )  # three overlap bars plus one new
    assert old.read_bytes() == original
    merged = load(config.state_dir / second.report["signals"][0]["snapshot_path"])
    assert len(merged.candles) == 7
    assert second.report["updates"][0]["previous_dataset_fingerprint"]


def test_catchup_is_historical_except_latest_fresh_bar(tmp_path: Path) -> None:
    config, provider = watchlist(tmp_path), FakeProvider()
    scan(config, clock=lambda: NOW, fetcher=provider)
    later = NOW + timedelta(days=3)
    result = scan(config, clock=lambda: later, fetcher=provider)
    assert result.report is not None
    assert len(result.report["signals"]) == 6
    assert sum(r["classification"] == "catchup" for r in result.report["signals"]) == 4
    assert len(result.report["fresh_entries"]) == 1
    assert (
        result.report["fresh_entries"][0]["candle_timestamp"]
        == due_sessions(instrument(), later).latest.isoformat()
    )


def test_stale_bootstrap_never_becomes_fresh_candidate(tmp_path: Path) -> None:
    item = replace(
        instrument(), finalization_delay_seconds=0, max_signal_age_seconds=60
    )
    result = scan(watchlist(tmp_path, item), clock=lambda: NOW, fetcher=FakeProvider())
    assert result.report is not None and result.report["fresh_entries"] == []
    assert all(r["classification"] == "catchup" for r in result.report["signals"])
    assert result.instruments[0]["freshness"] == "stale"


def test_slow_scan_expires_candidates_before_commit(tmp_path: Path) -> None:
    values = iter((NOW, NOW + timedelta(days=1)))
    result = scan(
        watchlist(tmp_path), clock=lambda: next(values), fetcher=FakeProvider()
    )
    assert result.report is not None and result.report["fresh_entries"] == []
    assert all(r["classification"] == "catchup" for r in result.report["signals"])


def test_expiry_is_capped_by_next_expected_publication(tmp_path: Path) -> None:
    item = replace(instrument(), max_signal_age_seconds=172800)
    result = scan(watchlist(tmp_path, item), clock=lambda: NOW, fetcher=FakeProvider())
    assert result.report is not None
    assert result.report["signals"][0]["expires_at"] == "2025-01-26T00:15:00+00:00"


@pytest.mark.parametrize(
    "time,day",
    [
        ("2025-01-25T00:14:59+00:00", 23),
        ("2025-01-25T00:15:00+00:00", 24),
    ],
)
def test_crypto_finalization_boundary(time: str, day: int) -> None:
    assert due_sessions(instrument(), datetime.fromisoformat(time)).latest.day == day


@pytest.mark.parametrize(
    "time,expected",
    [
        ("2025-01-20T22:00:00+00:00", "2025-01-17"),  # MLK holiday
        ("2025-03-07T21:29:59+00:00", "2025-03-06"),
        ("2025-03-07T21:30:00+00:00", "2025-03-07"),
        ("2025-03-10T20:29:59+00:00", "2025-03-07"),
        ("2025-03-10T20:30:00+00:00", "2025-03-10"),  # DST
        ("2025-11-28T18:29:59+00:00", "2025-11-26"),
        ("2025-11-28T18:30:00+00:00", "2025-11-28"),  # half-day
    ],
)
def test_stock_calendar_boundaries(time: str, expected: str) -> None:
    due = due_sessions(instrument("aapl", "stock"), datetime.fromisoformat(time))
    assert str(due.day(due.latest)) == expected


def test_provider_revision_creates_traceable_revision_not_repeat_candidate(
    tmp_path: Path,
) -> None:
    config, provider = watchlist(tmp_path), FakeProvider()
    first = scan(config, clock=lambda: NOW, fetcher=provider)
    assert first.report is not None
    original = Path(first.report_files[0]).read_bytes()
    stamp = due_sessions(instrument(), NOW).latest
    provider.changes[stamp] = 10.0
    revised = scan(config, clock=lambda: NOW, fetcher=provider, refresh=True)
    assert revised.report is not None and len(revised.report["signals"]) == 2
    assert revised.report["fresh_entries"] == []
    assert {r["classification"] for r in revised.report["signals"]} == {"revision"}
    assert revised.report["updates"][0]["revised_candles"] == [stamp.isoformat()]
    assert Path(first.report_files[0]).read_bytes() == original
    unchanged = scan(config, clock=lambda: NOW, fetcher=provider, refresh=True)
    assert unchanged.report is None and count(config.state_dir, "signals") == 4
    assert count(config.state_dir, "runs") == 2


def test_parameter_change_is_revision_and_not_duplicate_recommendation(
    tmp_path: Path,
) -> None:
    config, provider = watchlist(tmp_path), FakeProvider()
    scan(config, clock=lambda: NOW, fetcher=provider)
    changed = replace(
        config.instruments[0], strategies=(Momentum(3, 2), MeanReversion(2))
    )
    result = scan(
        replace(config, instruments=(changed,)), clock=lambda: NOW, fetcher=provider
    )
    assert result.report is not None
    assert len(result.report["signals"]) == 1
    assert result.report["signals"][0]["classification"] == "revision"
    assert result.report["fresh_entries"] == []


def test_partial_failure_retries_only_failed_asset_on_next_tick(tmp_path: Path) -> None:
    config = watchlist(tmp_path, instrument(), instrument("eth"))
    provider = FakeProvider()
    provider.failures["btc"] = [DataError("unsupported symbol")]
    first = scan(config, clock=lambda: NOW, fetcher=provider)
    assert first.exit_code == 1 and first.report is not None
    assert first.report["status"] == "partial_failure"
    assert {r["instrument"] for r in first.report["signals"]} == {"eth"}
    assert count(config.state_dir, "instruments") == 1
    second = scan(config, clock=lambda: NOW, fetcher=provider)
    assert second.exit_code == 0 and second.report is not None
    assert {r["instrument"] for r in second.report["signals"]} == {"btc"}
    assert [name for name, _ in provider.calls] == ["btc", "eth", "btc"]


def test_bounded_transient_retries_and_rate_spacing(tmp_path: Path) -> None:
    config, provider = watchlist(tmp_path), FakeProvider()
    provider.failures["btc"] = [TimeoutError("temporary"), ConnectionError("temporary")]
    slept: list[float] = []
    result = scan(config, clock=lambda: NOW, fetcher=provider, sleep=slept.append)
    assert result.exit_code == 0 and slept == [1.0, 2.0]
    assert len(provider.calls) == 3
    config = replace(
        watchlist(tmp_path / "another", instrument(), instrument("eth")),
        provider_interval_seconds=1,
    )
    slept.clear()
    scan(config, clock=lambda: NOW, fetcher=FakeProvider(), sleep=slept.append)
    assert slept == [1.0]


def test_missing_latest_data_is_failure_without_advancing_state(tmp_path: Path) -> None:
    config, provider = watchlist(tmp_path), FakeProvider()
    scan(config, clock=lambda: NOW, fetcher=provider)
    provider.truncate.add("btc")
    result = scan(config, clock=lambda: NOW + timedelta(days=1), fetcher=provider)
    assert result.exit_code == 1 and result.report is not None
    assert result.report["fresh_entries"] == []
    assert result.report["signals"] == []
    assert "coverage" in result.instruments[0]["error"]
    with State(config.state_dir) as state:
        old = state.previous(instrument().key)
        assert (
            old is not None
            and old["last_timestamp"]
            == due_sessions(instrument(), NOW).latest.isoformat()
        )


def test_backlog_limit_is_explicit_and_does_not_skip_bars(tmp_path: Path) -> None:
    config, provider = watchlist(tmp_path), FakeProvider()
    scan(config, clock=lambda: NOW, fetcher=provider)
    later = NOW + timedelta(days=12)
    blocked = scan(config, clock=lambda: later, fetcher=provider)
    assert blocked.exit_code == 1 and "max_catchup" in blocked.instruments[0]["error"]
    assert len(provider.calls) == 1
    increased = replace(config.instruments[0], max_catchup_bars=20)
    recovered = scan(
        replace(config, instruments=(increased,)), clock=lambda: later, fetcher=provider
    )
    assert recovered.report is not None and len(recovered.report["signals"]) == 24


def test_lock_rejects_concurrent_scan_without_provider_calls(tmp_path: Path) -> None:
    config, provider = watchlist(tmp_path), FakeProvider()
    with State(config.state_dir):
        with pytest.raises(Busy):
            scan(config, clock=lambda: NOW, fetcher=provider)
    assert provider.calls == []
    assert scan(config, clock=lambda: NOW, fetcher=provider).exit_code == 0


def test_crash_after_commit_repairs_reports_without_reprocessing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, provider = watchlist(tmp_path), FakeProvider()
    original = State.export
    calls = 0

    def interrupted(self: State) -> list[str]:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("simulated power loss after commit")
        return original(self)

    with monkeypatch.context() as patch:
        patch.setattr(State, "export", interrupted)
        with pytest.raises(OSError):
            scan(config, clock=lambda: NOW, fetcher=provider)
    assert count(config.state_dir, "runs") == 1
    assert not (config.state_dir / "reports").exists()
    recovered = scan(config, clock=lambda: NOW, fetcher=provider)
    assert recovered.report is None and len(recovered.report_files) == 2
    assert len(provider.calls) == 1 and count(config.state_dir, "signals") == 2


def test_crash_before_commit_rolls_back_progress_and_records(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, provider = watchlist(tmp_path), FakeProvider()
    scan(config, clock=lambda: NOW, fetcher=provider)

    def interrupted(*args: object, **kwargs: object) -> None:
        raise RuntimeError("simulated power loss before commit")

    with monkeypatch.context() as patch:
        patch.setattr(State, "report", interrupted)
        with pytest.raises(RuntimeError):
            scan(config, clock=lambda: NOW + timedelta(days=1), fetcher=provider)
    assert (
        count(config.state_dir, "runs") == 1 and count(config.state_dir, "signals") == 2
    )
    recovered = scan(config, clock=lambda: NOW + timedelta(days=1), fetcher=provider)
    assert recovered.report is not None and len(recovered.report["signals"]) == 2
    assert count(config.state_dir, "signals") == 4


def test_os_process_exit_releases_sqlite_lock(tmp_path: Path) -> None:
    config = watchlist(tmp_path)
    code = (
        "from pathlib import Path; from crow_trading.scanner.state import State; "
        "import os,sys\nwith State(Path(sys.argv[1])): os._exit(7)"
    )
    result = subprocess.run([sys.executable, "-c", code, str(config.state_dir)])
    assert result.returncode == 7
    assert scan(config, clock=lambda: NOW, fetcher=FakeProvider()).exit_code == 0


def test_modified_projection_is_preserved_and_missing_file_can_be_repaired(
    tmp_path: Path,
) -> None:
    config, provider = watchlist(tmp_path), FakeProvider()
    first = scan(config, clock=lambda: NOW, fetcher=provider)
    target = next(Path(p) for p in first.report_files if p.endswith(".md"))
    target.write_text("user annotation")
    with pytest.raises(DataError, match="projection conflict"):
        scan(config, clock=lambda: NOW, fetcher=provider)
    assert target.read_text() == "user annotation"
    target.rename(target.with_suffix(".notes"))
    recovered = scan(config, clock=lambda: NOW, fetcher=provider)
    assert str(target) in recovered.report_files
    assert target.with_suffix(".notes").read_text() == "user annotation"


def test_unrelated_database_is_not_modified(tmp_path: Path) -> None:
    root = tmp_path / "state"
    root.mkdir()
    with sqlite3.connect(root / "scanner.sqlite3") as db:
        db.execute("CREATE TABLE user_data(value TEXT)")
        db.execute("INSERT INTO user_data VALUES ('keep')")
    with pytest.raises(DataError, match="unrelated SQLite"):
        with State(root):
            pass
    with sqlite3.connect(root / "scanner.sqlite3") as db:
        assert db.execute("SELECT value FROM user_data").fetchone()[0] == "keep"


def test_invalid_watchlists(tmp_path: Path) -> None:
    raw: dict[str, Any] = {
        "schema_version": 1,
        "state_dir": "state",
        "attempts": 3,
        "provider_interval_seconds": 0,
        "instruments": [
            {
                "name": "btc",
                "kind": "crypto",
                "provider": "ccxt",
                "venue": "kraken",
                "symbol": "BTC/EUR",
                "currency": "EUR",
                "calendar": "24/7",
                "adjusted": False,
                "history_bars": 6,
                "overlap_bars": 3,
                "max_catchup_bars": 10,
                "finalization_delay_seconds": 900,
                "max_signal_age_seconds": 64800,
                "strategies": [
                    {
                        "id": "momentum",
                        "version": "1",
                        "parameters": {"entry_lookback": 2, "exit_lookback": 2},
                    }
                ],
            }
        ],
    }
    path = tmp_path / "watchlist.json"
    path.write_text(json.dumps(raw))
    assert read_watchlist(path).instruments[0].history_bars == 6
    for field, value in (
        ("history_bars", 1),
        ("history_bars", True),
        ("provider", "unknown"),
        ("calendar", "XNYS"),
        ("adjusted", "false"),
    ):
        changed = json.loads(json.dumps(raw))
        changed["instruments"][0][field] = value
        path.write_text(json.dumps(changed))
        with pytest.raises(DataError):
            read_watchlist(path)
    raw["instruments"][0]["strategies"][0]["version"] = "99"
    path.write_text(json.dumps(raw))
    with pytest.raises(DataError, match="version"):
        read_watchlist(path)


def test_numeric_context_identity_survives_float64_normalization() -> None:
    from crow_trading.scanner.datasets import context_hash

    opened = datetime(2025, 1, 1, tzinfo=UTC)
    integer_values = Candle(opened, opened + timedelta(days=1), 100, 101, 99, 100, 1000)
    float_values = Candle(
        opened, opened + timedelta(days=1), 100.0, 101.0, 99.0, 100.0, 1000.0
    )
    assert context_hash(instrument(), (integer_values,)) == context_hash(
        instrument(), (float_values,)
    )


def test_exhausted_retries_do_not_commit_asset_progress(tmp_path: Path) -> None:
    config, provider = watchlist(tmp_path), FakeProvider()
    provider.failures["btc"] = [TimeoutError("offline")] * 3
    slept: list[float] = []
    result = scan(config, clock=lambda: NOW, fetcher=provider, sleep=slept.append)
    assert result.exit_code == 1 and result.report is not None
    assert len(provider.calls) == 3 and slept == [1.0, 2.0]
    assert (
        count(config.state_dir, "instruments")
        == count(config.state_dir, "signals")
        == 0
    )
    assert result.report["fresh_entries"] == []


def test_overlap_revision_recalculates_affected_history_and_new_candle(
    tmp_path: Path,
) -> None:
    config, provider = watchlist(tmp_path), FakeProvider()
    scan(config, clock=lambda: NOW, fetcher=provider)
    prior = due_sessions(instrument(), NOW).latest
    provider.changes[prior - timedelta(days=1)] = 5.0
    result = scan(config, clock=lambda: NOW + timedelta(days=1), fetcher=provider)
    assert result.report is not None
    revised = [r for r in result.report["signals"] if r["classification"] == "revision"]
    assert len(revised) == 2 and {r["candle_timestamp"] for r in revised} == {
        prior.isoformat()
    }
    assert all(
        r["candle_timestamp"] != prior.isoformat()
        for r in result.report["fresh_entries"]
    )
    assert len(result.report["signals"]) == 4


def test_policy_only_change_is_audited_without_new_recommendations(
    tmp_path: Path,
) -> None:
    config, provider = watchlist(tmp_path), FakeProvider()
    scan(config, clock=lambda: NOW, fetcher=provider)
    modified = replace(config.instruments[0], max_signal_age_seconds=7200)
    result = scan(
        replace(config, instruments=(modified,)), clock=lambda: NOW, fetcher=provider
    )
    assert result.report is not None and result.report["signals"] == []
    assert result.report["updates"][0]["policy_changed"] is True
    assert result.report["updates"][0]["data_changed"] is False
    assert result.report["fresh_entries"] == []


def test_history_extension_preserves_existing_signal_identity(tmp_path: Path) -> None:
    config, provider = watchlist(tmp_path), FakeProvider()
    scan(config, clock=lambda: NOW, fetcher=provider)
    modified = replace(config.instruments[0], history_bars=12)
    result = scan(
        replace(config, instruments=(modified,)), clock=lambda: NOW, fetcher=provider
    )
    assert result.report is not None and result.report["signals"] == []
    with State(config.state_dir) as state:
        progress = state.previous(modified.key)
        assert progress is not None
        assert len(load(config.state_dir / progress["snapshot_path"]).candles) == 12
    assert count(config.state_dir, "signals") == 2


def test_clock_regression_rolls_back_batch(tmp_path: Path) -> None:
    config, provider = watchlist(tmp_path), FakeProvider()
    scan(config, clock=lambda: NOW, fetcher=provider)
    times = iter((NOW + timedelta(days=1), NOW))
    with pytest.raises(DataError, match="clock moved backwards"):
        scan(config, clock=lambda: next(times), fetcher=provider)
    assert (
        count(config.state_dir, "runs") == 1 and count(config.state_dir, "signals") == 2
    )


@pytest.mark.parametrize("offset,is_fresh", [(0, False), (-0.000001, True)])
def test_freshness_deadline_is_exclusive(
    tmp_path: Path, offset: float, is_fresh: bool
) -> None:
    item = replace(instrument(), max_signal_age_seconds=960)
    now = NOW + timedelta(seconds=offset)
    result = scan(watchlist(tmp_path, item), clock=lambda: now, fetcher=FakeProvider())
    assert result.report is not None
    assert bool(result.report["fresh_entries"]) is is_fresh


def test_cli_snapshot_mode_is_offline_and_never_reports_live_candidates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from functools import partial

    from crow_trading.data.storage import save
    from crow_trading.scanner import __main__ as cli

    source_config = json.loads(
        (Path(__file__).parents[1] / "examples/scanner/watchlist.json").read_text()
    )
    source_config["state_dir"] = "state"
    source_config["provider_interval_seconds"] = 0
    for raw in source_config["instruments"]:
        raw.update(
            provider="snapshot", venue="fixture", snapshot=raw["name"] + ".parquet"
        )
    path = tmp_path / "watchlist.json"
    path.write_text(json.dumps(source_config))
    config = read_watchlist(path)
    provider = FakeProvider()
    from crow_trading.scanner.timing import request_window

    for item in config.instruments:
        due = due_sessions(item, NOW)
        request = request_window(item, due, None, None)
        data = provider(item, request, NOW)
        # FakeProvider uses NMS for stocks; fixture venue remains explicit.
        data = replace(data, metadata=replace(data.metadata, venue="fixture"))
        assert item.snapshot is not None
        save(data, item.snapshot)
    monkeypatch.setattr(cli, "scan", partial(scan, clock=lambda: NOW))
    monkeypatch.setattr(sys, "argv", ["scanner", str(path)])
    assert cli.main() == 0
    output = json.loads(capsys.readouterr().out)
    assert output["fresh_entries"] == [] and output["new_signals"] == 4
    assert cli.main() == 0
    assert json.loads(capsys.readouterr().out)["status"] == "no_changes"


def test_cli_busy_and_invalid_configuration_exit_codes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from crow_trading.scanner import __main__ as cli

    source = Path(__file__).parents[1] / "examples/scanner/watchlist.json"
    raw = json.loads(source.read_text())
    raw["state_dir"] = "state"
    path = tmp_path / "watchlist.json"
    path.write_text(json.dumps(raw))
    monkeypatch.setattr(sys, "argv", ["scanner", str(path)])
    with State(tmp_path / "state"):
        assert cli.main() == 3
        assert json.loads(capsys.readouterr().err)["status"] == "busy"
    path.write_text("invalid JSON")
    assert cli.main() == 1
    assert json.loads(capsys.readouterr().err)["status"] == "error"
