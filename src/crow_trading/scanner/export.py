"""Explicit report exports; SQLite remains the authority."""

import json
from pathlib import Path
from typing import Any

from crow_trading.data.models import DataError
from crow_trading.scanner.state import State


def export_reports(root: Path, destination: Path) -> dict[str, Any]:
    if not (root / "scanner.sqlite3").is_file():
        raise DataError("scanner database does not exist")
    with State(root) as state:
        destination.mkdir(parents=True, exist_ok=False)
        files = state.export(destination)
        summary = destination / "failure-streaks.json"
        summary.write_text(
            json.dumps(
                {"schema_version": 1, "failure_streaks": state.failure_summaries()},
                sort_keys=True,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        state.commit()  # Persist a schema upgrade if exporting a version-1 database.
    return {"status": "exported", "report_files": [*files, str(summary)]}
