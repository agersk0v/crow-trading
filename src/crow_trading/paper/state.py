"""Local SQLite migrations, atomic accounting, and append-only business events."""

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from crow_trading.backtesting.models import ZERO, number
from crow_trading.data.models import DataError, utc
from crow_trading.paper.config import Account, Config
from crow_trading.scanner.config import digest
from crow_trading.scanner.state import Busy

APPLICATION_ID = 1129476176
SCHEMA = """
CREATE TABLE accounts (
 id TEXT PRIMARY KEY, policy_hash TEXT NOT NULL, policy_json TEXT NOT NULL,
 created_at TEXT NOT NULL, last_clock TEXT NOT NULL, cash TEXT NOT NULL,
 reserved TEXT NOT NULL, peak TEXT NOT NULL, stopped INTEGER NOT NULL,
 risk_halted INTEGER NOT NULL, risk_reason TEXT NOT NULL
);
CREATE TABLE positions (
 account_id TEXT NOT NULL, instrument_key TEXT NOT NULL, quantity TEXT NOT NULL,
 basis TEXT NOT NULL, PRIMARY KEY(account_id,instrument_key)
);
CREATE TABLE orders (
 id TEXT PRIMARY KEY, account_id TEXT NOT NULL, instrument_key TEXT NOT NULL,
 side TEXT NOT NULL, created_at TEXT NOT NULL, signal_id TEXT, reason TEXT NOT NULL,
 status TEXT NOT NULL, reserved TEXT NOT NULL, terminal_at TEXT,
 policy_json TEXT NOT NULL
);
CREATE UNIQUE INDEX pending_asset ON orders(account_id,instrument_key)
 WHERE status='pending';
CREATE TABLE fills (
 order_id TEXT PRIMARY KEY, payload TEXT NOT NULL
);
CREATE TABLE valuations (account_id TEXT PRIMARY KEY, payload TEXT NOT NULL);
CREATE TABLE decisions (
 account_id TEXT NOT NULL, signal_id TEXT NOT NULL, status TEXT NOT NULL,
 reason TEXT NOT NULL, payload TEXT NOT NULL, PRIMARY KEY(account_id,signal_id)
);
CREATE TABLE events (
 id TEXT PRIMARY KEY, account_id TEXT NOT NULL, recorded_at TEXT NOT NULL,
 kind TEXT NOT NULL, cash_delta TEXT NOT NULL, payload TEXT NOT NULL
);
"""


@dataclass(frozen=True)
class Balance:
    cash: Decimal
    reserved: Decimal
    peak: Decimal
    stopped: bool
    risk_halted: bool
    risk_reason: str


