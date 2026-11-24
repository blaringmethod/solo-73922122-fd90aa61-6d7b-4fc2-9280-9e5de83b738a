"""UTC / real-second conversions for the exposure ledger.

The ledger stores bare UTC calendar text without a timezone suffix.  To make
leap seconds first-class every instant is also kept as an integer number of
"real seconds" since 2000-01-01 00:00:00 UTC.  On a positive leap-second day
the civil day is 86401 real seconds long, and 23:59:60 is a real second of its
own.

Every conversion takes an explicit :class:`LeapTable`.  A single request reads
one table and uses that same table for parsing, day boundaries, clipping and
formatting, so a concurrent table replacement can never mix two tables.
"""

from __future__ import annotations

import bisect
import datetime as _dt
import re
from dataclasses import dataclass

EPOCH = _dt.datetime(2000, 1, 1, 0, 0, 0)
EPOCH_DATE = EPOCH.date()
DAY = 86400
UTC_RE = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class TimecodeError(ValueError):
    """Raised when UTC text or a leap-second table is not usable."""


@dataclass(frozen=True)
class LeapTable:
    """An immutable, validated set of positive leap-second dates."""

    dates: tuple[_dt.date, ...]

    def __post_init__(self) -> None:
        for day in self.dates:
            if not isinstance(day, _dt.date):
                raise TimecodeError("leap dates must be dates")
        if tuple(sorted(set(self.dates))) != self.dates:
            raise TimecodeError("leap dates must be sorted and unique")

    def __contains__(self, day: _dt.date) -> bool:
        return day in self.dates

    def effective_dates(self) -> tuple[_dt.date, ...]:
        """Leap days that can affect instants at or after the epoch."""

        return tuple(day for day in self.dates if day >= EPOCH_DATE)


DEFAULT_LEAP_DATES = (
    _dt.date(2005, 12, 31),
    _dt.date(2008, 12, 31),
    _dt.date(2012, 6, 30),
    _dt.date(2015, 6, 30),
    _dt.date(2016, 12, 31),
)
DEFAULT_LEAP_TABLE = LeapTable(DEFAULT_LEAP_DATES)


def parse_date(text: str) -> _dt.date:
    if not isinstance(text, str):
        raise TimecodeError("date must be text")
    text = text.strip()
    if not DATE_RE.match(text):
        raise TimecodeError("dates must look like YYYY-MM-DD")
    try:
        return _dt.date.fromisoformat(text)
    except ValueError as exc:
        raise TimecodeError(f"invalid date {text!r}") from exc


def normalize_utc_text(text: str) -> str:
    """Normalise the accepted UTC spellings to ``YYYY-MM-DD HH:MM:SS``."""

    if not isinstance(text, str):
        raise TimecodeError("UTC time must be text")
    value = text.strip()
    if value[-1:] in ("Z", "z"):
        value = value[:-1].rstrip()
    if "T" in value and " " not in value:
        value = value.replace("T", " ", 1)
    if not UTC_RE.match(value):
        raise TimecodeError(
            "UTC times must look like YYYY-MM-DD HH:MM:SS (an optional trailing Z is allowed)"
        )
    return value


def parse_utc(text: str, table: LeapTable) -> _dt.datetime:
    """Parse UTC calendar text against one leap table.

    ``23:59:60`` is accepted only at the end of a positive leap day and is
    returned as ``23:59:59``; :func:`utc_to_real` recognises the literal ``:60``
    suffix and advances one more real second.
    """

    value = normalize_utc_text(text)
    date_part, time_part = value.split(" ")
    try:
        day = _dt.date.fromisoformat(date_part)
        hour, minute, second = (int(part) for part in time_part.split(":"))
    except ValueError as exc:
        raise TimecodeError(f"invalid UTC time {text!r}") from exc

    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise TimecodeError(f"invalid clock fields in {text!r}")
    if second == 60:
        if hour != 23 or minute != 59:
            raise TimecodeError("second 60 is only valid at 23:59:60")
        if day not in table:
            raise TimecodeError(
                f"{text!r} uses second 60 but {day.isoformat()} is not a leap day "
                "in the active leap table"
            )
        return _dt.datetime(day.year, day.month, day.day, 23, 59, 59)
    if not (0 <= second <= 59):
        raise TimecodeError(f"invalid seconds in {text!r}")
    return _dt.datetime(day.year, day.month, day.day, hour, minute, second)


