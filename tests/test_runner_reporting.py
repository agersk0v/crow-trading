"""Focused reports retain reconciliation, incremental activity, and exact money."""

import json
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from crow_trading.data.models import DataError
from crow_trading.paper.engine import initialize
from crow_trading.paper.operations import inspect
from crow_trading.paper.state import State
from crow_trading.runner.engine import run
from crow_trading.runner.reporting import Snapshot, snapshot
from crow_trading.scanner.state import Busy
from tests.helpers.paper import T0, D, Harness


def test_startup_skips_history_and_incremental_fill_of_existing_order(
    tmp_path: Path,
) -> None:
    h = Harness(tmp_path)
    initialize(h.config, T0)
    h.scan()
    h.step()
    start = snapshot(h.config)
    assert not start.orders and not start.fills
    assert start.accounts[0]["pending_orders"] == 1
    h.scan(2)
    h.step(2)
    updated = snapshot(h.config, start.cursor)
    assert len(updated.fills) == 1
    fill = json.loads(updated.fills[0]["payload"])
    assert fill["side"] == "buy"
    assert fill["order_id"] == h.rows("orders")[0]["id"]
    assert len(updated.orders) == 1  # New exit, not the old entry order.
    assert updated.orders[0]["side"] == "sell"
    assert updated.accounts[0]["pending_orders"] == 1
    assert D(updated.accounts[0]["cash"]) == D("948.5")
    valuation = json.loads(updated.valuations[0]["payload"])
    assert D(valuation["equity"]) == D("998.5")
    assert updated.valuations == inspect(h.config.state_dir)["valuations"]
    unchanged = snapshot(h.config, updated.cursor)
    assert unchanged.orders == unchanged.fills == []
    restarted = snapshot(h.config)
    assert restarted.orders == restarted.fills == []
    assert restarted.cursor == updated.cursor


def test_summary_does_not_export_audit_tables_and_keeps_reconciliation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = Harness(tmp_path)
    initialize(h.config, T0)
    h.scan()
    h.step()
    queries: list[str] = []
    reconciled: list[str] = []
    original_rows = State.rows
    original_reconcile = State.reconcile

    def rows(
        self: State, query: str, args: tuple[object, ...] = ()
    ) -> list[dict[str, Any]]:
        queries.append(query)
        return original_rows(self, query, args)

    def reconcile(self: State, name: str) -> None:
        reconciled.append(name)
        original_reconcile(self, name)

    monkeypatch.setattr(State, "rows", rows)
    monkeypatch.setattr(State, "reconcile", reconcile)
    start = snapshot(h.config)
    assert reconciled == [h.account.name]
    for table in ("accounts", "positions", "orders", "fills", "decisions", "events"):
        assert f"SELECT * FROM {table} ORDER BY rowid" not in queries
    assert not any("SELECT f.payload" in query for query in queries)
    queries.clear()
    snapshot(h.config, start.cursor)
    assert any("f.rowid>? AND f.rowid<=?" in query for query in queries)
    assert any("WHERE rowid>? AND rowid<=?" in query for query in queries)


def test_unconfigured_accounts_filtered_but_still_reconciled(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    initialize(h.config, T0)
    other = replace(h.config, accounts=(replace(h.account, name="other"),))
    initialize(other, T0)
    report = snapshot(h.config)
    assert [r["id"] for r in report.accounts] == [h.account.name]
    with State(h.config.state_dir) as state:
        state.connection.execute("UPDATE accounts SET cash='9' WHERE id='other'")
        state.commit()
    with pytest.raises(DataError, match="reconciliation"):
        snapshot(h.config)


def test_failed_summary_does_not_drop_session_activity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = Harness(tmp_path)
    instant = T0
    calls = 0
    waits = 0
    lines: list[str] = []

    def busy_once(*args: Any, **kwargs: Any) -> Snapshot:
        nonlocal calls
        calls += 1
        if calls == 2:  # Startup succeeds, first cycle report is temporarily busy.
            raise Busy("inspection lock")
        return snapshot(*args, **kwargs)

    def wait(seconds: float) -> None:
        nonlocal waits, instant
        waits += 1
        if waits == 2:
            raise KeyboardInterrupt
        instant += timedelta(seconds=seconds)

    monkeypatch.setattr("crow_trading.runner.engine.snapshot", busy_once)
    with pytest.raises(KeyboardInterrupt):
        run(
            h.config, now=lambda: instant, wait=wait, emit=lines.append, fetcher=h.fetch
        )
    assert sum(" ORDER " in line for line in lines) == 1
    assert any("summary unavailable" in line for line in lines)
    assert len(inspect(h.config.state_dir)["orders"]) == 1
