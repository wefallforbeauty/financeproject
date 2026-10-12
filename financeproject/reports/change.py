"""Why net worth (or cash) changed: flows by counterpart and revaluation.

``networth_change`` splits the change of net worth between two dates into

- income and expenses (net saving), valued on the day they happened;
- exchange results: what was lost or gained against market prices when
  converting between units (the spread of buying dollars or gold);
- opening balances: accounts that entered the ledger (not a real change);
- revaluation: holding gains and losses of each commodity held.

In real TRY the lira itself has a holding loss (inflation erodes it); in USD or
grams of gold every other unit is revalued against that numeraire.
``cash_change`` does the same for the cash accounts (liquidity tier 0), with
the counterparts grouped as in a cash flow statement. The identity behind both
is in ``core.decompose``.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from financeproject.core.decompose import (
    EXCHANGE,
    Decomposition,
    Event,
    Leg,
    Valuer,
    decompose,
)
from financeproject.core.kinds import ASSET, EXPENSE, INCOME, LIABILITY
from financeproject.errors import FinanceError
from financeproject.ledger.accounts import account_tier
from financeproject.ledger.db import OPENING_ACCOUNT, TRADING_ROOT
from financeproject.ledger.models import Account, Posting, Transaction
from financeproject.reports.context import MarketData, load_market_data

UNITS = {"try": "TRY", "real": "real TRY", "usd": "USD", "gold": "grams of gold"}
NUMERAIRES = {"usd": "USD", "gold": "XAU_G"}

Group = Callable[[Account], bool]
Categorize = Callable[[Account], tuple[str, str]]


def make_valuer(market: MarketData, unit: str = "try") -> Valuer:
    """Value of one unit of a commodity on a day, in ``unit``."""
    if unit not in UNITS:
        raise FinanceError(f"Unknown unit {unit!r}; use one of: {', '.join(UNITS)}")
    book = market.book

    def price(commodity: str, on: date) -> Decimal | None:
        r = book.resolve(commodity, on)
        return None if r is None else r.value

    if unit == "try":
        return price
    if unit in NUMERAIRES:
        numeraire = NUMERAIRES[unit]

        def in_numeraire(commodity: str, on: date) -> Decimal | None:
            p, n = price(commodity, on), price(numeraire, on)
            return None if p is None or n is None else p / n

        return in_numeraire
    if market.deflator is None:
        raise FinanceError("Real values need the CPI: run 'finance price update --only cpi'.")
    deflator, base = market.deflator, market.base_month

    def real(commodity: str, on: date) -> Decimal | None:
        p = price(commodity, on)
        return None if p is None else p * Decimal(repr(deflator.factor(on, base)))

    return real


def balance_sheet(account: Account) -> bool:
    return account.type in (ASSET, LIABILITY)


def cash(account: Account) -> bool:
    """Cash and cash equivalents: asset accounts usable the same day (liquidity tier 0)."""
    return account.type == ASSET and account_tier(account) == 0


def _equity_category(account: Account) -> tuple[str, str]:
    if account.path.startswith(TRADING_ROOT + ":"):
        return EXCHANGE, account.path
    if account.path == OPENING_ACCOUNT:
        return "opening", account.path
    return "equity", account.path


def networth_category(account: Account) -> tuple[str, str]:
    if account.type == INCOME:
        return "income", account.path
    if account.type == EXPENSE:
        return "expenses", account.path
    return _equity_category(account)


REVOLVING_KINDS = ("credit_card", "overdraft")


def cash_category(account: Account) -> tuple[str, str]:
    """Counterparts of cash, grouped as in a cash flow statement."""
    if account.type == INCOME:
        return "income", account.path
    if account.type == EXPENSE:
        return "expenses", account.path
    if account.type == ASSET:
        return "investing", account.path
    if account.type == LIABILITY:
        return ("cards" if account.kind in REVOLVING_KINDS else "loans"), account.path
    return _equity_category(account)


def holdings(session: Session, at: date, in_group: Group) -> dict[str, Decimal]:
    """Quantities per commodity held by the group at the end of ``at``."""
    accounts = {a.id: a for a in session.scalars(select(Account))}
    out: dict[str, Decimal] = defaultdict(Decimal)
    rows = session.execute(
        select(Posting.account_id, Posting.commodity_code, Posting.amount)
        .join(Transaction, Posting.transaction_id == Transaction.id)
        .where(Transaction.date <= at)
    )
    for account_id, code, amount in rows:
        if in_group(accounts[account_id]):
            out[code] += amount
    return {c: v for c, v in out.items() if v}


def events(session: Session, start: date, end: date, in_group: Group, categorize: Categorize) -> list[Event]:
    """Transactions after ``start`` up to ``end`` that touch the group."""
    txs = session.scalars(
        select(Transaction)
        .where(Transaction.date > start, Transaction.date <= end)
        .options(selectinload(Transaction.postings).selectinload(Posting.account))
        .order_by(Transaction.date, Transaction.id)
    )
    out: list[Event] = []
    for tx in txs:
        legs = []
        inside = False
        for p in tx.postings:
            if in_group(p.account):
                legs.append(Leg(p.commodity_code, p.amount))
                inside = True
            else:
                category, detail = categorize(p.account)
                legs.append(Leg(p.commodity_code, p.amount, category, detail))
        if inside:
            out.append(Event(tx.date, tuple(legs)))
    return out


def change(
    session: Session,
    start: date,
    end: date,
    in_group: Group,
    categorize: Categorize,
    unit: str = "try",
    market: MarketData | None = None,
) -> Decomposition:
    if end < start:
        raise FinanceError("The end date is before the start date.")
    market = market or load_market_data(session)
    return decompose(
        start,
        end,
        holdings(session, start, in_group),
        events(session, start, end, in_group, categorize),
        make_valuer(market, unit),
    )


def networth_change(
    session: Session, start: date, end: date, unit: str = "try", market: MarketData | None = None
) -> Decomposition:
    """Change of net worth from the end of ``start`` to the end of ``end``."""
    return change(session, start, end, balance_sheet, networth_category, unit, market)


def cash_change(
    session: Session, start: date, end: date, unit: str = "try", market: MarketData | None = None
) -> Decomposition:
    """Change of cash (tier 0 assets) from the end of ``start`` to the end of ``end``."""
    return change(session, start, end, cash, cash_category, unit, market)
