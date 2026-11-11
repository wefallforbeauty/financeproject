"""A cash index from an interest rate that changes in steps.

The policy rate of the TCMB (the one-week repo rate) is a simple yearly rate
that holds from the day it is set until the next change. Money kept at it
grows every calendar day by ``1 + R / 365`` (Turkish lira money markets count
actual days over 365), so from day ``a`` to day ``b``

    G(a, b) = Π_{a < d ≤ b} (1 + R_d / 365)

and a rate held for a year compounds to ``(1 + R / 365)^365 − 1``. This is the
risk-free benchmark of the investment reports, before any withholding tax.
"""

from __future__ import annotations

from bisect import bisect_right
from collections.abc import Mapping, Sequence
from datetime import date, timedelta

DAYS = 365


class RateSteps:
    """Rates in per cent a year, each from its day on."""

    def __init__(self, steps: Mapping[date, float]):
        self.days = sorted(steps)
        self.values = [float(steps[d]) for d in self.days]

    def __bool__(self) -> bool:
        return bool(self.days)

    @property
    def first(self) -> date | None:
        return self.days[0] if self.days else None

    def at(self, on: date) -> float | None:
        """The rate in force on ``on`` (``None`` before the first)."""
        i = bisect_right(self.days, on)
        return self.values[i - 1] if i else None

    def last(self) -> tuple[date, float] | None:
        return (self.days[-1], self.values[-1]) if self.days else None

    def growth(self, start: date, end: date) -> float:
        """What 1 becomes from the end of ``start`` to the end of ``end``."""
        if end <= start:
            return 1.0
        if not self.days or start < self.days[0]:
            raise ValueError(f"No interest rate on {start}.")
        factor, day = 1.0, start + timedelta(days=1)
        while day <= end:
            i = bisect_right(self.days, day)
            until = min(end, self.days[i] - timedelta(days=1)) if i < len(self.days) else end
            n = (until - day).days + 1
            factor *= (1 + self.values[i - 1] / 100 / DAYS) ** n
            day = until + timedelta(days=1)
        return factor

    def period_returns(self, dates: Sequence[date]) -> list[float]:
        """The return over each step of ``dates`` (one fewer than the dates)."""
        return [self.growth(a, b) - 1 for a, b in zip(dates, dates[1:])]


def constant(rate_pct_effective: float, first: date = date(1900, 1, 1)) -> RateSteps:
    """Steps for a rate given as an effective yearly rate (40 means 1 becomes 1.40 in a year)."""
    simple = ((1 + rate_pct_effective / 100) ** (1 / DAYS) - 1) * DAYS * 100
    return RateSteps({first: simple})


def effective_annual(rate_pct: float) -> float:
    """The simple yearly rate compounded daily for a year, as a fraction."""
    return (1 + rate_pct / 100 / DAYS) ** DAYS - 1
