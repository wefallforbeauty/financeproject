"""Recurring payments found in the ledger, and the payments expected next.

Candidate series are the simple transactions (one balance-sheet and one income
or expense leg) of the same payee, category and direction. The payee is the
transaction's payee, or else its description; digits are dropped, so receipt
and reference numbers do not split a series. The detection rule is in
``core.recurring``.

``upcoming`` lists what is expected in the next days: the next payments of the
active and late series, the card installments falling due (installments are
known by month, not by day; they are listed on the first of their month) and
the installments of loans, unpaid ones from earlier days too. Loan payments
have three or more legs, so they never form a series themselves.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy.orm import Session

from financeproject.core.dates import month_start
from financeproject.core.recurring import LATE, STOPPED, Occurrence, Series, find_series
from financeproject.ingest.learn import simple_transactions, words
from financeproject.ledger.installments import due_between
from financeproject.ledger.loans import all_loans, loan_state


def candidates(session: Session) -> dict[tuple, tuple[str, str, int, str, list[Occurrence]]]:
    groups: dict[tuple, tuple[str, str, int, str, list[Occurrence]]] = {}
    occurrences: dict[tuple, list[Occurrence]] = defaultdict(list)
    for tx, bs, category in simple_transactions(session):
        name = tx.payee or tx.description
        key_words = words(name)
        if not key_words:
            continue
        direction = 1 if bs.amount > 0 else -1
        key = (direction, category.account_id, bs.commodity_code, " ".join(key_words))
        occurrences[key].append(Occurrence(tx.date, abs(bs.amount)))
        groups[key] = (" ".join(name.split()), category.account.path, direction, bs.commodity_code, occurrences[key])
    return groups


def recurring(session: Session, min_regularity: float = 0.75) -> list[Series]:
    return find_series(candidates(session), min_regularity)


@dataclass(frozen=True)
class Upcoming:
    on: date
    label: str
    amount: Decimal
    commodity: str
    direction: int
    kind: str
    """``recurring``, ``installments`` or ``loan``."""
    note: str = ""


def upcoming(session: Session, today: date, days: int = 30, series: list[Series] | None = None) -> list[Upcoming]:
    """Payments expected from ``today`` to ``today + days``, overdue ones first."""
    end = today + timedelta(days=days)
    out: list[Upcoming] = []
    for s in series if series is not None else recurring(session):
        status = s.status(today)
        if status == STOPPED:
            continue
        amount_note = "fixed" if s.fixed else "about"
        if status == LATE:
            out.append(Upcoming(s.next_due, s.label, s.typical, s.commodity, s.direction, "recurring", f"late; {s.period.name}, {amount_note}"))
            dates = s.due_dates(today, end)
        else:
            dates = s.due_dates(today, end)
        for d in dates:
            out.append(Upcoming(d, s.label, s.typical, s.commodity, s.direction, "recurring", f"{s.period.name}, {amount_note}"))
    by_card: dict[tuple[date, str], Decimal] = defaultdict(Decimal)
    for i in due_between(session, month_start(today), month_start(end)):
        by_card[(i.month, i.account)] += i.amount
    for (month, account), amount in sorted(by_card.items()):
        card = account.split(":")[-1]
        out.append(Upcoming(month, f"Card installments ({card})", amount, "TRY", -1, "installments", "due this month"))
    for loan in all_loans(session):
        state = loan_state(session, loan, today)
        unit = loan.account.commodity_code or "TRY"
        for i in state.remaining:
            if i.due > end:
                break
            note = "overdue" if i.due < today else f"{i.number} of {loan.count}"
            out.append(Upcoming(i.due, f"{loan.name}: installment {i.number}", i.payment, unit, -1, "loan", note))
    return sorted(out, key=lambda u: (u.on, -u.amount))
