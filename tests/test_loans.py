"""Loans: payment plans, the annual cost rate, payments in the ledger, statement lines."""

from datetime import date
from decimal import Decimal

import pytest

from financeproject.core.loans import (
    AVALANCHE,
    EQUAL_PRINCIPAL,
    LEAST,
    SNOWBALL,
    Debt,
    LoanTerms,
    annual_cost_rate,
    cash_or_installments,
    effective_annual,
    implied_monthly_rate,
    months_to_clear,
    offer_cost_rate,
    payoff,
    schedule,
    year_fraction,
)
from financeproject.errors import LedgerError
from financeproject.ingest.banks import read_statement
from financeproject.ingest.importer import import_statement
from financeproject.ledger.accounts import create_account, find_account
from financeproject.ledger.loans import (
    add_loan,
    delete_loan,
    get_loan,
    loan_by_reference,
    loan_state,
    owed,
    plan,
    record_payment,
)
from financeproject.ledger.models import Loan, Transaction
from financeproject.ledger.queries import account_balance, commodity_totals
from financeproject.ledger.transactions import opening_balance

D = Decimal

# The worked example of İş Bankası's note on the annual cost rate ("Yıllık maliyet
# oranı hesaplanması"): 10,000 TRY for 12 months at 1 % a month with a 50 TRY file fee.
ISBANK = LoanTerms(D(10000), D("0.01"), 12, date(2026, 2, 15))


def test_payment_plan_of_the_bank_example():
    plan_ = schedule(ISBANK)
    assert [i.payment for i in plan_[:11]] == [D("888.49")] * 11 and plan_[-1].payment == D("888.47")
    assert plan_[0].interest == D("100.00") and plan_[0].principal == D("788.49")
    assert sum(i.interest for i in plan_) == D("661.86")
    assert sum(i.principal for i in plan_) == D(10000) and plan_[-1].balance == 0
    assert [i.due for i in plan_[:3]] == [date(2026, 2, 15), date(2026, 3, 15), date(2026, 4, 15)]


def test_annual_cost_rate_of_the_bank_example():
    payments = [i.payment for i in schedule(ISBANK)]
    x = offer_cost_rate(D(10000), payments, D(50))
    assert x == pytest.approx(0.137504, abs=5e-6)  # the bank's note: 13.7504 %, published as 13.75 %
    assert round(x * 100, 2) == 13.75
    # with the dates: whole months are twelfths of a year, so the result is the same
    dated = annual_cost_rate([(date(2026, 1, 15), D(10000))], [(date(2026, 1, 15), D(50))] + [(i.due, i.payment) for i in schedule(ISBANK)])
    assert dated == pytest.approx(x, abs=1e-9)
    # without the fee it is the monthly rate compounded
    assert offer_cost_rate(D(10000), payments) == pytest.approx(effective_annual(0.01), abs=5e-5)


def test_year_fraction_counts_whole_months_then_days():
    assert year_fraction(date(2026, 1, 15), date(2026, 4, 15)) == pytest.approx(3 / 12)
    assert year_fraction(date(2026, 1, 31), date(2026, 3, 31)) == pytest.approx(2 / 12)
    assert year_fraction(date(2026, 1, 1), date(2026, 3, 15)) == pytest.approx(2 / 12 + 14 / 365)
    # the days are divided by the length of the year that ends on the last of them
    assert year_fraction(date(2028, 2, 1), date(2028, 3, 10)) == pytest.approx(1 / 12 + 9 / 365)  # 2027-02-10 to 2028-02-10
    assert year_fraction(date(2028, 3, 1), date(2028, 4, 10)) == pytest.approx(1 / 12 + 9 / 366)  # that year holds 29 February
    assert year_fraction(date(2026, 3, 1), date(2026, 1, 1)) == -year_fraction(date(2026, 1, 1), date(2026, 3, 1))


CONSUMER = LoanTerms(D(100000), D("0.0349"), 24, date(2026, 5, 13), kkdf=D("0.15"), bsmv=D("0.15"))


