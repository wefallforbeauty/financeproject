"""Reading and writing prices and index values in the database.

Source priority: a manual price is never overwritten by a fetched one, and for
the CPI the official EVDS levels replace the chained TCMB table, which replaces
the snapshot shipped with the package.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from financeproject.core.prices import PriceBook, Quote
from financeproject.core.rates import RateSteps
from financeproject.ledger.models import IndexValue, Price, StockSplit

MANUAL = "manual"
CPI = "CPI"
POLICY_RATE = "POLICY_RATE"
BUNDLED_RATES = "policy_rate_tcmb.csv"
CPI_SOURCE_PRIORITY = {"evds": 3, "tcmb-table": 2, "tcmb-rates": 2, "bundled": 1}

ADDED, UPDATED, SAME, KEPT = "added", "updated", "unchanged", "kept"


def upsert_price(session: Session, commodity: str, quote: str, on: date, value: Decimal, source: str) -> str:
    existing = session.scalar(
        select(Price).where(Price.commodity_code == commodity, Price.quote_code == quote, Price.date == on)
    )
    if existing is None:
        session.add(Price(commodity_code=commodity, quote_code=quote, date=on, value=value, source=source))
        return ADDED
    if existing.source == MANUAL and source != MANUAL:
        return KEPT
    if existing.value == value and existing.source == source:
        return SAME
    existing.value, existing.source, existing.fetched_at = value, source, datetime.now()
    return UPDATED


def price_dates(session: Session, commodity: str, quote: str) -> set[date]:
    return set(
        session.scalars(select(Price.date).where(Price.commodity_code == commodity, Price.quote_code == quote))
    )


def price_range(session: Session, commodity: str, quote: str) -> tuple[date | None, date | None]:
    return session.execute(
        select(func.min(Price.date), func.max(Price.date)).where(
            Price.commodity_code == commodity, Price.quote_code == quote
        )
    ).one()


def load_pricebook(session: Session, base: str = "TRY") -> PriceBook:
    quotes = (
        Quote(p.commodity_code, p.quote_code, p.date, p.value, p.source)
        for p in session.scalars(select(Price))
    )
    return PriceBook(quotes, base)


def upsert_index(
    session: Session, series: str, period: date, value: Decimal, source: str, base: str | None = None
) -> str:
    existing = session.scalar(select(IndexValue).where(IndexValue.series == series, IndexValue.period == period))
    if existing is None:
        session.add(IndexValue(series=series, period=period, value=value, source=source, base=base))
        return ADDED
    old_rank = CPI_SOURCE_PRIORITY.get(existing.source, 0)
    new_rank = CPI_SOURCE_PRIORITY.get(source, 0)
    if new_rank < old_rank:
        return KEPT
    if existing.value == value and existing.source == source:
        return SAME
    existing.value, existing.source, existing.base, existing.fetched_at = value, source, base, datetime.now()
    return UPDATED


def load_index(session: Session, series: str = CPI) -> dict[date, float]:
    rows = session.execute(
        select(IndexValue.period, IndexValue.value).where(IndexValue.series == series).order_by(IndexValue.period)
    )
    return {period: float(value) for period, value in rows}


def index_sources(session: Session, series: str = CPI) -> dict[str, int]:
    rows = session.execute(
        select(IndexValue.source, func.count()).where(IndexValue.series == series).group_by(IndexValue.source)
    )
    return dict(rows.all())


def bundled_rates() -> dict[date, float]:
    """The policy rate snapshot shipped with the package."""
    import csv
    from importlib import resources

    text = resources.files("financeproject.data").joinpath(BUNDLED_RATES).read_text(encoding="utf-8")
    return {date.fromisoformat(row["from"]): float(row["rate_pct"]) for row in csv.DictReader(text.splitlines())}


def load_rates(session: Session) -> tuple[RateSteps, str]:
    """The policy rate steps and where the latest come from: the snapshot shipped with the package,
    with the ledger's own (``finance price update``) on top."""
    steps = bundled_rates()
    stored = load_index(session, POLICY_RATE)
    steps.update({day: float(rate) for day, rate in stored.items()})
    return RateSteps(steps), "tcmb-rates" if stored else "bundled"


def upsert_split(session: Session, commodity: str, on: date, ratio: Decimal, source: str) -> str:
    existing = session.scalar(select(StockSplit).where(StockSplit.commodity_code == commodity, StockSplit.date == on))
    if existing is None:
        session.add(StockSplit(commodity_code=commodity, date=on, ratio=ratio, source=source))
        return ADDED
    if existing.ratio == ratio:
        return SAME
    existing.ratio, existing.source = ratio, source
    return UPDATED


def stored_splits(session: Session, commodity: str) -> list[tuple[date, float]]:
    rows = session.execute(
        select(StockSplit.date, StockSplit.ratio).where(StockSplit.commodity_code == commodity).order_by(StockSplit.date)
    )
    return [(on, float(ratio)) for on, ratio in rows]
