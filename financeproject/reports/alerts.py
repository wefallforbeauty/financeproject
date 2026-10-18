"""What needs attention in the ledger, for the start page.

Each alert has a code, a severity and its numbers; the interface words it.

- ``no_accounts``, ``no_transactions``: the ledger is new.
- ``uncategorized``: transactions waiting for a category.
- ``missing_price``: holdings without any price, left out of net worth.
- ``stale_price``: holdings valued with a price older than a week.
- ``bank_difference``: the ledger's balance at the end of the last imported
  statement of an account differs from the bank's (a transaction was deleted
  or changed since, or one is missing).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from financeproject.core.kinds import BALANCE_SHEET_TYPES
from financeproject.core.valuation import NetWorth
from financeproject.ledger.models import Account, ImportBatch, Transaction
from financeproject.ledger.queries import account_balance, uncategorized_count

INFO, WARNING, CRITICAL = "info", "warning", "critical"


@dataclass(frozen=True)
class Alert:
    code: str
    severity: str
    params: dict = field(default_factory=dict)


def bank_differences(session: Session) -> list[tuple[ImportBatch, Decimal]]:
    """For every account, its last import with a bank balance, when the ledger now differs from it."""
    latest: dict[int, ImportBatch] = {}
    batches = session.scalars(
        select(ImportBatch)
        .where(ImportBatch.undone_at.is_(None), ImportBatch.statement_balance.is_not(None), ImportBatch.account_id.is_not(None))
        .order_by(ImportBatch.statement_end, ImportBatch.id)
    )
    for batch in batches:
        latest[batch.account_id] = batch
    out = []
    for batch in latest.values():
        commodity = batch.account.commodity_code or "TRY"
        ledger = account_balance(session, batch.account, batch.statement_end).get(commodity, Decimal(0))
        if ledger != batch.statement_balance:
            out.append((batch, ledger - batch.statement_balance))
    return out


def alerts(session: Session, net_worth: NetWorth, today: date) -> list[Alert]:
    out: list[Alert] = []
    accounts = session.scalar(select(func.count()).select_from(Account).where(Account.type.in_(BALANCE_SHEET_TYPES)))
    if not accounts:
        return [Alert("no_accounts", INFO)]
    if not session.scalar(select(func.count()).select_from(Transaction)):
        return [Alert("no_transactions", INFO)]
    for batch, difference in bank_differences(session):
        out.append(
            Alert(
                "bank_difference",
                CRITICAL,
                {"account": batch.account.path, "on": batch.statement_end, "difference": difference, "file": batch.file_name},
            )
        )
    if count := uncategorized_count(session):
        out.append(Alert("uncategorized", WARNING, {"count": count}))
    if net_worth.missing:
        out.append(Alert("missing_price", WARNING, {"accounts": list(net_worth.missing)}))
    if net_worth.stale:
        out.append(Alert("stale_price", INFO, {"accounts": list(net_worth.stale)}))
    return out