def test_consumer_loan_with_kkdf_and_bsmv():
    plan_ = schedule(CONSUMER)
    first = plan_[0]
    assert CONSUMER.gross_rate == D("0.045370")
    assert first.payment == D("6924.20")
    assert (first.principal, first.interest, first.kkdf, first.bsmv) == (D("2387.20"), D("3490.00"), D("523.50"), D("523.50"))
    assert all(i.payment == D("6924.20") for i in plan_[:-1]) and plan_[-1].balance == 0
    assert sum(i.principal for i in plan_) == CONSUMER.principal
    assert all(i.payment == i.principal + i.interest + i.kkdf + i.bsmv for i in plan_)
    # the annual cost rate of a loan without fees is its gross monthly rate compounded
    x = annual_cost_rate([(date(2026, 4, 13), CONSUMER.principal)], [(i.due, i.payment) for i in plan_])
    assert x == pytest.approx(effective_annual(0.04537), abs=1e-4)


def test_the_rate_is_found_from_the_installment():
    assert implied_monthly_rate(D(10000), D("888.49"), 12) == D("0.010000")
    assert implied_monthly_rate(D(100000), D("6924.20"), 24, D("1.30")) == D("0.034900")
    assert implied_monthly_rate(D(1200), D(100), 12) == 0
    with pytest.raises(ValueError, match="less than the principal"):
        implied_monthly_rate(D(1200), D(90), 12)


def test_equal_principal_and_interest_free_plans():
    plan_ = schedule(LoanTerms(D(12000), D("0.02"), 12, date(2026, 1, 1), method=EQUAL_PRINCIPAL))
    assert all(i.principal == D(1000) for i in plan_)
    assert plan_[0].payment == D(1240) and plan_[-1].payment == D(1020)
    assert [i.payment for i in plan_] == sorted((i.payment for i in plan_), reverse=True)
    free = schedule(LoanTerms(D(1000), D(0), 3, date(2026, 1, 1)))
    assert [i.payment for i in free] == [D("333.33"), D("333.33"), D("333.34")]
    with pytest.raises(ValueError):
        LoanTerms(D(1000), D("0.01"), 0, date(2026, 1, 1))
    with pytest.raises(ValueError):
        LoanTerms(D(1000), D("0.01"), 3, date(2026, 1, 1), method="balloon")


def test_cash_or_installments():
    free = cash_or_installments(D(36000), D(6000), 6, 0.02)
    assert free.implied_rate == pytest.approx(0, abs=1e-9)
    assert free.present_value == pytest.approx(33608.59, abs=0.01) and free.installments_win
    discount = cash_or_installments(D(34000), D(6000), 6, 0.02)  # 2,000 off for cash
    assert discount.implied_rate == pytest.approx(0.01658, abs=1e-5)
    assert discount.installments_win  # the money earns more (2 %) than the plan charges (1.66 %)
    assert not cash_or_installments(D(34000), D(6000), 6, 0.01).installments_win
    now = cash_or_installments(D(36000), D(6000), 6, 0.02, first_in=0)
    assert now.present_value > free.present_value
    with pytest.raises(ValueError):
        cash_or_installments(D(0), D(6000), 6, 0.02)


def test_avalanche_pays_less_interest_than_snowball():
    debts = [Debt("loan", D(40000), D("0.0454"), D(5000)), Debt("friend", D(10000), D(0), D(0))]
    avalanche = payoff(debts, D(9000), AVALANCHE)
    snowball = payoff(debts, D(9000), SNOWBALL)
    assert avalanche.order == ["loan", "friend"] and snowball.order == ["friend", "loan"]
    assert avalanche.interest < snowball.interest
    assert avalanche.feasible and snowball.feasible and avalanche.months == snowball.months == 7
    # paying only the least amounts never clears a debt without a least payment
    least = payoff(debts, D(9000), LEAST)
    assert not least.feasible and least.paid_off == {"loan": 11}
    assert months_to_clear(D(40000), D("0.0454"), D(5000)) == 11
    assert not payoff(debts, D(4000), AVALANCHE).feasible  # the budget does not cover the least payments
    assert months_to_clear(D(40000), D("0.05"), D(2000)) is None
    with pytest.raises(ValueError):
        payoff(debts, D(9000), "random")


