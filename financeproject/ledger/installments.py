"""Installment purchases (Turkish "taksit") and their monthly schedule.

Turkish credit cards let a purchase be paid in equal monthly installments,
usually without interest. In the ledger the whole purchase is an expense and a
debt on the day it is made, because from that day on all of it is owed
(accrual accounting). The installment plan records when the parts fall due,
which gives:

- the installment load of the coming months, and
- the card balance split into installments not billed yet and the rest.

The amount is split into equal parts rounded down to the kuruş, and the
remainder goes to the first installment: 1,000 in 3 is 333.34 + 333.33 + 333.33.
The first installment is due in the month after the purchase unless another
month is given (it depends on the card's statement day).
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_DOWN, Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from financeproject.core.dates import add_months, month_start
from financeproject.core.kinds import LIABILITY
from financeproject.core.money import quantum
from financeproject.errors import LedgerError
from financeproject.ledger.accounts import find_account
from financeproject.ledger.models import Account, InstallmentPlan, Transaction
from financeproject.ledger.transactions import record_expense

MIN_INSTALLMENTS, MAX_INSTALLMENTS = 2, 36


def split_amount(total: Decimal, count: int, decimals: int = 2) -> list[Decimal]:
    """Equal parts rounded down; the remainder goes to the first part."""
    if count < 1:
        raise LedgerError("The number of installments must be positive.")
    part = (total / count).quantize(quantum(decimals), rounding=ROUND_DOWN)
    first = total - part * (count - 1)
    return [first] + [part] * (count - 1)


@dataclass(frozen=True, slots=True)
class Installment:
    plan_id: int
    number: int
    count: int
    month: date
    """First day of the month the installment is due in."""
    amount: Decimal
    account: str
    description: str
    purchased_on: date


def schedule(plan: InstallmentPlan) -> list[Installment]:
    amounts = split_amount(plan.total, plan.count)
    return [
        Installment(
            plan.id,
            k + 1,
            plan.count,
            add_months(plan.first_due, k),
            amount,
            plan.account.path,
            plan.transaction.description,
            plan.transaction.date,
        )
        for k, amount in enumerate(amounts)
    ]


def record_installment_purchase(
    session: Session,
    amount: Decimal,
    expense_account: str | Account,
    card: str | Account,
    on: date,
    description: str,
    count: int,
    first_due: date | None = None,
    payee: str | None = None,
) -> Transaction:
    """Book the purchase in full on the card and store its installment plan."""
    if not MIN_INSTALLMENTS <= count <= MAX_INSTALLMENTS:
        raise LedgerError(f"The number of installments must be between {MIN_INSTALLMENTS} and {MAX_INSTALLMENTS}.")
    account = find_account(session, card)
    if account.type != LIABILITY or account.kind != "credit_card":
        raise LedgerError(f"Installments are bought with a credit card account: {account.path}")
    due = month_start(first_due) if first_due else add_months(month_start(on), 1)
    if due < month_start(on):
        raise LedgerError("The first installment cannot fall due before the month of the purchase.")
    tx = record_expense(session, amount, expense_account, account, on, description, payee)
    tx.installment_plan = InstallmentPlan(account=account, count=count, first_due=due, total=amount)
    session.flush()
    return tx


def plans(session: Session) -> list[InstallmentPlan]:
    return list(
        session.scalars(
            select(InstallmentPlan)
            .options(selectinload(InstallmentPlan.transaction), selectinload(InstallmentPlan.account))
            .join(Transaction)
            .order_by(Transaction.date, InstallmentPlan.id)
        )
    )


def installments(session: Session) -> list[Installment]:
    """Every installment of every plan, by month."""
    out = [i for plan in plans(session) for i in schedule(plan)]
    return sorted(out, key=lambda i: (i.month, i.purchased_on, i.plan_id, i.number))


def due_between(session: Session, first_month: date, last_month: date) -> list[Installment]:
    """Installments due in the months from ``first_month`` to ``last_month`` inclusive."""
    a, b = month_start(first_month), month_start(last_month)
    return [i for i in installments(session) if a <= i.month <= b]


def not_billed(session: Session, month: date, on: date | None = None) -> dict[str, Decimal]:
    """Per card, the installments due after ``month`` of purchases made by ``on``."""
    out: dict[str, Decimal] = defaultdict(Decimal)
    for i in installments(session):
        if i.month > month_start(month) and (on is None or i.purchased_on <= on):
            out[i.account] += i.amount
    return dict(out)
