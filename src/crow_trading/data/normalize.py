"""Calendar-aware expected coverage and strict OHLCV validation."""

import math
from collections.abc import Mapping
from datetime import UTC, date, datetime, time, timedelta
from typing import cast

import exchange_calendars as xcals
import pandas as pd
from exchange_calendars.errors import CalendarError

from crow_trading.data.models import DataError, Dataset, Request, utc


def schedule(request: Request) -> tuple[dict[datetime, datetime], str]:
    """Map UTC candle opens to closes for dates in [start, end)."""
    result: dict[datetime, datetime] = {}
    if request.kind == "crypto":
        day = datetime.combine(request.start, time(), UTC)
        end = datetime.combine(request.end, time(), UTC)
        while day < end:
            result[day] = day + timedelta(days=1)
            day += timedelta(days=1)
        return result, "UTC"
    try:
        calendar = xcals.get_calendar(
            request.calendar,
            start=str(request.start - timedelta(days=7)),
            end=str(request.end + timedelta(days=7)),
        )
        sessions = calendar.sessions_in_range(
            str(request.start), str(request.end - timedelta(days=1))
        )
        for session in sessions:
            opened = cast(datetime, calendar.session_open(session).to_pydatetime())
            closed = cast(datetime, calendar.session_close(session).to_pydatetime())
            result[utc(opened)] = utc(closed)
        return result, str(calendar.tz)
    except (ValueError, KeyError, CalendarError) as exc:
        raise DataError(f"invalid or unavailable calendar: {request.calendar}") from exc


def stock_session_open(
    label: object, opens: Mapping[date, datetime], timezone: str
) -> datetime:
    """Interpret provider daily labels in the declared exchange timezone."""
    stamp = pd.Timestamp(label)
    if stamp.tzinfo is not None:
        stamp = stamp.tz_convert(timezone)
    session_date = stamp.date()
    if session_date not in opens:
        raise DataError(f"provider returned an unexpected session date: {session_date}")
    return opens[session_date]


def validate(dataset: Dataset, *, as_of: datetime | None = None) -> None:
    now = utc(as_of or datetime.now(UTC))
    expected, timezone = schedule(dataset.metadata.request)
    if not expected:
        raise DataError("requested window contains no trading sessions")
    if dataset.metadata.timezone != timezone:
        raise DataError("source timezone does not match the requested calendar")
    if not dataset.metadata.source or not dataset.metadata.venue:
        raise DataError("source and venue are required")
    if not dataset.metadata.provenance or not dataset.metadata.source_version:
        raise DataError("source provenance and version are required")
    retrieved_at = utc(dataset.metadata.retrieved_at)
    if retrieved_at > now:
        raise DataError("retrieval time is in the future")
    if max(expected.values()) > min(now, retrieved_at):
        raise DataError("requested window includes an incomplete or future candle")
    if not dataset.candles:
        raise DataError("provider returned no candles")
    seen: set[datetime] = set()
    previous: datetime | None = None
    for candle in dataset.candles:
        stamp = utc(candle.timestamp)
        if candle.timestamp.utcoffset() != timedelta(
            0
        ) or candle.closed_at.utcoffset() != timedelta(0):
            raise DataError("candle timestamps must be normalized to UTC")
        closed_at = utc(candle.closed_at)
        if stamp in seen:
            raise DataError(f"duplicate candle: {stamp.isoformat()}")
        if previous is not None and stamp < previous:
            raise DataError("candles must be ordered by timestamp")
        if stamp not in expected or closed_at != expected[stamp]:
            raise DataError(f"unexpected candle interval: {stamp.isoformat()}")
        values = (candle.open, candle.high, candle.low, candle.close, candle.volume)
        if not all(math.isfinite(value) for value in values):
            raise DataError("OHLCV must be finite")
        if min(values[:4]) <= 0 or candle.volume < 0:
            raise DataError("prices must be positive and volume nonnegative")
        if not (
            candle.low
            <= min(candle.open, candle.close)
            <= max(candle.open, candle.close)
            <= candle.high
        ):
            raise DataError("invalid OHLC bounds")
        seen.add(stamp)
        previous = stamp
    missing = expected.keys() - seen
    if missing:
        first = min(missing).isoformat()
        raise DataError(
            f"insufficient coverage: {len(missing)} missing candles; first={first}"
        )