class State:
    def __init__(self, root: Path, *, create: bool = False) -> None:
        self.root = root
        self.create = create
        self.db: sqlite3.Connection | None = None

    def __enter__(self) -> "State":
        path = self.root / "paper.sqlite3"
        if not self.create and not path.exists():
            raise DataError("paper database not initialized; run paper init first")
        self.root.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(path, timeout=0, isolation_level=None)
        db.row_factory = sqlite3.Row
        self.db = db
        try:
            db.execute("BEGIN IMMEDIATE")
            version = db.execute("PRAGMA user_version").fetchone()[0]
            application = db.execute("PRAGMA application_id").fetchone()[0]
            tables = db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
            if version == 0 and application == 0 and not tables:
                if not self.create:
                    raise DataError("paper schema is uninitialized")
                for statement in SCHEMA.split(";"):
                    if statement.strip():
                        db.execute(statement)
                db.execute(f"PRAGMA application_id={APPLICATION_ID}")
                db.execute("PRAGMA user_version=1")
            elif version != 1 or application != APPLICATION_ID:
                raise DataError(
                    "unsupported or unrelated paper database; migrations not applied"
                )
        except Exception as exc:
            db.close()
            self.db = None
            if isinstance(exc, sqlite3.OperationalError) and "locked" in str(exc):
                raise Busy("another paper worker owns this database") from exc
            raise
        return self

    @property
    def connection(self) -> sqlite3.Connection:
        assert self.db is not None
        return self.db

    def __exit__(self, *args: object) -> None:
        if self.db is not None:
            self.db.rollback()
            self.db.close()

    def rows(self, query: str, args: tuple[object, ...] = ()) -> list[dict[str, Any]]:
        return [dict(row) for row in self.connection.execute(query, args).fetchall()]

    def account(self, name: str) -> dict[str, Any]:
        rows = self.rows("SELECT * FROM accounts WHERE id=?", (name,))
        if not rows:
            raise DataError(f"unknown paper account: {name}")
        return rows[0]

    def balance(self, name: str) -> Balance:
        row = self.account(name)
        return Balance(
            number(row["cash"]),
            number(row["reserved"]),
            number(row["peak"]),
            bool(row["stopped"]),
            bool(row["risk_halted"]),
            row["risk_reason"],
        )

    def event(
        self,
        key: object,
        name: str,
        now: datetime,
        kind: str,
        payload: dict[str, Any],
        cash_delta: Decimal = ZERO,
    ) -> None:
        self.connection.execute(
            "INSERT OR IGNORE INTO events VALUES (?,?,?,?,?,?)",
            (
                digest(key),
                name,
                now.isoformat(),
                kind,
                str(cash_delta),
                json.dumps(payload, sort_keys=True),
            ),
        )

    def reserve(self, name: str) -> None:
        reserved = sum(
            (
                number(r["reserved"])
                for r in self.rows(
                    "SELECT reserved FROM orders WHERE account_id=? "
                    "AND status='pending'",
                    (name,),
                )
            ),
            ZERO,
        )
        self.connection.execute(
            "UPDATE accounts SET reserved=? WHERE id=?", (str(reserved), name)
        )

    def terminal(
        self, order: dict[str, Any], status: str, reason: str, now: datetime
    ) -> None:
        if order["status"] != "pending" or status not in ("cancelled", "rejected"):
            raise DataError("unsupported order transition")
        self.connection.execute(
            "UPDATE orders SET status=?,reason=?,terminal_at=? "
            "WHERE id=? AND status='pending'",
            (status, reason, now.isoformat(), order["id"]),
        )
        self.reserve(order["account_id"])
        self.event(
            [order["id"], status],
            order["account_id"],
            now,
            status,
            {"order_id": order["id"], "reason": reason},
        )

    def reconcile(self, name: str) -> None:
        balance = self.balance(name)
        events = self.rows(
            "SELECT * FROM events WHERE account_id=? ORDER BY rowid", (name,)
        )
        cash = sum((number(row["cash_delta"]) for row in events), ZERO)
        if abs(balance.cash - cash) > Decimal("1e-18") or balance.cash < 0:
            raise DataError("cash ledger reconciliation failed; no orders processed")
        for order in self.rows("SELECT * FROM orders WHERE account_id=?", (name,)):
            if (
                order["status"] not in ("pending", "filled", "rejected", "cancelled")
                or order["side"] not in ("buy", "sell")
                or number(order["reserved"]) < 0
            ):
                raise DataError(
                    "unknown order state; reconciliation must resolve it first"
                )
            created = utc(datetime.fromisoformat(order["created_at"]))
            if (
                order["terminal_at"]
                and utc(datetime.fromisoformat(order["terminal_at"])) < created
            ):
                raise DataError("order terminal time precedes submission")
        for decision in self.rows(
            "SELECT * FROM decisions WHERE account_id=? AND status='accepted'", (name,)
        ):
            payload = json.loads(decision["payload"])
            if not self.rows(
                "SELECT 1 FROM orders WHERE id=? AND account_id=?",
                (payload["order_id"], name),
            ):
                raise DataError(
                    "accepted decision has no order; refusing ambiguous replay"
                )
        pending = self.rows(
            "SELECT * FROM orders WHERE account_id=? AND status='pending'", (name,)
        )
        reserved = sum((number(row["reserved"]) for row in pending), ZERO)
        if reserved != balance.reserved or not ZERO <= reserved <= balance.cash:
            raise DataError("reservation reconciliation failed")
        reconstructed: dict[str, tuple[Decimal, Decimal]] = {}
        for row in self.rows("SELECT payload FROM fills ORDER BY rowid"):
            fill = json.loads(row["payload"])
            if fill["account_id"] != name:
                continue
            matching_orders = self.rows(
                "SELECT * FROM orders WHERE id=?", (fill["order_id"],)
            )
            if not matching_orders or matching_orders[0]["status"] != "filled":
                raise DataError("fill/order reconciliation failed")
            quantity, basis = reconstructed.get(fill["instrument_key"], (ZERO, ZERO))
            reconstructed[fill["instrument_key"]] = (
                quantity + number(fill["quantity_delta"]),
                basis + number(fill["basis_delta"]),
            )
        for row in self.rows("SELECT * FROM positions WHERE account_id=?", (name,)):
            expected = reconstructed.pop(row["instrument_key"], (ZERO, ZERO))
            if (
                abs(number(row["quantity"]) - expected[0]) > Decimal("1e-18")
                or abs(number(row["basis"]) - expected[1]) > Decimal("1e-18")
                or number(row["quantity"]) < 0
                or number(row["basis"]) < 0
            ):
                raise DataError("position ledger reconciliation failed")
        if any(q != 0 or b != 0 for q, b in reconstructed.values()):
            raise DataError("missing position state for committed fills")
        for row in self.rows(
            "SELECT id FROM orders WHERE account_id=? AND status='filled'", (name,)
        ):
            if not self.rows("SELECT 1 FROM fills WHERE order_id=?", (row["id"],)):
                raise DataError("filled order has no fill; refusing ambiguous replay")

    def ensure(
        self, config: Config, account: Account, now: datetime, *, initialize: bool
    ) -> None:
        policy = account.policy(config.mode)
        existing = self.rows("SELECT * FROM accounts WHERE id=?", (account.name,))
        if not existing:
            if not initialize:
                raise DataError("configured account is not initialized")
            self.connection.execute(
                "INSERT INTO accounts VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    account.name,
                    digest(policy),
                    json.dumps(policy, sort_keys=True),
                    now.isoformat(),
                    now.isoformat(),
                    str(account.settings.initial_cash),
                    "0",
                    str(account.settings.initial_cash),
                    0,
                    0,
                    "",
                ),
            )
            self.event(
                [account.name, "initial"],
                account.name,
                now,
                "initial_cash",
                {"mode": config.mode},
                account.settings.initial_cash,
            )
        elif existing[0]["policy_hash"] != digest(policy):
            raise DataError(
                "account policy changed; create a new account to preserve history"
            )
        if now < datetime.fromisoformat(self.account(account.name)["last_clock"]):
            raise DataError("paper clock moved backwards")
        self.reconcile(account.name)

    def commit(self) -> None:
        self.connection.commit()
