"""Finding recurring payments (subscriptions, bills, rent, salary) in a history.

The transactions of one payee and category form a candidate series. It counts
as recurring when its typical gap fits a calendar period and most single gaps
stay close to it:

1. The median gap between consecutive dates picks the period (weekly, monthly,
   …, yearly); medians are not thrown off by one late or doubled payment.
2. At least ``min_regularity`` of the gaps must lie within the period's
   tolerance (for monthly: 30.4 ± 6 days).
3. Weekly to quarterly series need three occurrences. Half-yearly and yearly
   ones need two, whose amounts must then lie within 10 % of each other, so
   that two visits to a shop half a year apart do not count.

The amount is "fixed" when every amount lies within 2 % of the median
(subscriptions, rent), otherwise "variable" (utility bills). The next payment is
expected one period after the last; for calendar-month periods on the median
day of the month of the last three payments. A series is "late" once that date
has passed by more than the tolerance, and "stopped" once a whole period has
passed without it.

This is a transparent rule, in the spirit of the recurring-payment and
subscription views of apps such as Monarch and Firefly III, not a fitted model.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from financeproject.core.dates import add_months, days_in_month, month_start


@dataclass(frozen=True)
class Period:
    name: str
    days: float
    """Nominal length in days."""
    months: int | None
    """Length in calendar months, for the periods that follow the calendar."""
    low: float
    high: float
    """Range of the median gap that selects this period."""
    tolerance: int
    """How far a single gap may stray from the nominal length, in days."""
    min_count: int


PERIODS = (
    Period("weekly", 7, None, 5, 9, 2, 3),
    Period("every 2 weeks", 14, None, 12, 16, 3, 3),
    Period("monthly", 30.44, 1, 26, 35, 6, 3),
    Period("every 2 months", 60.88, 2, 55, 66, 8, 3),
    Period("quarterly", 91.31, 3, 84, 98, 10, 3),
    Period("every 6 months", 182.62, 6, 170, 196, 15, 2),
    Period("yearly", 365.25, 12, 350, 380, 20, 2),
)
FIXED_TOLERANCE = Decimal("0.02")
PAIR_TOLERANCE = Decimal("0.10")
ACTIVE, LATE, STOPPED = "active", "late", "stopped"


@dataclass(frozen=True, slots=True)
class Occurrence:
    on: date
    amount: Decimal
    """Size of the payment (positive)."""


@dataclass(frozen=True)
class Series:
    label: str
    category: str
    direction: int
    """−1 for money out, +1 for money in."""
    commodity: str
    period: Period
    occurrences: tuple[Occurrence, ...]
    regularity: float
    """Share of gaps within the period's tolerance."""

    @property
    def last(self) -> Occurrence:
        return self.occurrences[-1]

    @property
    def amounts(self) -> list[Decimal]:
        return [o.amount for o in self.occurrences]

    @property
    def typical(self) -> Decimal:
        """Median of the last three amounts: the amount to expect next."""
        return Decimal(statistics.median(self.amounts[-3:]))

    @property
    def fixed(self) -> bool:
        median = Decimal(statistics.median(self.amounts))
        return median > 0 and all(abs(a - median) <= FIXED_TOLERANCE * median for a in self.amounts)

    @property
    def next_due(self) -> date:
        return next_date(self.period, [o.on for o in self.occurrences])

    def status(self, today: date) -> str:
        overdue = (today - self.next_due).days
        if overdue > self.period.days:
            return STOPPED
        if overdue > self.period.tolerance:
            return LATE
        return ACTIVE

    def due_dates(self, start: date, end: date) -> list[date]:
        """Expected dates in [start, end], continuing the series from its last payment."""
        out, dates = [], [o.on for o in self.occurrences]
        d = next_date(self.period, dates)
        while d <= end:
            if d >= start:
                out.append(d)
            dates.append(d)
            d = next_date(self.period, dates)
        return out


def next_date(period: Period, dates: list[date]) -> date:
    last = dates[-1]
    if period.months is None:
        return last + timedelta(days=round(period.days))
    day = round(statistics.median(d.day for d in dates[-3:]))
    month = add_months(month_start(last), period.months)
    return month.replace(day=min(day, days_in_month(month)))


def classify(dates: list[date]) -> tuple[Period, float] | None:
    """The period of a sorted list of dates and the share of regular gaps, or ``None``."""
    gaps = [(b - a).days for a, b in zip(dates, dates[1:])]
    if not gaps:
        return None
    median = statistics.median(gaps)
    for period in PERIODS:
        if period.low <= median <= period.high:
            if len(dates) < period.min_count:
                return None
            regular = sum(1 for g in gaps if abs(g - period.days) <= period.tolerance) / len(gaps)
            return period, regular
    return None


def _similar(amounts: list[Decimal]) -> bool:
    low, high = min(amounts), max(amounts)
    return low > 0 and high - low <= PAIR_TOLERANCE * high


def find_series(
    candidates: dict[object, tuple[str, str, int, str, list[Occurrence]]],
    min_regularity: float = 0.75,
) -> list[Series]:
    """Recurring series among ``candidates`` (key → label, category, direction, commodity, occurrences)."""
    out: list[Series] = []
    for label, category, direction, commodity, occurrences in candidates.values():
        occurrences = sorted(occurrences, key=lambda o: o.on)
        found = classify([o.on for o in occurrences])
        if found is None:
            continue
        period, regularity = found
        if len(occurrences) < 3 and not _similar([o.amount for o in occurrences]):
            continue
        if regularity >= min_regularity:
            out.append(Series(label, category, direction, commodity, period, tuple(occurrences), regularity))
    return sorted(out, key=lambda s: (s.direction, -s.typical, s.label))