def _leaps_before(day: _dt.date, table: LeapTable) -> int:
    """Leap seconds inserted on epoch..day-1."""

    dates = table.dates
    return bisect.bisect_left(dates, day) - bisect.bisect_left(dates, EPOCH_DATE)


def utc_to_real(text_or_dt: str | _dt.datetime, table: LeapTable) -> int:
    """Convert UTC text (or naive datetime) to real seconds since the epoch."""

    if isinstance(text_or_dt, str):
        dt = parse_utc(text_or_dt, table)
        on_leap_second = normalize_utc_text(text_or_dt).endswith(":60")
    else:
        dt = text_or_dt
        on_leap_second = False

    real = int((dt - EPOCH).total_seconds()) + _leaps_before(dt.date(), table)
    if on_leap_second:
        real += 1
    return real


def real_to_utc(real: int, table: LeapTable) -> tuple[_dt.datetime, bool]:
    """Convert real seconds to ``(UTC datetime, on_leap_second)``.

    ``on_leap_second`` is true for the extra ``23:59:60`` second, whose
    datetime reads 23:59:59.
    """

    if not isinstance(real, int) or isinstance(real, bool):
        raise TimecodeError("real seconds must be an integer")
    if real < 0:
        raise TimecodeError("real seconds must not be before 2000-01-01 00:00:00 UTC")

    leap_days = set(table.dates)
    # ``day_start(day)`` is monotone in the day, so bisect over a window that
    # certainly contains the answer (each leap can shift it by one day).
    max_offset = (_dt.date.max - EPOCH_DATE).days
    lo = max(0, real // DAY - len(table.dates) - 2)
    hi = min(max_offset, real // DAY + 2)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if _day_start_offset(mid, table) <= real:
            lo = mid
        else:
            hi = mid - 1
    guess = EPOCH_DATE + _dt.timedelta(days=lo)
    start = _day_start_offset(lo, table)
    while start > real and guess > EPOCH_DATE:  # safety for unusual tables
        guess -= _dt.timedelta(days=1)
        lo -= 1
        start = _day_start_offset(lo, table)

    schedule = real - start
    if guess in leap_days and schedule >= DAY:
        return _dt.datetime(guess.year, guess.month, guess.day, 23, 59, 59), True
    if schedule >= DAY:
        raise TimecodeError("real second lies beyond the supported calendar range")
    return (
        _dt.datetime(guess.year, guess.month, guess.day)
        + _dt.timedelta(seconds=schedule),
        False,
    )


def _day_start_offset(day_offset: int, table: LeapTable) -> int:
    day = EPOCH_DATE + _dt.timedelta(days=day_offset)
    return day_offset * DAY + _leaps_before(day, table)


def fmt_utc(dt: _dt.datetime, on_leap_second: bool = False) -> str:
    if on_leap_second:
        return dt.strftime("%Y-%m-%d %H:%M:60")
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def day_length(day: _dt.date, table: LeapTable) -> int:
    return DAY + (1 if day in table else 0)


def day_range_real(day: _dt.date, table: LeapTable) -> tuple[int, int]:
    """Half-open ``[start, end)`` real-second range of a UTC civil day."""

    start = utc_to_real(f"{day.isoformat()} 00:00:00", table)
    return start, start + day_length(day, table)


def parse_leap_table(text: str) -> LeapTable:
    """Parse a positive leap-second date list, one ``YYYY-MM-DD`` per line.

    Blank lines and ``#`` comments are ignored.  The whole submission is
    validated; one bad line rejects all of it.
    """

    if not isinstance(text, str):
        raise TimecodeError("leap table must be text")
    dates: list[_dt.date] = []
    seen: set[_dt.date] = set()
    for lineno, raw in enumerate(text.splitlines(), 1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        if not DATE_RE.match(line):
            raise TimecodeError(f"line {lineno}: expected YYYY-MM-DD, got {raw!r}")
        try:
            day = _dt.date.fromisoformat(line)
        except ValueError as exc:
            raise TimecodeError(f"line {lineno}: invalid date {line!r}") from exc
        if day in seen:
            raise TimecodeError(f"line {lineno}: duplicate date {line}")
        seen.add(day)
        dates.append(day)
    return LeapTable(tuple(sorted(dates)))


def format_leap_table(table: LeapTable) -> str:
    return "".join(f"{day.isoformat()}\n" for day in table.dates)
