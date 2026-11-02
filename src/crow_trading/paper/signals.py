"""Validate scanner provenance and atomically acknowledge strategy decisions."""

import json
from datetime import datetime, timedelta
from typing import Any

from crow_trading.data.models import DataError, utc
from crow_trading.paper.config import Account, Asset
from crow_trading.paper.orders import create_order
from crow_trading.paper.portfolio import Valuation, assets, pending, position
from crow_trading.paper.risk import entry_reservation
from crow_trading.paper.source import Source
from crow_trading.paper.state import State
from crow_trading.scanner.config import digest
from crow_trading.scanner.datasets import context_hash


def eligibility(
    record: dict[str, Any],
    account: Account,
    asset: Asset,
    source: Source,
    now: datetime,
    mode: str,
) -> str | None:
    if record["classification"] != "fresh":
        return "non_fresh_signal"
    if (record["data_mode"] == "snapshot") != (mode == "fixture"):
        return "fixture_mode_mismatch"
    if (
        record["identity"] != asset.instrument.identity()
        or record["strategy"] != account.strategy
    ):
        return "signal_identity_mismatch"
    opened = utc(datetime.fromisoformat(record["candle_timestamp"]))
    available = utc(datetime.fromisoformat(record["available_at"]))
    evaluated = utc(datetime.fromisoformat(record["evaluated_as_of"]))
    if not opened < available <= evaluated <= now:
        return "invalid_signal_timing"
    expires = min(
        utc(datetime.fromisoformat(record["expires_at"])),
        available + timedelta(seconds=account.signal_max_age_seconds),
    )
    if now >= expires:
        return "expired_signal"
    logical = (
        record["instrument_key"],
        record["candle_timestamp"],
        record["strategy_key"],
    )
    if source.latest_signals[logical] != record["id"]:
        return "superseded_signal"
    data = source.data(
        asset, path=record["snapshot_path"], expected_fp=record["dataset_fingerprint"]
    )
    index = next(
        (i for i, candle in enumerate(data.candles) if candle.timestamp == opened), None
    )
    if index is None or data.candles[index].closed_at != available:
        return "signal_candle_mismatch"
    selected = next(
        s
        for s in asset.instrument.strategies
        if digest(
            {"id": s.strategy_id, "version": s.version, "parameters": s.parameters()}
        )
        == record["strategy_key"]
    )
    history = data.candles[max(0, index + 1 - selected.history_required) : index + 1]
    if context_hash(asset.instrument, history) != record["signal_input_fingerprint"]:
        return "signal_input_mismatch"
    expected = selected.evaluate(asset.instrument.symbol, history, as_of=available)
    if (
        expected is None
        or expected.intent != record["intent"]
        or expected.reason != record["reason"]
    ):
        return "signal_output_mismatch"
    if (
        digest(
            [
                asset.instrument.key,
                opened.isoformat(),
                record["strategy_key"],
                record["signal_input_fingerprint"],
            ]
        )
        != record["id"]
    ):
        return "signal_id_mismatch"
    return None


def process_signals(
    state: State,
    account: Account,
    source: Source,
    now: datetime,
    mode: str,
    valuation: Valuation | None,
) -> list[str]:
    configured = assets(account)
    errors: list[str] = []
    for record in source.signals:
        if record["instrument_key"] not in configured or record[
            "strategy_key"
        ] != digest(account.strategy):
            continue
        known = state.rows(
            "SELECT status FROM decisions WHERE account_id=? AND signal_id=?",
            (account.name, record["id"]),
        )
        if known and known[0]["status"] != "deferred":
            continue
        asset = configured[record["instrument_key"]]
        reason: str | None = None
        order_id: str | None = None
        temporary = False
        try:
            reason = eligibility(record, account, asset, source, now, mode)
            if reason is None and record["intent"] == "hold":
                reason = "hold:" + record["reason"]
            existing = next(
                (
                    o
                    for o in pending(state, account.name)
                    if o["instrument_key"] == asset.instrument.key
                ),
                None,
            )
            quantity, _ = position(state, account.name, asset.instrument.key)
            if reason is None and record["intent"] == "enter_long":
                if existing:
                    reason = "pending_order"
                elif quantity:
                    reason = "already_long"
                elif valuation is None:
                    reason = "valuation_unavailable"
                    temporary = True
                else:
                    reserve, reason = entry_reservation(
                        state, account, asset, source, now, valuation
                    )
                    if reason is None:
                        order_id = create_order(
                            state,
                            account,
                            asset,
                            now,
                            "buy",
                            "strategy_entry",
                            record["id"],
                            record["id"],
                            reserve,
                        )
            elif reason is None and record["intent"] == "exit_long":
                if existing and existing["side"] == "buy":
                    state.terminal(
                        existing, "cancelled", "strategy_exit_before_fill", now
                    )
                    reason = "pending_entry_cancelled"
                elif existing:
                    reason = "pending_order"
                elif not quantity:
                    reason = "not_long"
                else:
                    order_id = create_order(
                        state,
                        account,
                        asset,
                        now,
                        "sell",
                        "strategy_exit",
                        record["id"],
                        record["id"],
                    )
            elif reason is None:
                reason = "unsupported_intent"
        except DataError as exc:
            reason = "input_unavailable:" + str(exc)
            temporary = True
            errors.append(str(exc))
        decision_payload: dict[str, Any] = {
            "signal": record,
            "received_at": now.isoformat(),
            "order_id": order_id,
            **source.decision_evidence(asset),
        }
        state.connection.execute(
            "INSERT OR REPLACE INTO decisions VALUES (?,?,?,?,?)",
            (
                account.name,
                record["id"],
                "accepted" if order_id else ("deferred" if temporary else "ignored"),
                reason or "order_created",
                json.dumps(decision_payload, sort_keys=True),
            ),
        )
        state.event(
            [
                account.name,
                "decision",
                record["id"],
                reason,
                source.fx_fingerprint,
            ],
            account.name,
            now,
            "decision",
            {
                "signal_id": record["id"],
                "reason": reason or "order_created",
                "order_id": order_id,
                **source.decision_evidence(asset),
            },
        )
    return errors
