"""SQLite is the authority for both progress and committed report content."""

import json
import os
import sqlite3
import tempfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from crow_trading.data.models import DataError

SCHEMA = """
CREATE TABLE IF NOT EXISTS instruments (
 identity TEXT PRIMARY KEY, policy_hash TEXT NOT NULL, last_timestamp TEXT NOT NULL,
 snapshot_path TEXT NOT NULL, snapshot_fingerprint TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS signals (
 id TEXT PRIMARY KEY, identity TEXT NOT NULL, candle_timestamp TEXT NOT NULL,
 strategy_key TEXT NOT NULL, context_hash TEXT NOT NULL, record_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS signal_candles ON signals(identity, candle_timestamp);
CREATE TABLE IF NOT EXISTS runs (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 report_json TEXT NOT NULL, report_md TEXT NOT NULL
);
"""


class Busy(DataError):
    """A different scanner owns this state database; retry on the next tick."""


class State:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.db: sqlite3.Connection | None = None

    def __enter__(self) -> "State":
        self.root.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(
            self.root / "scanner.sqlite3", timeout=0, isolation_level=None
        )
        connection.row_factory = sqlite3.Row
        self.db = connection
        try:
            connection.execute("BEGIN IMMEDIATE")
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            application = connection.execute("PRAGMA application_id").fetchone()[0]
            tables = connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
            if version not in (0, 1):
                raise DataError("unsupported scanner state version")
            if application not in (0, 1129477971) or (application == 0 and tables):
                raise DataError("refusing to modify an unrelated SQLite database")
            for statement in SCHEMA.split(";"):
                if statement.strip():
                    connection.execute(statement)
            connection.execute("PRAGMA application_id=1129477971")
            connection.execute("PRAGMA user_version=1")
        except Exception as exc:
            connection.close()
            self.db = None
            if isinstance(exc, sqlite3.OperationalError) and (
                "locked" in str(exc) or "busy" in str(exc)
            ):
                raise Busy(
                    "another scanner is active for this state directory"
                ) from exc
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

    @contextmanager
    def asset(self) -> Iterator[None]:
        self.connection.execute("SAVEPOINT asset")
        try:
            yield
        except Exception:
            self.connection.execute("ROLLBACK TO asset")
            self.connection.execute("RELEASE asset")
            raise
        else:
            self.connection.execute("RELEASE asset")

    def previous(self, identity: str) -> dict[str, str] | None:
        row = self.connection.execute(
            "SELECT * FROM instruments WHERE identity=?", (identity,)
        ).fetchone()
        return dict(row) if row else None

    def known_candles(self, identity: str) -> set[str]:
        return {
            row[0]
            for row in self.connection.execute(
                "SELECT DISTINCT candle_timestamp FROM signals WHERE identity=?",
                (identity,),
            )
        }

    def add_signal(self, record: dict[str, Any]) -> bool:
        cursor = self.connection.execute(
            "INSERT OR IGNORE INTO signals VALUES (?,?,?,?,?,?)",
            (
                record["id"],
                record["instrument_key"],
                record["candle_timestamp"],
                record["strategy_key"],
                record["signal_input_fingerprint"],
                json.dumps(record, sort_keys=True),
            ),
        )
        return cursor.rowcount == 1

    def progress(
        self, identity: str, policy_hash: str, last: str, path: str, fingerprint: str
    ) -> None:
        self.connection.execute(
            "INSERT OR REPLACE INTO instruments VALUES (?,?,?,?,?)",
            (identity, policy_hash, last, path, fingerprint),
        )

    def report(
        self, content: dict[str, Any], render: Callable[[dict[str, Any]], str]
    ) -> dict[str, Any]:
        cursor = self.connection.execute(
            "INSERT INTO runs(report_json,report_md) VALUES ('','')"
        )
        content = {**content, "run_id": cursor.lastrowid}
        self.connection.execute(
            "UPDATE runs SET report_json=?, report_md=? WHERE id=?",
            (
                json.dumps(content, sort_keys=True, indent=2) + "\n",
                render(content),
                cursor.lastrowid,
            ),
        )
        return content

    def commit(self) -> None:
        self.connection.commit()

    def export(self) -> list[str]:
        """Repair missing projections without overwriting conflicting user files."""
        repaired: list[str] = []
        for row in self.connection.execute("SELECT * FROM runs ORDER BY id").fetchall():
            for suffix, column in (("json", "report_json"), ("md", "report_md")):
                path = self.root / "reports" / f"{row['id']:06d}.{suffix}"
                content = row[column].encode("utf-8")
                if path.exists():
                    if path.read_bytes() != content:
                        raise DataError(
                            f"report projection conflict; preserve/move file: {path}"
                        )
                    continue
                path.parent.mkdir(parents=True, exist_ok=True)
                descriptor, temporary = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
                try:
                    with os.fdopen(descriptor, "wb") as stream:
                        stream.write(content)
                        stream.flush()
                        os.fsync(stream.fileno())
                    try:
                        os.link(temporary, path)
                    except FileExistsError:
                        if path.read_bytes() != content:
                            raise DataError(
                                f"concurrent report projection conflict: {path}"
                            ) from None
                    repaired.append(str(path))
                finally:
                    Path(temporary).unlink(missing_ok=True)
        return repaired
