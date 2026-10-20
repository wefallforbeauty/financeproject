"""Income statement, cash flow statement, household ratios and the Sankey diagram."""

from datetime import date, timedelta
from decimal import Decimal

import pytest

from financeproject.core.recurring import (
    ACTIVE,
    LATE,
    PERIODS,
    STOPPED,
    Occurrence,
    classify,
    find_series,
    next_date,
)
from financeproject.demo import build_demo, synthetic_prices
from financeproject.ledger.accounts import create_account
from financeproject.ledger.installments import record_installment_purchase
from financeproject.ledger.transactions import (
    Leg,
    add_transaction,
    opening_balance,
    record_exchange,
    record_expense,
    record_income,
    record_transfer,
)
from financeproject.prices.store import upsert_price
from financeproject.prices.update import load_bundled_cpi
from financeproject.reports.cashflow import cash_flow
from financeproject.reports.context import load_market_data
from financeproject.reports.income import IncomeStatement, income_statement, truncate
from financeproject.reports.ratios import BELOW, DANGER, OK, household_ratios
from financeproject.reports.recurring import recurring, upcoming
from financeproject.reports.sankey import build, fold_small, save_sankey


@pytest.fixture
def family(session):
    """Two months of a small household with known numbers."""
    s = session
    load_bundled_cpi(s)
    for day in (date(2026, 7, 1), date(2026, 8, 3), date(2026, 9, 1)):
        upsert_price(s, "USD", "TRY", day, Decimal("40") + day.month - 7, "test")
        upsert_price(s, "XAU_G", "USD", day, Decimal("100"), "test")
    create_account(s, "Assets:Bank:Checking", kind="checking")
    create_account(s, "Assets:Bank:Deposit", kind="time_deposit")
    create_account(s, "Assets:Gold:Grams", kind="gold")
    create_account(s, "Liabilities:Card", kind="credit_card")
    create_account(s, "Liabilities:Loan", kind="consumer_loan")
    for path in ("Expenses:Food:Groceries", "Expenses:Food:Restaurants", "Expenses:Rent", "Expenses:Interest",
                 "Expenses:Electronics", "Income:Salary", "Income:Interest"):
        create_account(s, path)
    opening_balance(s, "Checking", Decimal(30000), date(2026, 6, 30))
    opening_balance(s, "Deposit", Decimal(100000), date(2026, 6, 30))
    opening_balance(s, "Liabilities:Loan", Decimal(12000), date(2026, 6, 30))
    for month in (7, 8):
        record_income(s, Decimal(50000), "Salary", "Checking", date(2026, month, 1), "Salary")
        record_expense(s, Decimal(20000), "Rent", "Checking", date(2026, month, 3), "Rent")
        record_expense(s, Decimal(6000), "Groceries", "Card", date(2026, month, 5), "Groceries")
        record_expense(s, Decimal(2000), "Restaurants", "Card", date(2026, month, 6), "Dinner")
        record_transfer(s, Decimal(8000), "Checking", "Card", date(2026, month, 20), "Card payment")
        add_transaction(
            s, date(2026, month, 25), "Loan payment",
            [Leg("Liabilities:Loan", Decimal(2000)), Leg("Expenses:Interest", Decimal(500)), Leg("Checking", Decimal(-2500))],
        )
        record_income(s, Decimal(3000), "Income:Interest", "Deposit", date(2026, month, 28), "Interest")
    record_installment_purchase(s, Decimal(6000), "Electronics", "Card", date(2026, 7, 10), "Laptop", 3)
    record_exchange(s, "Checking", Decimal(8000), "Assets:Gold:Grams", Decimal(2), date(2026, 8, 10))
    return s


def test_income_statement_by_month_and_depth(family):
    st = income_statement(family, date(2026, 7, 1), date(2026, 8, 1))
    jul, aug = date(2026, 7, 1), date(2026, 8, 1)
    assert st.months == [jul, aug]
    assert st.income == {"Income:Salary": {jul: 50000, aug: 50000}, "Income:Interest": {jul: 3000, aug: 3000}}
    assert st.expenses["Expenses:Food"] == {jul: 8000, aug: 8000}  # depth 2 groups groceries and restaurants
    assert st.expenses["Expenses:Electronics"] == {jul: 6000}  # the installment purchase is a July expense in full
    assert st.total_income() == 106000 and st.total_expenses(jul) == 20000 + 8000 + 500 + 6000
    assert st.net(aug) == 53000 - 28500
    assert st.savings_rate(aug) == pytest.approx(24500 / 53000)
    assert list(IncomeStatement.totals(st.expenses))[0] == "Expenses:Rent"
    assert truncate("Expenses:Food:Groceries", 1) == "Expenses" and truncate("A:B", None) == "A:B"
    deep = income_statement(family, jul, aug, depth=0)
    assert "Expenses:Food:Groceries" in deep.expenses


