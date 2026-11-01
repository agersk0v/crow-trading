"""Import normalized daily CSV archives without changing their venue."""

import csv
import hashlib
import io
from datetime import UTC, datetime
from pathlib import Path

from crow_trading.data.models import Candle, DataError, Dataset, Metadata, Request, utc
from crow_trading.data.normalize import schedule, validate


def import_archive(
    path: Path,
    request: Request,
    *,
    venue: str,
    provenance: str,
    as_of: datetime | None = None,
) -> Dataset:
    """CSV timestamps must already be explicit UTC interval opens."""
    if not venue or not provenance:
        raise DataError("archive venue and provenance are required")
    expected, timezone = schedule(request)
    candles: list[Candle] = []
    contents = path.read_bytes()
    try:
        reader = csv.DictReader(io.StringIO(contents.decode("utf-8"), newline=""))
        for row in reader:
            stamp = utc(datetime.fromisoformat(row["timestamp"]))
            if stamp not in expected:
                raise DataError(
                    "archive contains a timestamp outside expected sessions"
                )
            candles.append(
                Candle(
                    stamp,
                    expected[stamp],
                    *(
                        float(row[key])
                        for key in ("open", "high", "low", "close", "volume")
                    ),
                )
            )
    except (KeyError, TypeError, ValueError, UnicodeError, csv.Error) as exc:
        raise DataError(f"invalid archive CSV: {exc}") from exc
    now = as_of or datetime.now(UTC)
    digest = hashlib.sha256(contents).hexdigest()
    dataset = Dataset(
        tuple(candles),
        Metadata(
            request,
            "csv-archive",
            venue,
            timezone,
            now,
            f"{provenance}; file_sha256={digest}",
            "normalized-csv-v1",
        ),
    )
    validate(dataset, as_of=now)
    return dataset
