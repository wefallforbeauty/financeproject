"""Installment purchases: splitting, schedules, validation, deletion."""

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from financeproject.errors import LedgerError
from financeproject.ledger.accounts import create_account
from financeproject.ledger.installments import (
    due_between,
    installments,
    not_billed,
    plans,
    record_installment_purchase,
    schedule,
    split_amount,
)
from financeproject.ledger.models import InstallmentPlan
from financeproject.ledger.queries import account_balance
from financeproject.ledger.transactions import delete_transaction


def test_split_amount_puts_the_remainder_first():
    assert split_amount(Decimal("1000"), 3) == [Decimal("333.34"), Decimal("333.33"), Decimal("333.33")]
    assert split_amount(Decimal("900"), 3) == [Decimal("300.00")] * 3
    assert sum(split_amount(Decimal("36931.62"), 6)) == Decimal("36931.62")
    with pytest.raises(LedgerError):
        split_amount(Decimal("1"), 0)


@pytest.fixture
def card(session):
    create_account(session, "Liabilities:Credit Card:Bonus", kind="credit_card")
    create_account(session, "Assets:Bank:Checking", kind="checking")
    create_account(session, "Expenses:Electronics")
    return session


def test_purchase_is_a_full_debt_with_a_schedule(card):
    s = card
    tx = record_installment_purchase(s, Decimal("1000"), "Electronics", "Bonus", date(2026, 10, 20), "Phone", 3)
    plan = tx.installment_plan
    assert (plan.count, plan.first_due, plan.total) == (3, date(2026, 11, 1), Decimal("1000"))
    owed = account_balance(s, plan.account, date(2026, 10, 20))
    assert owed == {"TRY": Decimal("-1000")}  # all of it is owed from the start
    parts = schedule(plan)
    assert [(i.month, i.amount, i.number) for i in parts] == [
        (date(2026, 11, 1), Decimal("333.34"), 1),
        (date(2026, 12, 1), Decimal("333.33"), 2),
        (date(2027, 1, 1), Decimal("333.33"), 3),
    ]
    record_installment_purchase(s, Decimal("600"), "Electronics", "Bonus", date(2026, 10, 25), "Headphones", 2, date(2026, 10, 1))
    assert [i.amount for i in due_between(s, date(2026, 10, 1), date(2026, 11, 30))] == [
        Decimal("300.00"), Decimal("333.34"), Decimal("300.00"),
    ]
    assert not_billed(s, date(2026, 11, 1)) == {"Liabilities:Credit Card:Bonus": Decimal("666.66")}
    assert not_billed(s, date(2026, 11, 1), on=date(2026, 10, 21)) == {"Liabilities:Credit Card:Bonus": Decimal("666.66")}
    assert not_billed(s, date(2026, 10, 1), on=date(2026, 10, 21)) == {"Liabilities:Credit Card:Bonus": Decimal("1000")}
    assert len(installments(s)) == 5 and len(plans(s)) == 2


def test_validation(card):
    s = card
    with pytest.raises(LedgerError, match="between 2 and 36"):
        record_installment_purchase(s, Decimal("100"), "Electronics", "Bonus", date(2026, 10, 1), "x", 1)
    with pytest.raises(LedgerError, match="credit card"):
        record_installment_purchase(s, Decimal("100"), "Electronics", "Checking", date(2026, 10, 1), "x", 3)
    with pytest.raises(LedgerError, match="before the month"):
        record_installment_purchase(s, Decimal("100"), "Electronics", "Bonus", date(2026, 10, 1), "x", 3, date(2026, 9, 1))


def test_deleting_the_purchase_deletes_its_plan(card):
    s = card
    tx = record_installment_purchase(s, Decimal("1000"), "Electronics", "Bonus", date(2026, 10, 20), "Phone", 3)
    delete_transaction(s, tx.id)
    assert s.scalar(select(func.count()).select_from(InstallmentPlan)) == 0
