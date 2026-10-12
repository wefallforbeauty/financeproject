"""Net worth over time in the four units."""

from __future__ import annotations

from datetime import date

import pandas as pd
from sqlalchemy.orm import Session

from financeproject.core.dates import sample_dates
from financeproject.core.valuation import (
    DEFAULT_MAX_AGE_DAYS,
    liquidity,
    net_worth,
    value_positions,
)
from financeproject.ledger.queries import positions_over_time, transaction_date_range
from financeproject.reports.context import MarketData, load_market_data

COLUMNS = [
    "date", "nominal_try", "real_try", "usd", "gold_g", "assets", "liabilities", "liquid_net", "missing_prices", "stale_prices",
]


def networth_series(
    session: Session,
    start: date | None = None,
    end: date | None = None,
    frequency: str = "month",
    owner: str | None = None,
    market: MarketData | None = None,
    max_age_days: int = DEFAULT_MAX_AGE_DAYS,
) -> pd.DataFrame:
    """One row per reporting date: net worth in TRY, real TRY, USD and grams of gold,
    assets, liabilities and liquid net assets (all TRY except the two units)."""
    first, last = transaction_date_range(session)
    if first is None:
        return pd.DataFrame(columns=COLUMNS)
    start = start or first
    end = end or max(last, date.today())
    market = market or load_market_data(session)
    dates = sample_dates(start, end, frequency)
    rows = []
    for d, positions in positions_over_time(session, dates, owner=owner).items():
        valued = value_positions(positions, market.book, d, max_age_days)
        nw = net_worth(valued, market.book, d, market.deflator, market.base_month)
        u = nw.net
        rows.append(
            {
                "date": d,
                "nominal_try": float(u.try_),
                "real_try": float(u.real_try) if u.real_try is not None else None,
                "usd": float(u.usd) if u.usd is not None else None,
                "gold_g": float(u.gold_g) if u.gold_g is not None else None,
                "assets": float(nw.assets),
                "liabilities": float(nw.liabilities),
                "liquid_net": float(liquidity(valued).liquid_net),
                "missing_prices": len(nw.missing),
                "stale_prices": len(nw.stale),
            }
        )
    return pd.DataFrame(rows, columns=COLUMNS)
