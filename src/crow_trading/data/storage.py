"""Single-file Parquet datasets with verified, embedded provenance."""

import hashlib
import json
import os
import tempfile
from dataclasses import asdict
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from crow_trading.data.models import Candle, DataError, Dataset, Metadata, Request
from crow_trading.data.normalize import validate

KEY = b"crow_trading.dataset.v1"
SCHEMA = pa.schema(
    [
        pa.field("timestamp", pa.timestamp("us", tz="UTC"), nullable=False),
        pa.field("closed_at", pa.timestamp("us", tz="UTC"), nullable=False),
        *(
            pa.field(name, pa.float64(), nullable=False)
            for name in ("open", "high", "low", "close", "volume")
        ),
    ]
)


def metadata_dict(dataset: Dataset) -> dict[str, Any]:
    meta = asdict(dataset.metadata)
    meta["request"]["start"] = str(dataset.metadata.request.start)
    meta["request"]["end"] = str(dataset.metadata.request.end)
    meta["retrieved_at"] = dataset.metadata.retrieved_at.isoformat()
    meta["actual_start"] = dataset.candles[0].timestamp.isoformat()
    meta["actual_end"] = dataset.candles[-1].closed_at.isoformat()
    meta["row_count"] = len(dataset.candles)
    return meta


def fingerprint(dataset: Dataset) -> str:
    """Content identity excludes retrieval time, so repeat fetches are idempotent."""
    meta = metadata_dict(dataset)
    del meta["retrieved_at"]
    rows = [
        [
            c.timestamp.isoformat(),
            c.closed_at.isoformat(),
            *[float(value) for value in (c.open, c.high, c.low, c.close, c.volume)],
        ]
        for c in dataset.candles
    ]
    payload = json.dumps(
        [meta, rows], sort_keys=True, separators=(",", ":"), allow_nan=False
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def save(dataset: Dataset, path: Path) -> str:
    validate(dataset)
    identity = fingerprint(dataset)
    if path.exists():
        if fingerprint(load(path)) != identity:
            raise DataError(f"refusing to replace a different dataset: {path}")
        return identity
    meta = metadata_dict(dataset)
    meta["fingerprint"] = identity
    table = pa.Table.from_pylist([asdict(c) for c in dataset.candles], schema=SCHEMA)
    table = table.replace_schema_metadata({KEY: json.dumps(meta).encode()})
    path.parent.mkdir(parents=True, exist_ok=True)
    # Write completely before making the immutable destination visible.
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, suffix=".parquet.tmp")
    os.close(descriptor)
    try:
        pq.write_table(table, temporary)
        try:
            os.link(temporary, path)
        except FileExistsError:
            if fingerprint(load(path)) != identity:
                raise DataError(
                    f"concurrent writer created a different dataset: {path}"
                ) from None
    finally:
        Path(temporary).unlink(missing_ok=True)
    return identity


def load(path: Path) -> Dataset:
    """Read and validate offline; reject schema, metadata, or data corruption."""
    try:
        table = pq.ParquetFile(path).read()
        if not table.schema.remove_metadata().equals(SCHEMA):
            raise DataError("unsupported Parquet schema")
        meta = json.loads((table.schema.metadata or {})[KEY])
        if meta["schema_version"] != 1:
            raise DataError("unsupported dataset version")
        raw_request = dict(meta["request"])
        raw_request["start"] = date.fromisoformat(raw_request["start"])
        raw_request["end"] = date.fromisoformat(raw_request["end"])
        request = Request(**raw_request)
        dataset = Dataset(
            tuple(Candle(**row) for row in table.to_pylist()),
            Metadata(
                request,
                meta["source"],
                meta["venue"],
                meta["timezone"],
                datetime.fromisoformat(meta["retrieved_at"]),
                meta["provenance"],
                meta["source_version"],
                meta["normalization"],
                meta["schema_version"],
            ),
        )
        validate(dataset)
        if metadata_dict(dataset) != {
            k: v for k, v in meta.items() if k != "fingerprint"
        }:
            raise DataError("stored coverage metadata does not match candles")
        if fingerprint(dataset) != meta["fingerprint"]:
            raise DataError("dataset fingerprint mismatch")
        return dataset
    except (KeyError, TypeError, ValueError, pa.ArrowException) as exc:
        raise DataError(f"invalid cached dataset: {exc}") from exc
