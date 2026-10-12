"""Personal balance sheet at one date."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy.orm import Session

from financeproject.core.kinds import ASSET, LIABILITY
from financeproject.core.valuation import (
    DEFAULT_MAX_AGE_DAYS,
    Liquidity,
    NetWorth,
    ValuedPosition,
    group_values,
    liquidity,
    net_worth,
    value_positions,
)
from financeproject.ledger.accounts import SHARED_OWNER
from financeproject.ledger.queries import positions_at
from financeproject.reports.context import MarketData, load_market_data


@dataclass(frozen=True)
class BalanceSheet:
    at: date
    owner: str | None
    assets: list[ValuedPosition]
    liabilities: list[ValuedPosition]
    net_worth: NetWorth
    liquidity: Liquidity
    by_owner: dict[str, Decimal]
    market: MarketData

    @property
    def empty(self) -> bool:
        return not self.assets and not self.liabilities


def build_balance_sheet(
    session: Session,
    at: date,
    owner: str | None = None,
    market: MarketData | None = None,
    max_age_days: int = DEFAULT_MAX_AGE_DAYS,
) -> BalanceSheet:
    market = market or load_market_data(session)
    valued = value_positions(positions_at(session, at, owner=owner), market.book, at, max_age_days)
    nw = net_worth(valued, market.book, at, market.deflator, market.base_month)
    return BalanceSheet(
        at=at,
        owner=owner,
        assets=[v for v in valued if v.position.account_type == ASSET],
        liabilities=[v for v in valued if v.position.account_type == LIABILITY],
        net_worth=nw,
        liquidity=liquidity(valued),
        by_owner={str(k): v for k, v in group_values(valued, lambda p: p.owner or SHARED_OWNER).items()},
        market=market,
    )