# --------------------------------------------------------------------------- in the ledger


@pytest.fixture
def bank(session):
    create_account(session, "Assets:Bank:Checking", kind="checking")
    create_account(session, "Liabilities:Loans:Car", kind="consumer_loan")
    create_account(session, "Expenses:Interest and Fees")
    opening_balance(session, "Checking", D(50000), date(2026, 1, 1))
    return session


def test_new_loan_paid_as_planned(bank):
    s = bank
    loan = add_loan(
        s, "Car", "Liabilities:Loans:Car", principal=D(10000), monthly_rate=D("0.01"), count=12,
        first_due=date(2026, 2, 15), fees=D(50), disburse_to="Checking", kkdf=D(0), bsmv=D(0),
    )
    assert loan.disbursed == date(2026, 1, 15) and loan.start_number == 1
    checking = find_account(s, "Checking")
    assert account_balance(s, checking, date(2026, 1, 15)) == {"TRY": D(59950)}  # paid out less the fee
    for k in range(12):
        tx = record_payment(s, loan, date(2026, 2 + k, 15) if k < 11 else date(2027, 1, 15), "Checking")
        assert tx.loan_payment.number == k + 1
    assert owed(s, loan, date(2027, 1, 15)) == 0
    assert account_balance(s, find_account(s, "Interest and Fees"), date(2027, 1, 15)) == {"TRY": D("711.86")}
    state = loan_state(s, loan, date(2027, 1, 15))
    assert state.paid == 12 and state.remaining == () and state.next is None
    with pytest.raises(LedgerError, match="early repayment"):
        record_payment(s, loan, date(2027, 2, 15), "Checking")
    assert all(v == 0 for v in commodity_totals(s).values())


def test_loan_from_before_the_ledger(bank):
    s = bank
    loan = add_loan(
        s, "Car", "Car", principal=CONSUMER.principal, monthly_rate=CONSUMER.monthly_rate, count=24,
        first_due=CONSUMER.first_due, opening_on=date(2026, 9, 1), reference="12345678-0001",
    )
    assert (loan.kkdf, loan.bsmv) == (D("0.15"), D("0.15"))  # the defaults of a consumer loan
    assert loan.start_number == 5  # four installments were due by 1 September
    assert owed(s, loan, date(2026, 9, 1)) == plan(loan)[3].balance == D("89781.47")
    with pytest.raises(LedgerError, match="before the ledger"):
        record_payment(s, loan, date(2026, 9, 13), "Checking", number=4)
    tx = record_payment(s, loan, date(2026, 9, 13), "Checking")
    split = {p.account.path: p.amount for p in tx.postings}
    assert split == {
        "Assets:Bank:Checking": D("-6924.20"),
        "Liabilities:Loans:Car": D("2850.81"),
        "Expenses:Interest and Fees": D("4073.39"),  # interest 3,133.37 + KKDF 470.01 + BSMV 470.01
    }
    with pytest.raises(LedgerError, match="already paid"):
        record_payment(s, loan, date(2026, 9, 14), "Checking", number=5)
    state = loan_state(s, loan, date(2026, 9, 30))
    assert state.paid == 5 and state.next.number == 6 and not state.replanned
    assert state.still_to_pay == sum(i.payment for i in plan(loan)[5:])
    assert loan_by_reference(s, " 12345678 0001 ") is loan and loan_by_reference(s, "12345678-0002") is None


