"""Consumer price index: chain-linking, rebasing, splicing, deflating.

Months are the first day of the month. Index levels are floats: the published
figures are rounded to two decimals, so exact decimal arithmetic would add
nothing here. Methods and references: docs/METHOD.md.
"""

from __future__ import annotations

import math
from bisect import bisect_right
from collections.abc import Mapping
from datetime import date

from financeproject.core.dates import add_months, days_in_month, month_start


def _check_consecutive(months: list[date]) -> None:
    for a, b in zip(months, months[1:]):
        if add_months(a, 1) != b:
            raise ValueError(f"Gap in the monthly series: {a:%Y-%m} → {b:%Y-%m}")


def chain_index(monthly_pct: Mapping[date, float], base_year: int | None = 2025) -> dict[date, float]:
    """Index levels from monthly % changes: I_m = I_{m-1} · (1 + r_m / 100).

    The month before the first change gets 100; with ``base_year`` the result is
    rescaled so the mean of that year's twelve months is 100 (the 2025=100
    convention of TurkStat, TÜİK).
    """
    if not monthly_pct:
        return {}
    months = sorted(monthly_pct)
    _check_consecutive(months)
    level = 100.0
    out: dict[date, float] = {}
    for m in months:
        level *= 1.0 + float(monthly_pct[m]) / 100.0
        out[m] = level
    return rebase(out, base_year) if base_year is not None else out


def rebase(index: Mapping[date, float], base_year: int) -> dict[date, float]:
    """Rescale so the average over ``base_year`` is 100."""
    base = [v for m, v in index.items() if m.year == base_year]
    if len(base) != 12:
        raise ValueError(f"Rebasing needs the 12 months of {base_year}; found {len(base)}.")
    scale = 100.0 / (sum(base) / 12.0)
    return {m: v * scale for m, v in sorted(index.items())}


def monthly_pct(index: Mapping[date, float]) -> dict[date, float]:
    return {
        m: (v / index[prev] - 1.0) * 100.0
        for m, v in sorted(index.items())
        if (prev := add_months(m, -1)) in index
    }


def annual_pct(index: Mapping[date, float]) -> dict[date, float]:
    return {
        m: (v / index[prev] - 1.0) * 100.0
        for m, v in sorted(index.items())
        if (prev := add_months(m, -12)) in index
    }


def splice(older: Mapping[date, float], newer: Mapping[date, float], min_overlap: int = 6) -> dict[date, float]:
    """Chain an older base onto a newer one.

    Two rebasings of one index differ by a constant factor, estimated as the
    mean ratio over the overlapping months. The newer series wins where both
    exist.
    """
    overlap = sorted(set(older) & set(newer))
    if len(overlap) < min_overlap:
        raise ValueError(f"Splicing needs at least {min_overlap} common months; found {len(overlap)}.")
    ratio = sum(newer[m] / older[m] for m in overlap) / len(overlap)
    out = {m: v * ratio for m, v in older.items() if m not in newer}
    out.update(newer)
    return dict(sorted(out.items()))


def _mid_month(m: date) -> float:
    """Ordinal of the middle of the month (between the 15th and 16th of a 30-day month)."""
    return m.toordinal() + (days_in_month(m) - 1) / 2.0


class Deflator:
    """Converts TRY amounts on any day into the prices of a base month.

    ``method="interpolate"`` (default): each monthly index is placed at the
    middle of its month and the price level on a given day is interpolated
    log-linearly between neighboring months. ``method="monthly"``: every day of a
    month uses that month's index.

    After the last published month the level is held constant: inflation since
    then is not known yet. ``is_extrapolated`` reports those days.
    """

    METHODS = ("interpolate", "monthly")

    def __init__(self, index: Mapping[date, float], method: str = "interpolate"):
        if not index:
            raise ValueError("The CPI series is empty.")
        if method not in self.METHODS:
            raise ValueError(f"Unknown method {method!r} ({', '.join(self.METHODS)})")
        self.method = method
        self._months = sorted(index)
        _check_consecutive(self._months)
        self._values = [float(index[m]) for m in self._months]
        self._by_month = dict(zip(self._months, self._values))
        self._anchors = [_mid_month(m) for m in self._months]
        self._logs = [math.log(v) for v in self._values]

    @property
    def first_month(self) -> date:
        return self._months[0]

    @property
    def last_month(self) -> date:
        return self._months[-1]

    def month_value(self, m: date) -> float:
        return self._by_month[month_start(m)]

    def level(self, d: date) -> float:
        """Price level on day ``d``."""
        if self.method == "monthly":
            m = month_start(d)
            if m < self._months[0]:
                return self._values[0]
            if m > self._months[-1]:
                return self._values[-1]
            return self._by_month[m]
        t = d.toordinal()
        i = bisect_right(self._anchors, t)
        if i == 0:
            return self._values[0]
        if i == len(self._anchors):
            return self._values[-1]
        t0, t1 = self._anchors[i - 1], self._anchors[i]
        w = (t - t0) / (t1 - t0)
        return math.exp(self._logs[i - 1] + w * (self._logs[i] - self._logs[i - 1]))

    def is_extrapolated(self, d: date) -> bool:
        """True outside the published range, where the level is held constant."""
        if self.method == "monthly":
            m = month_start(d)
            return m < self._months[0] or m > self._months[-1]
        t = d.toordinal()
        return t < self._anchors[0] or t > self._anchors[-1]

    def factor(self, d: date, base: date | None = None) -> float:
        """Multiply a TRY amount of day ``d`` by this to get base-month TRY."""
        base_month = month_start(base) if base else self.last_month
        if base_month not in self._by_month:
            raise ValueError(f"The base month is not in the CPI series: {base_month:%Y-%m}")
        return self._by_month[base_month] / self.level(d)


def fisher_real_rate(nominal: float, inflation: float) -> float:
    """Real rate from the Fisher equation: (1 + i) / (1 + π) − 1 (rates as fractions)."""
    return (1.0 + nominal) / (1.0 + inflation) - 1.0
