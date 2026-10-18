"""Debt reports and the 02 commands: loans, the annual cost rate, cash or installments, payoff."""

from datetime import date
from decimal import Decimal

import pytest
from typer.testing import CliRunner

from financeproject.cli import app
from financeproject.core.loans import Debt
from financeproject.errors import FinanceError
from financeproject.ledger.accounts import create_account
from financeproject.ledger.installments import record_installment_purchase
from financeproject.ledger.loans import add_loan, plan, record_payment
from financeproject.ledger.models import Loan
from financeproject.ledger.transactions import opening_balance
from financeproject.prices.store import upsert_index
from financeproject.reports.debt import debt_report, payoff_debts, trailing_inflation
from financeproject.reports.recurring import upcoming

D = Decimal


@pytest.fixture
def household(session):
    create_account(session, "Assets:Bank:Checking", kind="checking")
    create_account(session, "Liabilities:Loans:Car", kind="consumer_loan")
    create_account(session, "Liabilities:Credit Card:Bonus", kind="credit_card")
    create_account(session, "Liabilities:Personal:Friend", kind="personal_debt")
    create_account(session, "Expenses:Home")
    opening_balance(session, "Checking", D(100000), date(2026, 1, 1))
    opening_balance(session, "Friend", D(5000), date(2026, 1, 1))
    loan = add_loan(
        session, "Car", "Car", principal=D(100000), monthly_rate=D("0.0349"), count=24,
        first_due=date(2026, 2, 13), disburse_to="Checking",
    )
    record_payment(session, loan, date(2026, 2, 13), "Checking")
    record_installment_purchase(session, D(9000), "Home", "Bonus", date(2026, 2, 20), "Sofa", 3)
    return session


def test_debt_report(household):
    s = household
    report = debt_report(s, date(2026, 2, 28), inflation=0.30)
    by_account = {line.account: line for line in report.lines}
    assert set(by_account) == {"Liabilities:Loans:Car", "Liabilities:Credit Card:Bonus", "Liabilities:Personal:Friend"}
    car = by_account["Liabilities:Loans:Car"]
    assert car.owed == plan(car.state.loan)[0].balance and car.state.next.number == 2
    assert car.effective_rate == pytest.approx(1.04537**12 - 1)
    assert car.real_rate == pytest.approx(1.04537**12 / 1.30 - 1)
    # 23 installments discounted at 30 % a year are worth less than their sum
    assert car.today_value < car.state.still_to_pay
    # installment k falls due on 13 (k+1)/2026: k − 2 whole months and 13 days after 28 February
    assert float(car.today_value) == pytest.approx(
        sum(float(i.payment) * 1.30 ** -((i.number - 2) / 12 + 13 / 365) for i in car.state.remaining), abs=0.05
    )
    assert by_account["Liabilities:Credit Card:Bonus"].later_installments == D(9000)  # March, April and May
    assert report.owed == car.owed + D(9000) + D(5000)
    assert report.inflation.source == "given"


def test_trailing_inflation_comes_from_the_cpi(session):
    assert trailing_inflation(session) is None
    for month, level in ((date(2025, 9, 1), 100.0), (date(2026, 8, 1), 128.0), (date(2026, 9, 1), 130.0)):
        upsert_index(session, "CPI", month, D(str(level)), "test")
    expected = trailing_inflation(session)
    assert expected.rate == pytest.approx(0.30) and expected.source == "CPI, 12 months to 2026-09"


def test_payoff_debts(household):
    s = household
    debts = payoff_debts(s, date(2026, 2, 28), {"Bonus": D("0.04")}, {"Friend": D(500)})
    by_name = {d.name: d for d in debts}
    assert set(by_name) == {"Car", "Bonus", "Friend"}
    loan = s.query(Loan).one()
    assert by_name["Car"] == Debt("Car", plan(loan)[0].balance, D("0.045370"), D("6924.20"))
    assert by_name["Bonus"].monthly_rate == D("0.04") and by_name["Bonus"].minimum == 0
    assert by_name["Friend"].monthly_rate == 0 and by_name["Friend"].minimum == D(500)
    with pytest.raises(FinanceError, match="is a loan"):
        payoff_debts(s, date(2026, 2, 28), {"Car": D("0.01")}, {})
    with pytest.raises(FinanceError, match="not a debt"):
        payoff_debts(s, date(2026, 2, 28), {"Checking": D("0.01")}, {})


def test_upcoming_lists_loan_installments(household):
    items = upcoming(household, date(2026, 3, 20), days=30, series=[])
    loans = [u for u in items if u.kind == "loan"]
    assert [(u.on, u.note) for u in loans] == [(date(2026, 3, 13), "overdue"), (date(2026, 4, 13), "3 of 24")]
    assert all(u.amount == D("6924.20") and u.direction == -1 for u in loans)
    # the parts, for an interface that words them itself
    assert [(u.loan, u.number, u.count, u.late) for u in loans] == [("Car", 2, 24, True), ("Car", 3, 24, False)]


# --------------------------------------------------------------------------- commands

runner = CliRunner(env={"COLUMNS": "200"})


def run(db, *args):
    return runner.invoke(app, ["-d", db, *args])


def ok(db, *args):
    result = run(db, *args)
    assert result.exit_code == 0, result.output
    return result.output


@pytest.fixture
def db(tmp_path):
    path = str(tmp_path / "ledger.sqlite")
    ok(path, "init")
    ok(path, "account", "add", "Assets:Bank:Checking", "--kind", "checking")
    ok(path, "opening", "Checking", "50000", "--date", "2026-01-01")
    return path