def test_taxes_can_have_their_own_account(bank):
    s = bank
    create_account(s, "Expenses:Taxes on Interest")
    loan = add_loan(
        s, "Car", "Car", principal=D(100000), monthly_rate=D("0.0349"), count=24, first_due=date(2026, 2, 13),
        disburse_to="Checking", tax_account="Expenses:Taxes on Interest",
    )
    tx = record_payment(s, loan, date(2026, 2, 13), "Checking")
    split = {p.account.path: p.amount for p in tx.postings}
    assert split["Expenses:Interest and Fees"] == D("3490.00") and split["Expenses:Taxes on Interest"] == D("1047.00")


def test_early_repayment_and_a_new_plan(bank):
    s = bank
    loan = add_loan(
        s, "Car", "Car", principal=D(12000), monthly_rate=D("0.02"), count=12, first_due=date(2026, 2, 1),
        disburse_to="Checking", kkdf=D(0), bsmv=D(0),
    )
    record_payment(s, loan, date(2026, 2, 1), "Checking")
    tx = record_payment(s, loan, date(2026, 2, 10), "Checking", D(3000), extra=True)
    assert tx.loan_payment.number is None
    assert {p.account.path: p.amount for p in tx.postings}["Liabilities:Loans:Car"] == D(3000)  # all principal
    state = loan_state(s, loan, date(2026, 2, 10))
    assert state.replanned and len(state.remaining) == 11 and state.next.number == 2
    assert state.next.payment < plan(loan)[1].payment
    assert sum(i.principal for i in state.remaining) == state.owed
    with pytest.raises(LedgerError, match="more than"):
        record_payment(s, loan, date(2026, 2, 11), "Checking", D(100000), extra=True)


def test_an_installment_collected_in_parts_pays_one_month_of_interest(bank):
    s = bank
    loan = add_loan(
        s, "Car", "Car", principal=D(10000), monthly_rate=D("0.02"), count=10, first_due=date(2026, 2, 1),
        disburse_to="Checking", kkdf=D("0.15"), bsmv=D("0.15"),
    )
    due = plan(loan)[0]
    first = record_payment(s, loan, date(2026, 2, 1), "Checking", D(200), number=1, partial=True)
    second = record_payment(s, loan, date(2026, 2, 3), "Checking", due.payment - D(200), number=1, partial=True)
    cost = sum(p.amount for tx in (first, second) for p in tx.postings if p.account.path == "Expenses:Interest and Fees")
    principal = sum(p.amount for tx in (first, second) for p in tx.postings if p.account.path == "Liabilities:Loans:Car")
    assert cost == due.cost and principal == due.principal
    assert {p.account.path for p in first.postings} == {"Assets:Bank:Checking", "Expenses:Interest and Fees"}  # interest first


def test_overpayments_count_as_interest_only_when_allowed(bank):
    s = bank
    loan = add_loan(
        s, "Car", "Car", principal=D(1000), monthly_rate=D("0.02"), count=2, first_due=date(2026, 2, 1),
        disburse_to="Checking", kkdf=D(0), bsmv=D(0),
    )
    record_payment(s, loan, date(2026, 2, 1), "Checking")
    with pytest.raises(LedgerError, match="more than"):
        record_payment(s, loan, date(2026, 3, 1), "Checking", D(600))
    tx = record_payment(s, loan, date(2026, 3, 1), "Checking", D(600), partial=True)  # e.g. late interest
    assert owed(s, loan, date(2026, 3, 1)) == 0
    assert sum(p.amount for p in tx.postings if p.account.path == "Expenses:Interest and Fees") > plan(loan)[1].interest


