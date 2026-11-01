"""Calendar-derived daily publication deadlines, independent of local timezone."""

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from crow_trading.data.models import DataError, Request, utc
from crow_trading.data.normalize import schedule
from crow_trading.scanner.config import Instrument


@dataclass(frozen=True)
class Due:
    opens: tuple[datetime, ...]
    closes: dict[datetime, datetime]
    timezone: str
    next_ready_at: datetime | None

    @property
    def latest(self) -> datetime:
        return self.opens[-1]

    def day(self, opened: datetime) -> date:
        return opened.astimezone(ZoneInfo(self.timezone)).date()


def due_sessions(item: Instrument, now: datetime) -> Due:
    now = utc(now)
    span = 4 * (item.history_bars + item.max_catchup_bars + item.overlap_bars) + 14
    request = Request(
        item.symbol,
        now.date() - timedelta(days=span),
        now.date() + timedelta(days=14),
        item.currency,
        item.kind,
        item.calendar,
        adjusted=item.adjusted,
    )
    expected, timezone = schedule(request)
    delay = timedelta(seconds=item.finalization_delay_seconds)
    ready = {
        opened: closed for opened, closed in expected.items() if closed + delay <= now
    }
    if len(ready) < item.history_bars:
        raise DataError("calendar has insufficient ready history")
    upcoming = [closed + delay for closed in expected.values() if closed + delay > now]
    return Due(tuple(ready), ready, timezone, min(upcoming) if upcoming else None)


def request_window(
    item: Instrument, due: Due, previous: datetime | None, cached_start: date | None
) -> Request:
    end_index = len(due.opens) - 1
    required_start = due.day(due.opens[-item.history_bars])
    if previous is None:
        start = required_start
    else:
        if previous > due.latest:
            raise DataError(
                "clock/publication policy is earlier than committed progress"
            )
        if previous not in due.closes:
            raise DataError(
                "backlog exceeds supported lookback; increase catch-up limit"
            )
        index = due.opens.index(previous)
        if end_index - index > item.max_catchup_bars:
            raise DataError(
                "backlog exceeds max_catchup_bars; increase it or rebootstrap"
            )
        overlap_start = due.day(due.opens[max(0, index - item.overlap_bars + 1)])
        start = (
            min(required_start, overlap_start)
            if cached_start is None or cached_start > required_start
            else overlap_start
        )
    return Request(
        item.symbol,
        start,
        due.day(due.latest) + timedelta(days=1),
        item.currency,
        item.kind,
        item.calendar,
        adjusted=item.adjusted,
    )