def test_loan_commands(db, tmp_path):
    out = ok(
        db, "loan", "add", "Car loan", "Liabilities:Loans:Car", "--principal", "100000", "--rate", "3.49", "--count", "24",
        "--first-due", "2026-02-13", "--paid-into", "Checking", "--reference", "12345678-0001",
    )
    assert "Account created: Liabilities:Loans:Car" in out and "6,924.20" in out and "70.31 %" in out
    assert "KKDF 15 % and BSMV 15 %" in out
    out = ok(db, "loan", "pay", "car loan", "--from", "Checking", "--date", "2026-02-13")
    assert "installment 1: 6,924.20 = principal 2,387.20 + interest and taxes 4,537.00" in out
    out = ok(db, "loan", "list", "--date", "2026-02-28")
    assert "1/24" in out and "2026-03-13" in out and "97,612.80" in out
    out = ok(db, "loan", "schedule", "Car", "--csv", str(tmp_path / "plan.csv"))
    assert "paid 2026-02-13" in out and "166,180.72" in out
    assert (tmp_path / "plan.csv").read_text(encoding="utf-8").count("\n") == 25
    out = ok(db, "loan", "pay", "Car", "--from", "Checking", "--amount", "10000", "--extra", "--date", "2026-03-01")
    assert "early repayment: 10,000.00 = principal 10,000.00" in out
    assert "*" in ok(db, "loan", "list", "--date", "2026-03-01")  # replanned from the balance
    result = run(db, "loan", "pay", "Car", "--from", "Checking", "--number", "1")
    assert result.exit_code == 1 and "already paid" in result.output
    out = ok(db, "upcoming", "--date", "2026-03-01", "--days", "20")
    assert "Car loan: installment 2" in out
    assert "Loan terms deleted" in ok(db, "loan", "delete", "Car loan")
    assert "No loans yet" in ok(db, "loan", "list")


def test_loan_add_options(db):
    out = ok(
        db, "loan", "add", "Old loan", "Liabilities:Loans:Old", "--principal", "100000", "--installment", "6924.20",
        "--count", "24", "--first-due", "2026-05-13", "--opening-on", "2026-09-01",
    )
    assert "3.490 % a month (worked out from the installment)" in out and "Installments 1–4 count as paid" in out
    result = run(db, "loan", "add", "X", "Liabilities:Loans:X", "--principal", "1", "--count", "1", "--first-due", "2026-05-13")
    assert result.exit_code == 1 and "--rate" in result.output
    result = run(db, "loan", "add", "X", "Nowhere", "--principal", "1", "--rate", "1", "--count", "1", "--first-due", "2026-05-13")
    assert result.exit_code == 1 and "full path" in result.output
    out = ok(
        db, "loan", "add", "Family", "Liabilities:Loans:Family", "--kind", "personal_debt", "--principal", "12000",
        "--rate", "0", "--count", "12", "--first-due", "2026-02-01", "--paid-into", "Checking",
    )
    assert "12 installments of 1,000.00" in out and "KKDF" not in out


def test_apr_command():
    out = runner.invoke(app, ["apr", "--amount", "10000", "--count", "12", "--installment", "888.49", "--last", "888.47", "--fees", "50"])
    assert out.exit_code == 0, out.output
    assert "13.75 %" in out.output and "10,711.86" in out.output
    out = runner.invoke(app, ["apr", "--amount", "100000", "--count", "24", "--rate", "3.49"])
    assert out.exit_code == 0 and "6,924.20" in out.output and "70.31 %" in out.output
    out = runner.invoke(app, ["apr", "--amount", "100", "--count", "2"])
    assert out.exit_code == 1 and "--installment" in out.output


def test_cash_or_installments_command(db):
    out = ok(db, "cash-or-installments", "--cash", "34000", "--installment", "6000", "--count", "6", "--rate", "2")
    assert "1.66 % a month" in out and "33,608.59" in out and "the installments are cheaper" in out
    out = ok(db, "cash-or-installments", "--cash", "34000", "--installment", "6000", "--count", "6", "--annual-rate", "10")
    assert "cash is cheaper" in out
    out = ok(db, "cash-or-installments", "--cash", "36000", "--installment", "6000", "--count", "6")
    assert "no interest" in out and "inflation, CPI" in out  # the bundled CPI is loaded by init


def test_payoff_and_debt_commands(db):
    ok(
        db, "loan", "add", "Car", "Liabilities:Loans:Car", "--principal", "60000", "--rate", "3.49", "--count", "12",
        "--first-due", "2026-02-13", "--paid-into", "Checking",
    )
    ok(db, "account", "add", "Liabilities:Personal:Friend", "--kind", "personal_debt")
    ok(db, "opening", "Friend", "6000", "--date", "2026-01-01")
    out = ok(db, "payoff", "--budget", "9000", "--minimum", "Friend=1000", "--date", "2026-01-31")
    assert "As now: least payments only" in out and "Avalanche saves" in out
    assert "Car (" in out and "Friend (" in out
    result = run(db, "payoff", "--budget", "100", "--date", "2026-01-31")
    assert result.exit_code == 1 and "does not cover" in result.output
    result = run(db, "payoff", "--budget", "9000", "--rate", "Friend")
    assert result.exit_code == 1 and "ACCOUNT=VALUE" in result.output
    out = ok(db, "debt", "--date", "2026-01-31", "--inflation", "30")
    assert "inflation 30.0 % a year (given)" in out and "Liabilities:Personal:Friend" in out and "In today's money" in out
    assert "Inflation lightens the loans" in out
