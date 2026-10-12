"""Splitting a change of value into flows and revaluation (core.decompose and reports.change)."""

from datetime import date, timedelta
from decimal import Decimal

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from financeproject.core.decompose import Event, Leg, decompose
from financeproject.core.inflation import Deflator
from financeproject.ledger.accounts import create_account
from financeproject.ledger.db import init_db, make_engine, session_scope
from financeproject.ledger.transactions import (
    opening_balance,
    record_exchange,
    record_expense,
    record_income,
    record_transfer,
)
from financeproject.prices.store import upsert_price
from financeproject.reports.balance_sheet import build_balance_sheet
from financeproject.reports.change import cash_change, networth_change
from financeproject.reports.context import MarketData

D0, D1, D2, D3 = date(2026, 1, 31), date(2026, 2, 10), date(2026, 2, 20), date(2026, 2, 28)


def usd_value(prices):
    def value(commodity, on):
        if commodity == "TRY":
            return Decimal(1)
        candidates = [d for d in prices if d <= on]
        return prices[max(candidates)] if candidates else None

    return value


def test_hand_worked_example():
    """Hold 1,000 TRY and 100 USD; USD/TRY 40 → 42 on Feb 15, 44 at the end.

    Feb 10: salary 5,000 TRY. Feb 20: buy 50 USD for 2,150 TRY (market 42: 2,100,
    so 50 TRY is lost to the spread).
    """
    value = usd_value({D0: Decimal(40), date(2026, 2, 15): Decimal(42), D3: Decimal(44)})
    events = [
        Event(D1, (Leg("TRY", Decimal(5000)), Leg("TRY", Decimal(-5000), "income", "Income:Salary"))),
        Event(
            D2,
            (
                Leg("TRY", Decimal(-2150)),
                Leg("TRY", Decimal(2150), "exchange", "Equity:Trading:TRY"),
                Leg("USD", Decimal(-50), "exchange", "Equity:Trading:USD"),
                Leg("USD", Decimal(50)),
            ),
        ),
    ]
    d = decompose(D0, D3, {"TRY": Decimal(1000), "USD": Decimal(100)}, events, value)
    assert d.start_value == 1000 + 100 * 40
    assert d.end_value == (1000 + 5000 - 2150) + 150 * 44
    assert d.flows == {"income": Decimal(5000), "exchange": Decimal(-50)}
    # USD: 100 held 40→42 (+200), then 150 held 42→44 (+300)
    assert d.revaluation == {"USD": Decimal(500)}
    assert d.residual == 0
    assert d.details["income"] == {"Income:Salary": Decimal(5000)}


def test_unvalued_commodities_are_left_out():
    value = usd_value({D0: Decimal(40)})

    def with_fund(commodity, on):
        return None if commodity == "FUND" else value(commodity, on)

    events = [
        Event(
            D1,
            (
                Leg("TRY", Decimal(-1000)),
                Leg("TRY", Decimal(1000), "exchange", "Equity:Trading:TRY"),
                Leg("FUND", Decimal(-10), "exchange", "Equity:Trading:FUND"),
                Leg("FUND", Decimal(10)),
            ),
        )
    ]
    d = decompose(D0, D3, {"TRY": Decimal(5000)}, events, with_fund)
    assert d.unvalued == ("FUND",)
    assert d.flows == {"unvalued": Decimal(-1000)}  # not an exchange loss: it went into the fund
    assert d.end_value == 4000 and d.residual == 0


@pytest.fixture
def household(session):
    s = session
    for code, quote, day, value in (
        ("USD", "TRY", date(2026, 1, 1), "40"),
        ("USD", "TRY", date(2026, 2, 15), "42"),
        ("XAU_G", "USD", date(2026, 1, 1), "100"),
        ("XAU_G", "USD", date(2026, 2, 15), "105"),
    ):
        upsert_price(s, code, quote, day, Decimal(value), "test")
    create_account(s, "Assets:Bank:Checking", kind="checking")
    create_account(s, "Assets:FX:USD", kind="fx", commodity="USD")
    create_account(s, "Assets:Gold:Grams", kind="gold")
    create_account(s, "Assets:Bank:Deposit", kind="time_deposit")
    create_account(s, "Liabilities:Card", kind="credit_card")
    create_account(s, "Liabilities:Loan", kind="consumer_loan")
    create_account(s, "Expenses:Food")
    create_account(s, "Expenses:Interest")
    create_account(s, "Income:Salary")
    opening_balance(s, "Checking", Decimal(100000), date(2026, 1, 1))
    opening_balance(s, "Assets:FX:USD", Decimal(500), date(2026, 1, 1))
    opening_balance(s, "Liabilities:Loan", Decimal(20000), date(2026, 1, 1))
    record_income(s, Decimal(60000), "Salary", "Checking", date(2026, 2, 1), "Salary")
    record_expense(s, Decimal(3000), "Food", "Card", date(2026, 2, 3), "Groceries")
    record_transfer(s, Decimal(3000), "Checking", "Card", date(2026, 2, 15), "Card payment")
    record_exchange(s, "Checking", Decimal(8500), "Assets:FX:USD", Decimal(200), date(2026, 2, 16))  # 42 at market
    record_exchange(s, "Checking", Decimal(16800), "Assets:Gold:Grams", Decimal(4), date(2026, 2, 17))  # 4,410 at market
    record_transfer(s, Decimal(20000), "Checking", "Assets:Bank:Deposit", date(2026, 2, 18), "Deposit")
    return s


