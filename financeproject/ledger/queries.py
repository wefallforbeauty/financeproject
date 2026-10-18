"""Balances read from the ledger."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation

from sqlalchemy import Float, cast, func, or_, select
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
from financeproject.ledger.db import UNCATEGORIZED_EXPENSE, UNCATEGORIZED_INCOME
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


UNCATEGORIZED = (UNCATEGORIZED_EXPENSE, UNCATEGORIZED_INCOME)


def uncategorized_count(session: Session) -> int:
    """Transactions with a leg in ``Expenses:Uncategorized`` or ``Income:Uncategorized``."""
    return session.scalar(
        select(func.count(func.distinct(Posting.transaction_id)))
        .join(Account, Posting.account_id == Account.id)
        .where(Account.path.in_(UNCATEGORIZED))
    )


def balances_by_account(session: Session, at: date) -> dict[int, dict[str, Decimal]]:
    """Every account's non-zero balances at the end of day ``at``, in one pass."""
    totals: dict[int, dict[str, Decimal]] = defaultdict(lambda: defaultdict(Decimal))
    rows = session.execute(
        select(Posting.account_id, Posting.commodity_code, Posting.amount)
        .join(Transaction, Posting.transaction_id == Transaction.id)
        .where(Transaction.date <= at)
    )
    for account_id, code, amount in rows:
        totals[account_id][code] += amount
    return {a: {c: v for c, v in by.items() if v != 0} for a, by in totals.items()}


@dataclass(frozen=True)
class TransactionRow:
    """A transaction as a list shows it."""

    id: int
    date: date
    description: str
    payee: str | None
    note: str | None
    source: str
    accounts: tuple[str, ...]
    """Its asset and liability accounts."""
    categories: tuple[str, ...]
    """Its income and expense accounts."""
    amount: Decimal
    """The change of its first asset or liability account (money out negative)."""
    commodity: str
    legs: int
    import_id: int | None


def _row(tx: Transaction) -> TransactionRow:
    sheet = [p for p in tx.postings if p.account.type in BALANCE_SHEET_TYPES]
    categories = [p for p in tx.postings if p.account.type in (INCOME, EXPENSE)]
    main = sheet[0] if sheet else tx.postings[0]
    return TransactionRow(
        tx.id,
        tx.date,
        tx.description,
        tx.payee,
        tx.note,
        tx.source,
        tuple(dict.fromkeys(p.account.path for p in sheet)),
        tuple(dict.fromkeys(p.account.path for p in categories)),
        main.amount,
        main.commodity_code,
        len(tx.postings),
        tx.import_link.import_id if tx.import_link else None,
    )


def find_transactions(
    session: Session,
    *,
    text: str | None = None,
    account_id: int | None = None,
    start: date | None = None,
    end: date | None = None,
    uncategorized: bool = False,
    import_id: int | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[TransactionRow], int]:
    """Transactions newest first, with the total count, filtered by:

    - ``text``: in the description, payee or note, ignoring case and Turkish
      letters; a number also finds the amounts it equals;
    - ``account_id``: a leg in this account (or a category);
    - ``start``, ``end``: the dates, inclusive;
    - ``uncategorized``: a leg in an uncategorized account;
    - ``import_id``: added by this import.
    """
    from financeproject.ledger.models import ImportLink

    stmt = select(Transaction)
    if text and text.strip():
        key = f"%{fold(text.strip())}%"
        match = or_(
            func.fold(Transaction.description).like(key),
            func.fold(Transaction.payee).like(key),
            func.fold(Transaction.note).like(key),
        )
        try:
            number = Decimal(text.strip().replace(",", "."))
        except (InvalidOperation, ValueError):
            number = None
        if number is not None and number.is_finite():
            match = or_(
                match,
                Transaction.postings.any(func.abs(cast(Posting.amount, Float) - float(abs(number))) < 0.005),
                Transaction.postings.any(func.abs(cast(Posting.amount, Float) + float(abs(number))) < 0.005),
            )
        stmt = stmt.where(match)
    if account_id is not None:
        stmt = stmt.where(Transaction.postings.any(Posting.account_id == account_id))
    if start is not None:
        stmt = stmt.where(Transaction.date >= start)
    if end is not None:
        stmt = stmt.where(Transaction.date <= end)
    if uncategorized:
        stmt = stmt.where(Transaction.postings.any(Posting.account.has(Account.path.in_(UNCATEGORIZED))))
    if import_id is not None:
        stmt = stmt.where(Transaction.import_link.has(ImportLink.import_id == import_id))
    total = session.scalar(select(func.count()).select_from(stmt.subquery()))
    txs = session.scalars(
        stmt.options(selectinload(Transaction.postings).selectinload(Posting.account), selectinload(Transaction.import_link))
        .order_by(Transaction.date.desc(), Transaction.id.desc())
        .limit(limit)
        .offset(offset)
    )
    return [_row(tx) for tx in txs], total


__all__ = [
    "SHARED_OWNER",
    "TransactionRow",
    "balances_by_account",
    "find_transactions",
    "uncategorized_count",
    "account_balance",
    "commodity_totals",
    "positions_at",
    "positions_over_time",
    "to_position",
    "transaction_date_range",
]
