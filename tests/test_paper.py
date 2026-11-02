"""Local paper accounts: prospective fills, atomic cash, and durable handoff."""

import json
import sqlite3
from dataclasses import replace
from datetime import date, datetime, timedelta
from functools import partial
from pathlib import Path

import pytest

from crow_trading.backtesting.models import Costs
from crow_trading.data.models import DataError, Dataset, Request
from crow_trading.paper.config import Asset
from crow_trading.paper.engine import initialize, step
from crow_trading.paper.operations import control, inspect
from crow_trading.paper.state import State
from crow_trading.scanner.config import Instrument, definition
from crow_trading.scanner.engine import scan
from crow_trading.strategies import MeanReversion
from tests.helpers.paper import T0, D, Harness


def fixed_clock(instant: datetime) -> datetime:
    return instant


def test_persistent_roundtrip_costs_and_no_earlier_open(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.scan()
    initialize(h.config, T0)
    assert h.step()["status"] == "success"
    order = h.rows("orders")[0]
    assert order["status"] == "pending" and D(order["reserved"]) == D("51.5")
    assert D(h.rows("accounts")[0]["cash"]) == 1000
    assert D(h.rows("accounts")[0]["reserved"]) == D("51.5")
    h.step()
    assert len(h.rows("orders")) == 1
    h.scan(1)
    h.step(1)
    assert h.rows("fills") == []  # Jan 25 open preceded Jan 25 00:16 submission.
    h.scan(2)
    assert h.step(2)["status"] == "success"
    buy = json.loads(h.rows("fills")[0]["payload"])
    assert buy["effective_at"] == "2025-01-26T00:00:00+00:00"
    assert buy["recorded_at"] == (T0 + timedelta(days=2)).isoformat()
    assert D(buy["quantity"]) == 5 and D(buy["fee_account"]) == D("1.5")
    assert D(h.rows("accounts")[0]["cash"]) == D("948.5")
    h.scan(3)
    h.step(3)
    assert len(h.rows("fills")) == 1
    h.scan(4)
    h.step(4)
    sell = json.loads(h.rows("fills")[1]["payload"])
    assert sell["effective_at"] == "2025-01-28T00:00:00+00:00"
    assert D(sell["fee_account"]) == D("1.75")
    assert D(sell["realized_pnl"]) == D("21.75")
    assert D(h.rows("accounts")[0]["cash"]) == D("1021.75")
    assert D(h.rows("positions")[0]["quantity"]) == 0
    before = len(h.rows("events"))
    h.step(4)
    assert len(h.rows("fills")) == 2 and len(h.rows("events")) == before
    assert inspect(h.config.state_dir)["reconciliation_errors"] == {}


def test_shared_cash_reservations_and_position_count(tmp_path: Path) -> None:
    h = Harness(tmp_path, 2)
    h.account = replace(h.account, max_positions=1)
    h.config = replace(h.config, accounts=(h.account,))
    h.scan()
    initialize(h.config, T0)
    h.step()
    assert len(h.rows("orders")) == 1
    assert {r["reason"] for r in h.rows("decisions")} == {
        "order_created",
        "position_count_limit",
    }
    assert D(h.rows("accounts")[0]["reserved"]) == D("51.5")


@pytest.mark.parametrize(
    "cash,exposure,reason",
    [
        ("100", "1", "insufficient_unreserved_cash"),
        ("1000", "0.06", "shared_exposure_limit"),
    ],
)
def test_shared_limits_across_multiple_assets(
    tmp_path: Path, cash: str, exposure: str, reason: str
) -> None:
    h = Harness(tmp_path, 2)
    account = replace(
        h.account,
        settings=replace(
            h.account.settings, initial_cash=D(cash), max_exposure=D(exposure)
        ),
    )
    h.config = replace(h.config, accounts=(account,))
    h.scan()
    initialize(h.config, T0)
    h.step()
    assert len(h.rows("orders")) == 1
    assert reason in {row["reason"] for row in h.rows("decisions")}
    assert D(h.rows("accounts")[0]["reserved"]) <= D(cash)


def test_strategy_accounts_do_not_sell_each_others_positions(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    other = replace(h.account, name="mean", strategy=definition(MeanReversion(2)))
    h.config = replace(h.config, accounts=(h.account, other))
    h.scan()
    initialize(h.config, T0)
    h.step()
    assert {r["account_id"] for r in h.rows("orders")} == {"momentum"}
    assert any(
        r["account_id"] == "mean" and r["reason"] == "not_long"
        for r in h.rows("decisions")
    )
    h.scan(1)
    h.step(1)
    h.scan(2)
    h.step(2)
    assert (
        D(
            next(r for r in h.rows("positions") if r["account_id"] == "momentum")[
                "quantity"
            ]
        )
        == 5
    )
    assert D(next(r for r in h.rows("accounts") if r["id"] == "mean")["cash"]) == 1000


def test_manual_stop_releases_entries_and_resume_does_not_replay_old_signal(
    tmp_path: Path,
) -> None:
    h = Harness(tmp_path)
    h.scan()
    initialize(h.config, T0)
    h.step()
    control(h.config.state_dir, "stop", account_id="momentum", now=T0)
    assert h.rows("orders")[0]["status"] == "cancelled"
    assert D(h.rows("accounts")[0]["reserved"]) == 0
    control(h.config.state_dir, "resume", account_id="momentum", now=T0)
    h.step()
    assert len(h.rows("orders")) == 1
    h.scan(1)
    h.step(1)
    assert len(h.rows("orders")) == 2


def test_stop_keeps_exits_and_reconciliation_active(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.scan()
    initialize(h.config, T0)
    h.step()
    for day in (1, 2):
        h.scan(day)
        h.step(day)
    control(
        h.config.state_dir, "stop", account_id="momentum", now=T0 + timedelta(days=2)
    )
    assert any(
        r["side"] == "sell" and r["status"] == "pending" for r in h.rows("orders")
    )
    for day in (3, 4):
        h.scan(day)
        h.step(day)
    assert len(h.rows("fills")) == 2
    assert h.rows("accounts")[0]["stopped"] == 1


def test_idempotent_initialization_and_policy_changes_are_rejected(
    tmp_path: Path,
) -> None:
    h = Harness(tmp_path)
    initialize(h.config, T0)
    initialize(h.config, T0)
    assert len(h.rows("events")) == 1
    assert D(h.rows("accounts")[0]["cash"]) == 1000
    changed = replace(h.account, max_positions=1)
    with pytest.raises(DataError, match="policy changed"):
        initialize(replace(h.config, accounts=(changed,)), T0)


def test_unknown_committed_fill_state_blocks_new_activity(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.scan()
    initialize(h.config, T0)
    h.step()
    with sqlite3.connect(h.config.state_dir / "paper.sqlite3") as db:
        db.execute("UPDATE orders SET status='filled'")
    with pytest.raises(DataError, match="reconciliation"):
        h.step()


def test_database_migration_and_unrelated_database_protection(tmp_path: Path) -> None:
    root = tmp_path / "paper"
    root.mkdir()
    sqlite3.connect(root / "paper.sqlite3").close()
    h = Harness(tmp_path)
    initialize(h.config, T0)
    with sqlite3.connect(root / "paper.sqlite3") as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 1
    other = tmp_path / "unrelated"
    other.mkdir()
    with sqlite3.connect(other / "paper.sqlite3") as db:
        db.execute("CREATE TABLE keep(value TEXT)")
    with pytest.raises(DataError, match="unrelated"):
        with State(other, create=True):
            pass


def test_crash_before_fill_commit_rolls_back_and_retry_fills_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = Harness(tmp_path)
    h.scan()
    initialize(h.config, T0)
    h.step()
    h.scan(1)
    h.step(1)
    h.scan(2)
    original = State.commit

    def interrupted(self: State) -> None:
        raise RuntimeError("before commit")

    with monkeypatch.context() as patch:
        patch.setattr(State, "commit", interrupted)
        with pytest.raises(RuntimeError, match="before commit"):
            h.step(2)
    assert h.rows("fills") == []
    assert h.rows("orders")[0]["status"] == "pending"
    assert D(h.rows("accounts")[0]["cash"]) == 1000
    assert State.commit is original
    h.step(2)
    h.step(2)
    assert len(h.rows("fills")) == 1
    assert D(h.rows("accounts")[0]["cash"]) == D("948.5")


def test_unknown_commit_outcome_reconciles_before_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = Harness(tmp_path)
    h.scan()
    initialize(h.config, T0)
    h.step()
    h.scan(1)
    h.step(1)
    h.scan(2)
    original = State.commit

    def interrupted(self: State) -> None:
        original(self)
        raise RuntimeError("lost acknowledgement after commit")

    with monkeypatch.context() as patch:
        patch.setattr(State, "commit", interrupted)
        with pytest.raises(RuntimeError, match="lost acknowledgement"):
            h.step(2)
    assert len(h.rows("fills")) == 1
    h.step(2)
    assert len(h.rows("fills")) == 1
    assert inspect(h.config.state_dir)["reconciliation_errors"] == {}


def test_concurrent_worker_and_lock_release(tmp_path: Path) -> None:
    from crow_trading.scanner.state import Busy

    h = Harness(tmp_path)
    h.scan()
    initialize(h.config, T0)
    with State(h.config.state_dir):
        with pytest.raises(Busy):
            h.step()
    h.step()
    h.step()
    assert len(h.rows("orders")) == 1


def test_revision_cancels_pending_order_without_retrading(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.scan()
    initialize(h.config, T0)
    h.step()
    h.prices[date(2025, 1, 24)] = (12, 12)
    scan(h.watchlist, clock=lambda: T0, fetcher=h.fetch, refresh=True)
    h.step()
    assert h.rows("orders")[0]["status"] == "cancelled"
    assert h.rows("orders")[0]["reason"] == "signal_superseded"
    assert D(h.rows("accounts")[0]["reserved"]) == 0
    assert any(r["reason"] == "non_fresh_signal" for r in h.rows("decisions"))


def test_expired_catchup_and_fixture_records_do_not_create_default_paper_orders(
    tmp_path: Path,
) -> None:
    h = Harness(tmp_path)
    h.scan()
    initialize(h.config, T0)
    h.step(1)
    assert h.rows("orders") == []
    assert h.rows("decisions")[0]["reason"] == "expired_signal"
    other = Harness(tmp_path / "other")
    other.config = replace(other.config, mode="paper")
    other.scan()
    initialize(other.config, T0)
    other.step()
    assert other.rows("orders") == []
    assert other.rows("decisions")[0]["reason"] == "fixture_mode_mismatch"


def test_global_loss_stop_persists_and_resume_cannot_clear_risk_halt(
    tmp_path: Path,
) -> None:
    free = Costs(D(0), D(0), D(0), D(0), D(1), D(0), D(0))
    h = Harness(tmp_path, costs=free)
    account = replace(
        h.account, settings=replace(h.account.settings, max_loss=D("0.01"))
    )
    h.config = replace(h.config, accounts=(account,))
    h.prices[date(2025, 1, 26)] = (10, 5)
    h.scan()
    initialize(h.config, T0)
    h.step()
    for day in (1, 2):
        h.scan(day)
        h.step(day)
    assert h.rows("accounts")[0]["risk_halted"] == 1
    assert any(
        row["reason"] == "risk:loss_limit" and row["side"] == "sell"
        for row in h.rows("orders")
    )
    control(
        h.config.state_dir, "resume", account_id="momentum", now=T0 + timedelta(days=2)
    )
    assert h.rows("accounts")[0]["risk_halted"] == 1


def test_missing_fx_defers_without_consuming_signal_and_can_retry(
    tmp_path: Path,
) -> None:
    h = Harness(tmp_path)
    account = replace(
        h.account,
        settings=replace(
            h.account.settings, account_currency="USD", fx_max_age_hours=1000
        ),
    )
    h.config = replace(h.config, accounts=(account,))
    h.scan()
    initialize(h.config, T0)
    result = h.step()
    assert result["status"] == "degraded" and h.rows("orders") == []
    assert h.rows("decisions")[0]["status"] == "deferred"
    h.fx.write_text(
        json.dumps(
            {
                "base": "EUR",
                "source": "synthetic USD rate",
                "rates": {
                    "USD": [
                        {"available_at": "2025-01-01T00:00:00Z", "eur_per_unit": "0.5"}
                    ]
                },
            }
        )
    )
    result = h.step()
    assert result["status"] == "success"
    assert len(h.rows("orders")) == 1
    assert h.rows("orders")[0]["account_id"] == "momentum"
    assert len(h.rows("decisions")) == 1
    assert h.rows("decisions")[0]["status"] == "accepted"


def test_no_market_or_broker_network_calls_in_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import socket

    h = Harness(tmp_path)
    h.scan()
    initialize(h.config, T0)

    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("paper worker attempted a network call")

    with monkeypatch.context() as patch:
        patch.setattr(socket.socket, "connect", forbidden)
        patch.setattr(socket, "create_connection", forbidden)
        assert h.step()["status"] == "success"


def test_unknown_order_state_and_corrupt_cash_fail_closed(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.scan()
    initialize(h.config, T0)
    h.step()
    with sqlite3.connect(h.config.state_dir / "paper.sqlite3") as db:
        db.execute("UPDATE orders SET status='unknown_remote_result'")
        db.execute("UPDATE accounts SET reserved='0'")
    with pytest.raises(DataError, match="unknown order state"):
        h.step()
    with sqlite3.connect(h.config.state_dir / "paper.sqlite3") as db:
        db.execute("UPDATE orders SET status='pending'")
        db.execute("UPDATE accounts SET cash='999',reserved='51.5'")
    with pytest.raises(DataError, match="cash ledger"):
        h.step()


def test_stock_session_fill_and_quote_currency_fees(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    item = replace(
        h.items[0], kind="stock", symbol="AAPL", currency="USD", calendar="XNYS"
    )
    actions = tmp_path / "actions.json"
    actions.write_text(
        json.dumps(
            {
                "source": "synthetic no-action interval",
                "symbol": "AAPL",
                "start": "2025-01-01",
                "end": "2025-02-01",
                "coverage_complete": True,
                "prices_unadjusted": True,
                "events": [],
            }
        )
    )
    h.fx.write_text(
        json.dumps(
            {
                "base": "EUR",
                "source": "synthetic USD rate",
                "rates": {
                    "USD": [
                        {"available_at": "2025-01-24T12:00:00Z", "eur_per_unit": "0.5"}
                    ]
                },
            }
        )
    )
    h.watchlist = replace(h.watchlist, instruments=(item,))
    account = replace(h.account, assets=(Asset(item, h.costs, actions),))
    h.config = replace(h.config, watchlist=h.watchlist, accounts=(account,))
    h.scan()
    initialize(h.config, T0)
    h.step()
    for day in (1, 2):
        h.scan(day)
        h.step(day)
    assert h.rows("fills") == []  # Weekend, then Monday session not closed yet.
    h.scan(3)
    result = h.step(3)
    assert result["status"] == "success"
    fill = json.loads(h.rows("fills")[0]["payload"])
    assert fill["effective_at"] == "2025-01-27T14:30:00+00:00"
    assert D(fill["quantity"]) == 7
    assert D(fill["fee_account"]) == D("0.955")
    assert fill["quote_currency"] == "USD" and D(fill["quote_to_account_fx"]) == D(
        "0.5"
    )
    assert fill["action_fingerprints"]


def test_currency_gap_cannot_borrow_from_unreserved_cash(tmp_path: Path) -> None:
    free = Costs(D(0), D(0), D(0), D(0), D(1), D(0), D(0))
    h = Harness(tmp_path, costs=free)
    h.fx.write_text(
        json.dumps(
            {
                "base": "EUR",
                "source": "synthetic USD rates",
                "rates": {
                    "USD": [
                        {"available_at": "2025-01-24T00:00:00Z", "eur_per_unit": "0.5"},
                        {"available_at": "2025-01-26T00:00:00Z", "eur_per_unit": "0.4"},
                    ]
                },
            }
        )
    )
    account = replace(
        h.account, settings=replace(h.account.settings, account_currency="USD")
    )
    h.config = replace(h.config, accounts=(account,))
    h.scan()
    initialize(h.config, T0)
    h.step()
    assert D(h.rows("accounts")[0]["reserved"]) == 100
    for day in (1, 2):
        h.scan(day)
        h.step(day)
    assert not h.rows("fills")
    assert h.rows("orders")[0]["reason"] == "reservation_exceeded"
    assert D(h.rows("accounts")[0]["cash"]) == 1000


def test_missing_stock_action_evidence_defers_without_reserving_money(
    tmp_path: Path,
) -> None:
    h = Harness(tmp_path)
    item = replace(h.items[0], kind="stock", symbol="AAPL", calendar="XNYS")
    h.watchlist = replace(h.watchlist, instruments=(item,))
    account = replace(h.account, assets=(Asset(item, h.costs),))
    h.config = replace(h.config, watchlist=h.watchlist, accounts=(account,))
    h.scan()
    initialize(h.config, T0)
    result = h.step()
    assert result["status"] == "degraded"
    assert h.rows("orders") == []
    assert h.rows("decisions")[0]["status"] == "deferred"
    assert D(h.rows("accounts")[0]["reserved"]) == 0


def test_scanner_state_and_reports_are_not_modified(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.watchlist = replace(h.watchlist, export_reports=True)
    h.config = replace(h.config, watchlist=h.watchlist)
    h.scan()
    files = [
        h.watchlist.state_dir / "scanner.sqlite3",
        *list((h.watchlist.state_dir / "reports").iterdir()),
    ]
    original = {path: path.read_bytes() for path in files}
    initialize(h.config, T0)
    h.step()
    h.step()
    assert all(path.read_bytes() == content for path, content in original.items())


def test_stale_marks_block_entries_and_manual_stop_works_without_inputs(
    tmp_path: Path,
) -> None:
    h = Harness(tmp_path)
    h.scan()
    initialize(h.config, T0)
    h.step()
    h.fx.unlink()
    control(h.config.state_dir, "stop", account_id="momentum", now=T0)
    assert h.rows("accounts")[0]["stopped"] == 1
    assert D(h.rows("accounts")[0]["reserved"]) == 0


def test_real_process_exit_rolls_back_local_financial_update(tmp_path: Path) -> None:
    import subprocess
    import sys

    h = Harness(tmp_path)
    h.scan()
    initialize(h.config, T0)
    code = (
        "from pathlib import Path; "
        "from crow_trading.paper.state import State; import os,sys\n"
        "with State(Path(sys.argv[1])) as s:\n"
        " s.connection.execute(\"UPDATE accounts SET cash='999'\")\n os._exit(7)"
    )
    result = subprocess.run([sys.executable, "-c", code, str(h.config.state_dir)])
    assert result.returncode == 7
    h.step()
    assert D(h.rows("accounts")[0]["cash"]) == 1000
    assert len(h.rows("orders")) == 1


def test_synthetic_cli_demo_export_and_unsupported_policy(tmp_path: Path) -> None:
    import subprocess
    import sys

    root = Path(__file__).parents[1]
    destination = tmp_path / "demo"
    subprocess.run(
        [
            sys.executable,
            str(root / "examples/paper/demo.py"),
            "--output",
            str(destination),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    report = json.loads((destination / "report/paper-report.json").read_text())
    assert len(report["fills"]) == 2 and not report["reconciliation_errors"]
    assert D(report["accounts"][0]["cash"]) == D("1021.75")
    inspected = subprocess.run(
        [
            sys.executable,
            "-m",
            "crow_trading.paper",
            "inspect",
            "--state",
            str(destination / "paper-state"),
            "--section",
            "fills",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    assert len(json.loads(inspected.stdout)["fills"]) == 2
    raw = json.loads((destination / "paper.json").read_text())
    raw["fill_policy"] = "partially_filled"
    invalid = destination / "invalid.json"
    invalid.write_text(json.dumps(raw))
    result = subprocess.run(
        [sys.executable, "-m", "crow_trading.paper", "init", str(invalid)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1
    assert "full fills" in result.stderr


def test_stale_held_asset_blocks_new_shared_account_entries(tmp_path: Path) -> None:
    h = Harness(tmp_path, 2)

    def feed(item: Instrument, request: Request, now: datetime) -> Dataset:
        data = h.fetch(item, request, now)
        if item.name != "asset1":
            return data
        return replace(
            data,
            candles=tuple(
                replace(
                    c,
                    open=20.0 if c.timestamp.date() >= date(2025, 1, 27) else 10.0,
                    high=20.0 if c.timestamp.date() >= date(2025, 1, 27) else 10.0,
                    low=20.0 if c.timestamp.date() >= date(2025, 1, 27) else 10.0,
                    close=20.0 if c.timestamp.date() >= date(2025, 1, 27) else 10.0,
                )
                for c in data.candles
            ),
        )

    scan(h.watchlist, clock=lambda: T0, fetcher=feed)
    initialize(h.config, T0)
    h.step()
    for day in (1, 2):
        scan(
            h.watchlist,
            clock=partial(fixed_clock, T0 + timedelta(days=day)),
            fetcher=feed,
        )
        h.step(day)
    # Only the unheld asset gets an updated source snapshot on the next day.
    scan(
        replace(h.watchlist, instruments=(h.items[1],)),
        clock=lambda: T0 + timedelta(days=3),
        fetcher=feed,
    )
    result = h.step(3)
    assert result["status"] == "degraded"
    assert any("behind" in error for error in result["accounts"][0]["errors"])
    assert any(d["reason"] == "valuation_unavailable" for d in h.rows("decisions"))
    assert not any(
        o["instrument_key"] == h.items[1].key and o["side"] == "buy"
        for o in h.rows("orders")
    )


def test_missing_first_execution_bar_never_uses_a_later_substitute(
    tmp_path: Path,
) -> None:
    h = Harness(tmp_path)
    h.scan()
    initialize(h.config, T0)
    h.step()
    replacement = replace(h.watchlist, state_dir=tmp_path / "new-scanner")
    later = T0 + timedelta(days=120)
    scan(replacement, clock=lambda: later, fetcher=h.fetch)
    h.config = replace(h.config, watchlist=replacement)
    result = step(h.config, later)
    assert result["status"] == "degraded"
    assert any(
        "execution bar is missing" in error for error in result["accounts"][0]["errors"]
    )
    assert h.rows("fills") == []
    assert h.rows("orders")[0]["status"] == "pending"


def test_changed_signal_output_is_rejected_even_with_matching_input_hash(
    tmp_path: Path,
) -> None:
    h = Harness(tmp_path)
    h.scan()
    initialize(h.config, T0)
    with sqlite3.connect(h.watchlist.state_dir / "scanner.sqlite3") as db:
        for identifier, payload in db.execute(
            "SELECT id,record_json FROM signals"
        ).fetchall():
            record = json.loads(payload)
            if record["strategy"]["id"] == "momentum":
                record["intent"] = "exit_long"
                db.execute(
                    "UPDATE signals SET record_json=? WHERE id=?",
                    (json.dumps(record), identifier),
                )
    h.step()
    assert h.rows("orders") == []
    assert h.rows("decisions")[0]["reason"] == "signal_output_mismatch"


def test_shared_account_accepts_two_assets_without_duplicate_funds(
    tmp_path: Path,
) -> None:
    h = Harness(tmp_path, 2)
    h.scan()
    initialize(h.config, T0)
    h.step()
    assert len(h.rows("orders")) == 2
    assert D(h.rows("accounts")[0]["reserved"]) == 103
    for day in (1, 2):
        h.scan(day)
        h.step(day)
    assert len(h.rows("fills")) == 2
    assert D(h.rows("accounts")[0]["cash"]) == 897
    assert sum(D(p["quantity"]) for p in h.rows("positions")) == 10
    assert inspect(h.config.state_dir)["reconciliation_errors"] == {}


@pytest.mark.parametrize(
    "quantity_step,quantity,final_cash",
    [("1", "4", "1019"), ("0.1", "4.9", "1023.275")],
)
def test_spread_slippage_and_fractional_fill_accounting(
    tmp_path: Path, quantity_step: str, quantity: str, final_cash: str
) -> None:
    costs = Costs(D(0), D(0), D(100), D(50), D(quantity_step), D(0), D(0))
    h = Harness(tmp_path, costs=costs)
    h.scan()
    initialize(h.config, T0)
    h.step()
    for day in (1, 2, 3, 4):
        h.scan(day)
        h.step(day)
    buy, sell = [json.loads(row["payload"]) for row in h.rows("fills")]
    assert D(buy["fill_price_quote"]) == D("10.1")
    assert D(buy["quantity"]) == D(quantity)
    assert D(sell["fill_price_quote"]) == D("14.85")
    assert D(h.rows("accounts")[0]["cash"]) == D(final_cash)
    assert D(h.rows("accounts")[0]["reserved"]) == 0
    assert inspect(h.config.state_dir)["reconciliation_errors"] == {}


def test_paper_archive_links_immutable_prices_and_survives_source_unlink(
    tmp_path: Path,
) -> None:
    import os

    h = Harness(tmp_path)
    h.scan()
    initialize(h.config, T0)
    h.step()
    originals = {
        path.name: path
        for path in (h.watchlist.state_dir / "snapshots").glob("*.parquet")
    }
    archives = list((h.config.state_dir / "inputs").glob("*.parquet"))
    assert archives
    target = archives[0]
    original = originals[target.name]
    assert os.path.samefile(original, target)
    # Remove one directory entry only; a hard link keeps the archive alive.
    expected = target.read_bytes()
    original.unlink()
    assert target.read_bytes() == expected


def test_paper_archive_verified_copy_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import os

    h = Harness(tmp_path)
    h.scan()
    initialize(h.config, T0)
    link = os.link

    def unsupported(source: str | Path, destination: str | Path) -> None:
        if Path(source).parent == h.watchlist.state_dir / "snapshots":
            raise OSError("cross-device link")
        link(source, destination)

    monkeypatch.setattr(os, "link", unsupported)
    assert h.step()["status"] == "success"
    originals = {
        path.name: path
        for path in (h.watchlist.state_dir / "snapshots").glob("*.parquet")
    }
    archives = list((h.config.state_dir / "inputs").glob("*.parquet"))
    assert archives
    assert all(not os.path.samefile(originals[path.name], path) for path in archives)
    assert all(
        originals[path.name].read_bytes() == path.read_bytes() for path in archives
    )


def test_existing_corrupt_archive_is_not_overwritten(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.scan()
    initialize(h.config, T0)
    h.step()
    target = next((h.config.state_dir / "inputs").glob("*.parquet"))
    target.unlink()  # Separate a corrupt archive from the still-valid source inode.
    target.write_bytes(b"invalid archive")
    result = h.step(2)
    assert result["status"] == "degraded"
    assert target.read_bytes() == b"invalid archive"
    assert not h.rows("fills")
    assert D(h.rows("accounts")[0]["cash"]) == 1000


def test_paper_reads_legacy_scanner_schema_without_migrating_it(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.scan()
    path = h.watchlist.state_dir / "scanner.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute("DROP TABLE failure_streaks")
        db.execute("PRAGMA user_version=1")
    before = path.read_bytes()
    initialize(h.config, T0)
    assert h.step()["status"] == "success"
    assert path.read_bytes() == before
