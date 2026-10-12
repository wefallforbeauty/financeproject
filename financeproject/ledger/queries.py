"""Balances read from the ledger."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from datetime import date
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from financeproject.core.kinds import (
    ASSET,
    BALANCE_SHEET_TYPES,
    EQUITY,
    EXPENSE,
    INCOME,
    LIABILITY,
)
from financeproject.core.text import fold
from financeproject.core.valuation import Position
from financeproject.ledger.accounts import SHARED_OWNER, account_tier, owner_name
from financeproject.ledger.models import Account, Posting, Transaction

TYPE_ORDER = {ASSET: 0, LIABILITY: 1, EQUITY: 2, INCOME: 3, EXPENSE: 4}


def _accounts(session: Session) -> dict[int, Account]:
    return {a.id: a for a in session.scalars(select(Account).options(selectinload(Account.owner)))}


def to_position(account: Account, commodity: str, quantity: Decimal) -> Position:
    return Position(
        account_id=account.id,
        account=account.path,
        account_type=account.type,
        commodity=commodity,
        quantity=quantity,
        kind=account.kind,
        owner=owner_name(account),
        tier=account_tier(account),
        institution=account.institution,
    )


def _keep(account: Account, types: Iterable[str], owner: str | None) -> bool:
    if account.type not in types:
        return False
    return owner is None or fold(owner_name(account)) == fold(owner)


def _sort_key(p: Position) -> tuple:
    return (TYPE_ORDER.get(p.account_type, 9), p.account, p.commodity)


def positions_at(
    session: Session,
    at: date,
    types: Iterable[str] = BALANCE_SHEET_TYPES,
    owner: str | None = None,
) -> list[Position]:
    """Non-zero balances per account and commodity at the end of day ``at``.

    ``owner`` filters by account owner; ``"shared"`` selects shared accounts.
    """
    types = tuple(types)
    stmt = (
        select(Posting.account_id, Posting.commodity_code, Posting.amount)
        .join(Transaction, Posting.transaction_id == Transaction.id)
        .where(Transaction.date <= at)
    )
    totals: dict[tuple[int, str], Decimal] = defaultdict(Decimal)
    for account_id, code, amount in session.execute(stmt):
        totals[(account_id, code)] += amount
    accounts = _accounts(session)
    out = [
        to_position(accounts[a], code, qty)
        for (a, code), qty in totals.items()
        if qty != 0 and _keep(accounts[a], types, owner)
    ]
    return sorted(out, key=_sort_key)


def positions_over_time(
    session: Session,
    dates: Iterable[date],
    types: Iterable[str] = BALANCE_SHEET_TYPES,
    owner: str | None = None,
) -> dict[date, list[Position]]:
    """``positions_at`` for many dates in one pass over the postings."""
    types = tuple(types)
    rows = session.execute(
        select(Transaction.date, Posting.account_id, Posting.commodity_code, Posting.amount)
        .join(Transaction, Posting.transaction_id == Transaction.id)
        .order_by(Transaction.date, Posting.id)
    ).all()
    accounts = _accounts(session)
    running: dict[tuple[int, str], Decimal] = defaultdict(Decimal)
    out: dict[date, list[Position]] = {}
    i = 0
    for d in sorted(set(dates)):
        while i < len(rows) and rows[i][0] <= d:
            _, account_id, code, amount = rows[i]
            running[(account_id, code)] += amount
            i += 1
        out[d] = sorted(
            (
                to_position(accounts[a], code, qty)
                for (a, code), qty in running.items()
                if qty != 0 and _keep(accounts[a], types, owner)
            ),
            key=_sort_key,
        )
    return out


def commodity_totals(session: Session) -> dict[str, Decimal]:
    """Sum of all postings per commodity. Always zero in a consistent ledger."""
    totals: dict[str, Decimal] = defaultdict(Decimal)
    for code, amount in session.execute(select(Posting.commodity_code, Posting.amount)):
        totals[code] += amount
    return dict(totals)


def transaction_date_range(session: Session) -> tuple[date | None, date | None]:
    return session.execute(select(func.min(Transaction.date), func.max(Transaction.date))).one()


def account_balance(session: Session, account: Account, at: date) -> dict[str, Decimal]:
    stmt = (
        select(Posting.commodity_code, Posting.amount)
        .join(Transaction, Posting.transaction_id == Transaction.id)
        .where(Posting.account_id == account.id, Transaction.date <= at)
    )
    totals: dict[str, Decimal] = defaultdict(Decimal)
    for code, amount in session.execute(stmt):
        totals[code] += amount
    return {c: v for c, v in totals.items() if v != 0}


__all__ = [
    "SHARED_OWNER",
    "account_balance",
    "commodity_totals",
    "positions_at",
    "positions_over_time",
    "to_position",
    "transaction_date_range",
]