@pytest.mark.parametrize("unit", ["try", "usd", "gold", "real"])
def test_networth_change_matches_the_balance_sheets(household, unit):
    s = household
    from financeproject.reports.context import load_market_data

    market = load_market_data(s)
    if unit == "real":
        months = {date(2026, m, 1): 100.0 + m for m in range(1, 4)}
        market = MarketData(market.book, Deflator(months), {})
    a, b = date(2026, 1, 31), date(2026, 2, 28)
    d = networth_change(s, a, b, unit, market)
    attribute = {"try": "try_", "usd": "usd", "gold": "gold_g", "real": "real_try"}[unit]
    for day_, value in ((a, d.start_value), (b, d.end_value)):
        expected = getattr(build_balance_sheet(s, day_, market=market).net_worth.net, attribute)
        assert value == pytest.approx(expected, abs=Decimal("1e-9"))
    assert abs(d.residual) < Decimal("1e-12")


def test_networth_change_in_try(household):
    d = networth_change(household, date(2026, 1, 31), date(2026, 2, 28))
    assert d.flows["income"] == 60000 and d.flows["expenses"] == -3000
    # USD bought at 42.5 against 42 at market (−100), gold at 4,200/g against 4,410 (+840)
    assert d.flows["exchange"] == Decimal(-100) + Decimal(840)
    # 500 USD held 40 → 42 (+1,000); 4 g bought at 4,410 and still 4,410 at the end
    assert d.revaluation == {"USD": Decimal(1000)}
    assert "opening" not in d.flows  # the opening balances are dated before the period


def test_cash_flow_categories(household):
    d = cash_change(household, date(2026, 1, 31), date(2026, 2, 28))
    assert d.flows["income"] == 60000
    assert d.flows["cards"] == -3000  # the card purchase leaves cash only when the card is paid
    assert "expenses" not in d.flows
    # buying gold (−16,800 at 4,200/g, worth 4,410/g) and moving 20,000 to a time deposit are investing
    assert d.flows["investing"] == -(4 * Decimal(4410)) - 20000
    assert d.flows["exchange"] == Decimal(-100) + Decimal(840)  # the dollar purchase stays in cash
    assert d.revaluation == {"USD": Decimal(1000)}
    assert d.residual == 0


amounts = st.decimals(min_value=Decimal("1"), max_value=Decimal("20000"), places=2)
operations = st.lists(
    st.tuples(st.sampled_from(["income", "spend", "card", "pay", "usd", "gold"]), amounts, st.integers(1, 59)),
    min_size=1,
    max_size=20,
)


@settings(max_examples=30, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(ops=operations, start=st.integers(0, 30), length=st.integers(1, 40))
def test_identity_holds_on_random_ledgers(ops, start, length):
    """Start value + flows + revaluation = end value, and both ends equal the balance sheet."""
    engine = make_engine(":memory:")
    init_db(engine)
    origin = date(2026, 1, 1)
    with session_scope(engine) as s:
        for q in (("USD", "TRY", date(2026, 1, 1), "40"), ("USD", "TRY", date(2026, 1, 20), "41.5"),
                  ("XAU_G", "USD", date(2026, 1, 1), "100"), ("XAU_G", "USD", date(2026, 2, 5), "97.25")):
            upsert_price(s, q[0], q[1], q[2], Decimal(q[3]), "test")
        create_account(s, "Assets:Bank", kind="checking")
        create_account(s, "Assets:USD", kind="fx", commodity="USD")
        create_account(s, "Assets:Gold", kind="gold")
        create_account(s, "Liabilities:Card", kind="credit_card")
        create_account(s, "Expenses:General")
        create_account(s, "Income:General")
        opening_balance(s, "Assets:Bank", Decimal(50000), origin)
        for kind, value, offset in ops:
            on = origin + timedelta(days=offset)
            if kind == "income":
                record_income(s, value, "Income:General", "Assets:Bank", on, "i")
            elif kind == "spend":
                record_expense(s, value, "Expenses:General", "Assets:Bank", on, "s")
            elif kind == "card":
                record_expense(s, value, "Expenses:General", "Liabilities:Card", on, "c")
            elif kind == "pay":
                record_transfer(s, value, "Assets:Bank", "Liabilities:Card", on, "p")
            elif kind == "usd":
                record_exchange(s, "Assets:Bank", value, "Assets:USD", (value / 41).quantize(Decimal("0.01")) or Decimal("0.01"), on)
            else:
                record_exchange(s, "Assets:Bank", value, "Assets:Gold", (value / 4000).quantize(Decimal("0.0001")) or Decimal("0.0001"), on)
        from financeproject.reports.context import load_market_data

        market = load_market_data(s)
        a = origin + timedelta(days=start)
        b = a + timedelta(days=length)
        for unit, attribute in (("try", "try_"), ("usd", "usd")):
            d = networth_change(s, a, b, unit, market)
            assert abs(d.residual) < Decimal("1e-12")
            assert d.start_value == pytest.approx(getattr(build_balance_sheet(s, a, market=market).net_worth.net, attribute), abs=Decimal("1e-9"))
            assert d.end_value == pytest.approx(getattr(build_balance_sheet(s, b, market=market).net_worth.net, attribute), abs=Decimal("1e-9"))
    engine.dispose()
