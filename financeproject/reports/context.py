"""Prices and CPI loaded once per report."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from sqlalchemy.orm import Session

from financeproject.core.inflation import Deflator
from financeproject.core.prices import PriceBook
from financeproject.prices.store import index_sources, load_index, load_pricebook


@dataclass(frozen=True)
class MarketData:
    book: PriceBook
    deflator: Deflator | None
    cpi_sources: dict[str, int]
    cpi_base: date | None = None
    """Month whose prices real TRY is expressed in; default: the latest CPI month."""

    @property
    def base_month(self) -> date | None:
        if self.deflator is None:
            return None
        return self.cpi_base or self.deflator.last_month


def load_market_data(session: Session, cpi_method: str = "interpolate", cpi_base: date | None = None) -> MarketData:
    index = load_index(session)
    deflator = Deflator(index, cpi_method) if index else None
    return MarketData(load_pricebook(session), deflator, index_sources(session), cpi_base)
