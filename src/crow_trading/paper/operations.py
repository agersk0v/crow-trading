"""Local inspection, explicit stop/resume/cancel, and immutable report exports."""

import csv
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from crow_trading.data.models import DataError, utc
from crow_trading.paper.state import State

TABLES = (
    "accounts",
    "positions",
    "orders",
    "fills",
    "decisions",
    "events",
    "valuations",
)


def inspect(root: Path) -> dict[str, Any]:
    with State(root) as state:
        result = {
            table: state.rows(f"SELECT * FROM {table} ORDER BY rowid")
            for table in TABLES
        }
        problems: dict[str, str] = {}
        for account in result["accounts"]:
            try:
                state.reconcile(account["id"])
            except DataError as exc:
                problems[account["id"]] = str(exc)
        return {"reconciliation_errors": problems, **result}


def control(
    root: Path,
    command: str,
    account_id: str | None = None,
    order_id: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    now = utc(now or datetime.now(UTC))
    with State(root) as state:
        if command == "cancel":
            rows = state.rows("SELECT * FROM orders WHERE id=?", (order_id,))
            if not rows:
                raise DataError("unknown order")
            order = rows[0]
            now = max(
                now,
                datetime.fromisoformat(
                    state.account(order["account_id"])["last_clock"]
                ),
            )
            if order["status"] == "pending":
                state.terminal(order, "cancelled", "manual_cancel", now)
            else:
                raise DataError("only pending paper orders can be cancelled")
        elif command in ("stop", "resume"):
            if account_id is None:
                raise DataError("account ID is required")
            last = datetime.fromisoformat(state.account(account_id)["last_clock"])
            if command == "resume" and now < last:
                raise DataError("resume clock precedes financial state")
            now = max(now, last)
            if command == "resume":
                state.reconcile(account_id)
            state.connection.execute(
                "UPDATE accounts SET stopped=? WHERE id=?",
                (int(command == "stop"), account_id),
            )
            if command == "stop":
                for order in state.rows(
                    "SELECT * FROM orders WHERE account_id=? "
                    "AND status='pending' AND side='buy'",
                    (account_id,),
                ):
                    state.terminal(order, "cancelled", "manual_stop", now)
            state.event(
                [account_id, command, now.isoformat()],
                account_id,
                now,
                command,
                {"risk_halt_cleared": False},
            )
        else:
            raise DataError("unknown control command")
        state.commit()
    return {"status": command, "account": account_id, "order": order_id}


def export(root: Path, destination: Path) -> dict[str, Any]:
    content = inspect(root)
    destination.mkdir(parents=True, exist_ok=False)
    (destination / "paper-report.json").write_text(
        json.dumps(content, sort_keys=True, indent=2) + "\n", encoding="utf-8"
    )
    for table in TABLES:
        rows = content[table]
        if table == "fills":
            rows = [json.loads(row["payload"]) for row in rows]
        elif table == "valuations":
            rows = [
                {"account_id": row["account_id"], **json.loads(row["payload"])}
                for row in rows
            ]
        fields = list(rows[0]) if rows else ["id"]
        # Values containing structured data are JSON strings inside CSV cells.
        with (destination / (table + ".csv")).open(
            "x", encoding="utf-8", newline=""
        ) as stream:
            writer = csv.DictWriter(stream, fieldnames=fields)
            writer.writeheader()
            writer.writerows(
                {
                    key: json.dumps(value, sort_keys=True)
                    if isinstance(value, (dict, list))
                    else value
                    for key, value in row.items()
                }
                for row in rows
            )
    return {
        "status": "exported",
        "directory": str(destination),
        "reconciliation_errors": content["reconciliation_errors"],
    }
