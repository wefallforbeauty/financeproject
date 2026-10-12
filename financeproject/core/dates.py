"""Dates and calendar months. A month is represented by its first day."""

from __future__ import annotations

import calendar
import re
from collections.abc import Iterator
from datetime import date, timedelta

from financeproject.core.text import fold
from financeproject.errors import DateError

MONTHS = (
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
)
MONTHS_SHORT = tuple(m[:3] for m in MONTHS)
FREQUENCIES = ("month", "week", "day")

_ISO = re.compile(r"(\d{4})-(\d{1,2})-(\d{1,2})")
_DAY_FIRST = re.compile(r"(\d{1,2})[./-](\d{1,2})[./-](\d{4})")


def parse_date(text: str | date, today: date | None = None) -> date:
    """Accept ``2026-10-09``, ``09.10.2026``, ``9/10/2026`` (day first), ``today`` and ``yesterday``."""
    if isinstance(text, date):
        return text
    today = today or date.today()
    key = fold(text)
    if key == "today":
        return today
    if key == "yesterday":
        return today - timedelta(days=1)
    try:
        if m := _ISO.fullmatch(key):
            return date(int(m[1]), int(m[2]), int(m[3]))
        if m := _DAY_FIRST.fullmatch(key):
            return date(int(m[3]), int(m[2]), int(m[1]))
    except ValueError as exc:
        raise DateError(f"Invalid date: {text!r} ({exc})") from exc
    raise DateError(f"Cannot read the date {text!r} (examples: 2026-10-09, 09.10.2026)")


def month_start(d: date) -> date:
    return d.replace(day=1)


def days_in_month(m: date) -> int:
    return calendar.monthrange(m.year, m.month)[1]


def month_end(d: date) -> date:
    return d.replace(day=days_in_month(d))


def add_months(m: date, k: int) -> date:
    """First day of the month ``k`` months after the month of ``m``."""
    index = m.year * 12 + (m.month - 1) + k
    return date(index // 12, index % 12 + 1, 1)


def months_between(a: date, b: date) -> int:
    """Whole months from the month of ``a`` to the month of ``b``."""
    return (b.year - a.year) * 12 + (b.month - a.month)


def parse_month(text: str) -> date:
    """Accept ``2026-09``, ``2026-9`` and ``09-2026``; return the first day of that month."""
    s = text.strip()
    if m := re.fullmatch(r"(\d{4})-(\d{1,2})", s):
        year, month = int(m[1]), int(m[2])
    elif m := re.fullmatch(r"(\d{1,2})[-./](\d{4})", s):
        year, month = int(m[2]), int(m[1])
    else:
        raise DateError(f"Cannot read the month {text!r} (example: 2026-09)")
    if not 1 <= month <= 12:
        raise DateError(f"Invalid month: {text!r}")
    return date(year, month, 1)


def format_month(m: date) -> str:
    return f"{m.year:04d}-{m.month:02d}"


def month_label(m: date) -> str:
    """``date(2026, 9, 1)`` → ``"September 2026"``."""
    return f"{MONTHS[m.month - 1]} {m.year}"


def business_days(start: date, end: date) -> Iterator[date]:
    """Monday to Friday between ``start`` and ``end`` inclusive (holidays not removed)."""
    d = start
    while d <= end:
        if d.weekday() < 5:
            yield d
        d += timedelta(days=1)


def sample_dates(start: date, end: date, frequency: str = "month") -> list[date]:
    """Reporting dates from ``start`` to ``end``: month ends (``month``), Sundays
    (``week``) or every day (``day``); ``end`` is always the last date."""
    if end < start:
        return []
    out: list[date] = []
    if frequency == "month":
        d = month_end(start)
        while d < end:
            out.append(d)
            d = month_end(add_months(d, 1))
    elif frequency == "week":
        d = start + timedelta(days=(6 - start.weekday()) % 7)
        while d < end:
            out.append(d)
            d += timedelta(days=7)
    elif frequency == "day":
        d = start
        while d < end:
            out.append(d)
            d += timedelta(days=1)
    else:
        raise DateError(f"Unknown frequency {frequency!r} (month, week or day)")
    out.append(end)
    return out