def test_income_statement_in_real_terms_is_larger_for_older_months(family):
    nominal = income_statement(family, date(2026, 7, 1), date(2026, 8, 1))
    real = income_statement(family, date(2026, 7, 1), date(2026, 8, 1), unit="real")
    jul = date(2026, 7, 1)
    assert real.income["Income:Salary"][jul] > nominal.income["Income:Salary"][jul]


def test_cash_flow_months(family):
    months = cash_flow(family, date(2026, 7, 1), date(2026, 8, 1))
    jul, aug = months
    assert jul.beginning == 30000  # only the checking account is cash; the deposit is tier 1
    assert jul.line("income") == 50000 and jul.line("expenses") == -20000 - 500
    assert jul.line("operating") == 50000 - 20500
    assert jul.line("cards") == -8000 and jul.line("loans") == -2000
    assert jul.ending == 30000 + 50000 - 20000 - 8000 - 2500
    assert aug.beginning == jul.ending
    # 2 g of gold worth 41 × 100 × 2 = 8,200 TRY at market, bought for 8,000
    assert aug.line("investing") == -8200 and aug.line("exchange") == 200
    assert aug.ending == aug.beginning + 50000 - 20000 - 8000 - 2500 - 8000
    for month in months:
        assert month.decomposition.residual == 0


def test_household_ratios(family):
    report = household_ratios(family, date(2026, 8, 31), months=2)
    assert report.income == 106000 and report.expenses == 20000 * 2 + 8000 * 2 + 500 * 2 + 6000
    assert report.principal_repaid == 4000
    assert report.loan_payments == 5000 and report.mortgage_payments == 0
    assert report.installments_due == 2000  # the laptop: first installment in August, the next ones later
    by_name = {r.name: r for r in report.ratios}
    assert by_name["Savings rate"].status == OK
    emergency = by_name["Emergency fund"]
    assert emergency.value == pytest.approx(float(report.liquid / report.monthly_expenses))
    assert by_name["Investment assets / net worth"].status == BELOW
    assert by_name["Inflation protection"].value is None  # no history a year back


def test_ratio_status_bands():
    from financeproject.reports.ratios import _status

    assert _status(16, 15, 20, higher_is_better=False) == BELOW
    assert _status(21, 15, 20, higher_is_better=False) == DANGER
    assert _status(-5, 10, 0) == DANGER and _status(5, 10, 0) == BELOW and _status(None, 1) is None


def test_sankey_nodes_and_drawing(tmp_path):
    income = {"Income:Salary": Decimal(90000), "Income:Interest": Decimal(5000)}
    expenses = {f"Expenses:C{i}": Decimal(1000 * (i + 1)) for i in range(12)}
    data = build(income, expenses)
    assert [n.label for n in data.left] == ["Salary", "Interest"]
    assert len(data.right) == 10 and data.right[8].label == "Other" and data.right[-1].label == "Saved"
    assert data.right[-1].value == 95000 - 78000 and data.grouped
    short = build({"Income:Salary": Decimal(100)}, {"Expenses:Rent": Decimal(150)})
    assert short.left[-1].label == "From savings" and short.left[-1].value == 50
    path = save_sankey(data, tmp_path / "s.png", "Test")
    assert path.stat().st_size > 20_000
    empty = save_sankey(build({}, {}), tmp_path / "e.png", "Empty")
    assert empty.exists()