def test_loan_rules(bank):
    s = bank
    create_account(s, "Liabilities:Credit Card:Bonus", kind="credit_card")
    terms = {"principal": D(1000), "monthly_rate": D("0.01"), "count": 2, "first_due": date(2026, 2, 1)}
    with pytest.raises(LedgerError, match="liability account of kind"):
        add_loan(s, "Card", "Bonus", **terms)
    with pytest.raises(LedgerError, match="either"):
        add_loan(s, "Car", "Car", **terms, disburse_to="Checking", opening_on=date(2026, 1, 1))
    with pytest.raises(LedgerError, match="before the loan is paid out"):
        add_loan(s, "Car", "Car", **terms, disbursed=date(2026, 3, 1))
    loan = add_loan(s, "Car", "Car", **terms, reference="77-1")
    with pytest.raises(LedgerError, match="already has a loan"):
        add_loan(s, "Other", "Car", **terms)
    create_account(s, "Liabilities:Loans:Home", kind="mortgage")
    with pytest.raises(LedgerError, match="already a loan called"):
        add_loan(s, "car", "Home", **terms)
    with pytest.raises(LedgerError, match="reference"):
        add_loan(s, "Home", "Home", **terms, reference="77 1")
    assert get_loan(s, "CAR") is loan and get_loan(s, "Loans:Car") is loan
    with pytest.raises(LedgerError, match="No loan named"):
        get_loan(s, "boat")
    with pytest.raises(LedgerError, match="owed nothing"):  # no payout or opening balance was booked
        record_payment(s, loan, date(2026, 2, 1), "Checking")
    delete_loan(s, loan)
    assert s.query(Loan).count() == 0 and s.query(Transaction).count() == 1  # the opening balance of Checking stays


def test_a_loan_on_an_account_with_history_starts_after_it(bank):
    s = bank
    opening_balance(s, "Car", D(5100), date(2026, 3, 20))
    loan = add_loan(s, "Car", "Car", principal=D(10000), monthly_rate=D("0.01"), count=12, first_due=date(2026, 1, 15))
    assert loan.start_number == 4  # the installments of January, February and March came before the first transaction
    create_account(s, "Liabilities:Loans:Van", kind="vehicle_loan")
    opening_balance(s, "Van", D(100), date(2026, 1, 1))
    with pytest.raises(LedgerError, match="already has transactions"):
        add_loan(s, "Van", "Van", principal=D(1000), monthly_rate=D(0), count=1, first_due=date(2026, 2, 1), disburse_to="Checking")


# --------------------------------------------------------------------------- statements


def test_statement_installment_lines_pay_the_loan(bank, fixtures):
    s = bank
    create_account(s, "Assets:Bank:Ziraat Checking", kind="checking")
    loan = add_loan(
        s, "Car", "Car", principal=CONSUMER.principal, monthly_rate=CONSUMER.monthly_rate, count=24,
        first_due=CONSUMER.first_due, opening_on=date(2026, 9, 1), reference="12345678-0001",
    )
    path = fixtures / "ziraat_statement.xlsx"
    preview = import_statement(s, read_statement(path), path, "Ziraat Checking", add_opening=True, dry_run=True)
    row = next(r for r in preview.rows if r.how.startswith("loan"))
    assert row.how == "loan Car #5" and row.counter_account == "Liabilities:Loans:Car" and not preview.unknown_loans
    result = import_statement(s, read_statement(path), path, "Ziraat Checking", add_opening=True)
    assert result.written and result.loan_payments == 1 and result.balance_check.ok
    assert owed(s, loan, date(2026, 9, 30)) == plan(loan)[4].balance  # paid exactly as planned
    payment = next(p for p in loan.payments if p.number == 5)
    assert payment.transaction.payee == "Loan 12345678-0001" and payment.transaction.import_key
    html = fixtures / "ziraat_statement.html"
    again = import_statement(s, read_statement(html), html, "Ziraat Checking")
    assert again.added == 0 and again.duplicates == 16


def test_installment_lines_before_the_loan_starts_are_refused(bank, fixtures):
    s = bank
    create_account(s, "Assets:Bank:Ziraat Checking", kind="checking")
    add_loan(
        s, "Car", "Car", principal=CONSUMER.principal, monthly_rate=CONSUMER.monthly_rate, count=24,
        first_due=CONSUMER.first_due, opening_on=date(2026, 9, 20), reference="12345678-0001",
    )
    path = fixtures / "ziraat_statement.xlsx"
    result = import_statement(s, read_statement(path), path, "Ziraat Checking", add_opening=True)
    assert not result.written and "counts as paid before the ledger began" in result.errors[0]
