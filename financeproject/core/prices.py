"""As-of price lookup with one-hop cross rates.

A quote says "one unit of ``commodity`` cost ``value`` units of ``quote`` on
``on``". The price of a commodity at a date is the latest quote on or before
that date. Gold is stored per gram in USD and reaches TRY through the USD/TRY
rate (one hop), so the gold and USD units stay consistent with each other.
"""

from __future__ import annotations

from bisect import bisect_right
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

TROY_OUNCE_GRAMS = Decimal("31.1034768")
"""Grams in one troy ounce (exact by definition)."""


@dataclass(frozen=True, slots=True)
class Quote:
    commodity: str
    quote: str
    on: date
    value: Decimal
    source: str = ""


@dataclass(frozen=True, slots=True)
class ResolvedPrice:
    """Price of one unit in the requested currency.

    ``as_of`` is the oldest date among the quotes used, so a stale leg makes the
    whole price stale.
    """

    value: Decimal
    as_of: date
    path: tuple[Quote, ...] = ()

    def age_days(self, at: date) -> int:
        return (at - self.as_of).days


class PriceBook:
    """In-memory price history."""

    def __init__(self, quotes: Iterable[Quote] = (), base: str = "TRY"):
        self.base = base
        self._series: dict[tuple[str, str], list[Quote]] = {}
        for q in quotes:
            self._series.setdefault((q.commodity, q.quote), []).append(q)
        self._dates: dict[tuple[str, str], list[date]] = {}
        for key, items in self._series.items():
            items.sort(key=lambda q: q.on)
            self._dates[key] = [q.on for q in items]

    def __len__(self) -> int:
        return sum(len(v) for v in self._series.values())

    def pairs(self) -> list[tuple[str, str]]:
        return sorted(self._series)

    def latest(self, commodity: str, quote: str, at: date) -> Quote | None:
        """Latest quote on or before ``at``."""
        key = (commodity, quote)
        dates = self._dates.get(key)
        if not dates:
            return None
        i = bisect_right(dates, at)
        return self._series[key][i - 1] if i else None

    def resolve(self, commodity: str, at: date, quote: str | None = None) -> ResolvedPrice | None:
        """Price of ``commodity`` in ``quote`` (default: the base currency).

        Tries a direct quote, then one intermediate currency. When several
        routes exist, the one whose oldest leg is most recent wins.
        """
        target = quote or self.base
        if commodity == target:
            return ResolvedPrice(Decimal(1), at)
        routes: list[ResolvedPrice] = []
        direct = self.latest(commodity, target, at)
        if direct is not None:
            routes.append(ResolvedPrice(direct.value, direct.on, (direct,)))
        for c, middle in self._series:
            if c != commodity or middle == target:
                continue
            leg1 = self.latest(commodity, middle, at)
            leg2 = self.latest(middle, target, at)
            if leg1 is not None and leg2 is not None:
                routes.append(ResolvedPrice(leg1.value * leg2.value, min(leg1.on, leg2.on), (leg1, leg2)))
        if not routes:
            return None
        return max(routes, key=lambda r: (r.as_of, -len(r.path)))

    def first_date(self, commodity: str, quote: str) -> date | None:
        dates = self._dates.get((commodity, quote))
        return dates[0] if dates else None

    def last_date(self, commodity: str, quote: str) -> date | None:
        dates = self._dates.get((commodity, quote))
        return dates[-1] if dates else None