def test_small_items_fold_into_other():
    values = {"A": Decimal(500), "B": Decimal(300), "C": Decimal(150), "D": Decimal(30), "E": Decimal(20)}
    assert [k for k, _ in fold_small(values, 9, "Other", Decimal(40))] == ["A", "B", "C", "Other"]
    assert dict(fold_small(values, 9, "Other", Decimal(40)))["Other"] == 50
    # one small item alone stays; the three largest always stay
    assert len(fold_small(values, 9, "Other", Decimal(25))) == 5
    assert [k for k, _ in fold_small(values, 9, "Other", Decimal(1000))] == ["A", "B", "C", "Other"]
    # a category already called "Other" is merged, not repeated
    folded = fold_small({**values, "Other": Decimal(10)}, 9, "Other", Decimal(40))
    assert [k for k, _ in folded] == ["A", "B", "C", "Other"] and folded[-1][1] == 60
    # the web's Sankey folds what is under 2% of the budget
    data = build({"Income:Salary": Decimal(10000)}, {"Expenses:Rent": Decimal(3000), "Expenses:Food": Decimal(1000),
                 "Expenses:Bills": Decimal(500), "Expenses:Tea": Decimal(100), "Expenses:Gum": Decimal(50)},
                 min_share=Decimal("0.02"))
    assert [n.label for n in data.right] == ["Rent", "Food", "Bills", "Other", "Saved"] and data.grouped


def dates_every(start, days, n, jitter=()):
    return [start + timedelta(days=days * k + (jitter[k] if k < len(jitter) else 0)) for k in range(n)]


def test_classify_periods():
    monthly = [date(2026, m, 3) for m in range(1, 8)]
    period, regular = classify(monthly)
    assert period.name == "monthly" and regular == 1.0
    weekly = dates_every(date(2026, 1, 5), 7, 6, jitter=(0, 1, -1, 0, 2))
    assert classify(weekly)[0].name == "weekly"
    assert classify([date(2025, 3, 1), date(2026, 3, 2)])[0].name == "yearly"
    assert classify([date(2026, 1, 1), date(2026, 2, 1)]) is None  # monthly needs three
    assert classify([date(2026, 1, 1), date(2026, 1, 3), date(2026, 1, 20)]) is None


def test_next_date_keeps_the_day_of_month():
    monthly = next(p for p in PERIODS if p.name == "monthly")
    assert next_date(monthly, [date(2026, 11, 30), date(2026, 12, 31), date(2027, 1, 31)]) == date(2027, 2, 28)
    weekly = next(p for p in PERIODS if p.name == "weekly")
    assert next_date(weekly, [date(2026, 1, 5)]) == date(2026, 1, 12)


def test_series_status_and_amounts():
    monthly = [Occurrence(date(2026, m, 7), Decimal("229.99")) for m in range(1, 7)]
    candidates = {
        "netflix": ("NETFLIX", "Expenses:Subscriptions", -1, "TRY", monthly),
        "visits": ("SHOP", "Expenses:Food", -1, "TRY", [Occurrence(date(2026, 1, 2), Decimal(100)), Occurrence(date(2026, 7, 1), Decimal(300))]),
    }
    [series] = find_series(candidates)
    assert series.label == "NETFLIX" and series.fixed and series.typical == Decimal("229.99")
    assert series.next_due == date(2026, 7, 7)
    assert series.status(date(2026, 7, 10)) == ACTIVE
    assert series.status(date(2026, 7, 20)) == LATE
    assert series.status(date(2026, 8, 20)) == STOPPED
    assert series.due_dates(date(2026, 7, 1), date(2026, 9, 30)) == [date(2026, 7, 7), date(2026, 8, 7), date(2026, 9, 7)]


def test_recurring_and_upcoming_on_the_demo(session):
    start, end = date(2025, 10, 1), date(2026, 9, 30)
    load_bundled_cpi(session)
    for q in synthetic_prices(start - timedelta(days=7), end):
        upsert_price(session, q.commodity, q.quote, q.on, q.value, q.source)
    market = load_market_data(session)
    build_demo(session, start, end, market.book, market.deflator)
    found = {s.label: s for s in recurring(session)}
    assert found["VIDEO STREAMING"].period.name == "monthly" and found["VIDEO STREAMING"].typical == Decimal("279.99")
    assert found["MUSIC STREAMING"].fixed
    assert found["SAMPLE EMPLOYER LTD"].direction == 1
    assert "BURGER RESTAURANT" not in found  # irregular restaurant visits are not a series
    items = upcoming(session, date(2026, 10, 1), days=31, series=list(found.values()))
    labels = [u.label for u in items]
    assert "Card installments (Sample Card)" in labels
    assert labels.count("VIDEO STREAMING") == 1
    video = next(u for u in items if u.label == "VIDEO STREAMING")
    assert (video.period, video.fixed, video.late) == ("monthly", found["VIDEO STREAMING"].fixed, False)
    assert {u.account for u in items if u.kind == "installments"} == {"Sample Card"}
    assert all(date(2026, 10, 1) <= u.on <= date(2026, 11, 1) for u in items if u.kind == "recurring")
