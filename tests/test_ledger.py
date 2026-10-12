"""Double-entry rules, accounts, helpers and balance queries."""

from datetime import date, timedelta
from decimal import Decimal

import pytest
from conftest import simple_book
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from sqlalchemy import select

from financeproject.core.kinds import ASSET, EQUITY, EXPENSE, INCOME, LIABILITY
from financeproject.core.valuation import value_positions
from financeproject.errors import (
    AccountError,
    AccountNotFound,
    AmbiguousAccount,
    AmountError,
    DuplicateTransaction,
    FinanceError,
    LedgerError,
    UnbalancedTransaction,
)
from financeproject.ledger.accounts import create_account, find_account, normalize_path
from financeproject.ledger.db import init_db, make_engine, open_ledger, session_scope
from financeproject.ledger.models import Account, Commodity, Meta, Transaction
from financeproject.ledger.queries import (
    commodity_totals,
    positions_at,
    positions_over_time,
)
from financeproject.ledger.transactions import (
    Leg,
    add_transaction,
    delete_transaction,
    opening_balance,
    record_exchange,
    record_expense,
    record_income,
    record_transfer,
)

D = date(2026, 3, 2)


@pytest.fixture
def household(session):
    create_account(session, "Assets:Bank:Checking", kind="checking", owner="Me", institution="Bank A")
    create_account(session, "Assets:FX:USD", kind="fx", commodity="USD", owner="Me")
    create_account(session, "Assets:Gold:Grams", kind="gold", owner="Dad")
    create_account(session, "Assets:Bank:Joint", kind="checking")
    create_account(session, "Liabilities:Credit Card:Bonus", kind="credit_card", owner="Me")
    create_account(session, "Expenses:Food:Groceries")
    create_account(session, "Expenses:Food:Restaurants")
    create_account(session, "Income:Salary")
    return session


def test_init_db_seeds_and_is_idempotent(engine):
    init_db(engine)
    with session_scope(engine) as s:
        codes = set(s.scalars(select(Commodity.code)))
        paths = set(s.scalars(select(Account.path)))
    assert {"TRY", "USD", "EUR", "XAU_G"} <= codes
    assert {"Equity:Opening Balances", "Expenses:Uncategorized", "Income:Uncategorized"} <= paths
    open_ledger(engine)  # the current schema opens


def test_ledgers_of_version_00_are_refused(engine):
    with session_scope(engine) as s:
        s.get(Meta, "schema_version").value = "1"
    with pytest.raises(FinanceError, match="version 00"):
        open_ledger(engine)


def test_normalize_path():
    assert normalize_path("assets : Bank :  Ziraat  Checking") == "Assets:Bank:Ziraat Checking"
    assert normalize_path("LIABILITY:Card") == "Liabilities:Card"
    assert normalize_path("expense:Rent") == "Expenses:Rent"
    for bad in ("Bank:X", "Assets", "Assets::X", "Expenses: "):
        with pytest.raises(AccountError):
            normalize_path(bad)


def test_create_account_rules(session):
    a = create_account(session, "assets:gold:bar", kind="gold")
    assert (a.path, a.type, a.commodity_code) == ("Assets:gold:bar", ASSET, "XAU_G")
    with pytest.raises(AccountError, match="already exists"):
        create_account(session, "Assets:GOLD:Bar")  # same after folding
    with pytest.raises(AccountError, match="needs a commodity"):
        create_account(session, "Assets:FX:Euro", kind="fx")
    with pytest.raises(AccountError, match="not a valid kind"):
        create_account(session, "Assets:Card", kind="credit_card")
    with pytest.raises(AccountError, match="Unknown commodity"):
        create_account(session, "Assets:Fund:X", kind="fund", commodity="TEFAS.X")
    with pytest.raises(AccountError, match="liquidity tier"):
        create_account(session, "Liabilities:Loan", kind="mortgage", tier=1)
    with pytest.raises(AccountError, match="Only asset and liability"):
        create_account(session, "Expenses:Rent", kind="checking")
    e = create_account(session, "Expenses:Rent")
    assert e.type == EXPENSE and e.commodity_code is None


def test_find_account(household):
    s = household
    assert find_account(s, "assets:bank:checking").path == "Assets:Bank:Checking"
    assert find_account(s, "groceries").path == "Expenses:Food:Groceries"
    assert find_account(s, "bonus").path == "Liabilities:Credit Card:Bonus"
    with pytest.raises(AmbiguousAccount) as exc:
        find_account(s, "food")
    assert exc.value.candidates == ["Expenses:Food:Groceries", "Expenses:Food:Restaurants"]
    with pytest.raises(AccountNotFound):
        find_account(s, "no such thing")


def test_balanced_transaction_and_elided_leg(household):
    s = household
    tx = add_transaction(s, D, "Groceries", [Leg("Groceries", Decimal("450")), Leg("Bonus", None)])
    amounts = {p.account.path: p.amount for p in tx.postings}
    assert amounts == {"Expenses:Food:Groceries": Decimal("450"), "Liabilities:Credit Card:Bonus": Decimal("-450")}


def test_rules_reject_bad_transactions(household):
    s = household
    with pytest.raises(UnbalancedTransaction):
        add_transaction(s, D, "x", [Leg("Groceries", Decimal("450")), Leg("Checking", Decimal("-400"))])
    with pytest.raises(LedgerError, match="at least two"):
        add_transaction(s, D, "x", [Leg("Groceries", Decimal("1"))])
    with pytest.raises(LedgerError, match="At most one"):
        add_transaction(s, D, "x", [Leg("Groceries", None), Leg("Checking", None)])
    with pytest.raises(LedgerError, match="zero"):
        add_transaction(s, D, "x", [Leg("Groceries", Decimal(0)), Leg("Checking", Decimal(0))])
    with pytest.raises(AmountError):
        add_transaction(s, D, "x", [Leg("Groceries", Decimal("1.001")), Leg("Checking", None)])
    with pytest.raises(LedgerError, match="holds only USD"):
        add_transaction(s, D, "x", [Leg("Assets:FX:USD", Decimal("1"), "TRY"), Leg("Checking", None)])
    with pytest.raises(UnbalancedTransaction, match="cannot balance"):
        add_transaction(
            s, D, "x", [Leg("Assets:FX:USD", Decimal("10")), Leg("Groceries", Decimal("5")), Leg("Checking", None)]
        )
    with pytest.raises(LedgerError, match="description"):
        add_transaction(s, D, " ", [Leg("Groceries", Decimal("1")), Leg("Checking", None)])


def test_open_and_closed_dates(household):
    s = household
    a = create_account(s, "Assets:Bank:New", kind="checking", opened_on=D)
    with pytest.raises(LedgerError, match="opened"):
        add_transaction(s, D - timedelta(days=1), "x", [Leg(a, Decimal("1")), Leg("Salary", None)])
    add_transaction(s, D, "x", [Leg(a, Decimal("1")), Leg("Salary", None)])
    a.closed_on = D
    with pytest.raises(LedgerError, match="closed"):
        add_transaction(s, D + timedelta(days=1), "x", [Leg(a, Decimal("1")), Leg("Salary", None)])


def test_duplicate_import_key(household):
    s = household
    add_transaction(s, D, "a", [Leg("Groceries", Decimal("1")), Leg("Checking", None)], import_key="k1")
    with pytest.raises(DuplicateTransaction):
        add_transaction(s, D, "a", [Leg("Groceries", Decimal("1")), Leg("Checking", None)], import_key="k1")


def test_opening_balance_signs(household):
    s = household
    opening_balance(s, "Checking", Decimal("10000"), D)
    opening_balance(s, "Bonus", Decimal("2500"), D)  # a debt of 2,500
    opening_balance(s, "USD", Decimal("100"), D)
    pos = {(p.account, p.commodity): p.quantity for p in positions_at(s, D, types=(ASSET, LIABILITY, EQUITY))}
    assert pos[("Assets:Bank:Checking", "TRY")] == Decimal("10000")
    assert pos[("Liabilities:Credit Card:Bonus", "TRY")] == Decimal("-2500")
    assert pos[("Equity:Opening Balances", "TRY")] == Decimal("-7500")
    assert pos[("Equity:Opening Balances", "USD")] == Decimal("-100")
    with pytest.raises(LedgerError):
        opening_balance(s, "Groceries", Decimal("1"), D)


def test_helpers_card_cycle_and_exchange(household):
    s = household
    opening_balance(s, "Checking", Decimal("100000"), D)
    record_income(s, Decimal("50000"), "Salary", "Checking", D, "Salary")
    record_expense(s, Decimal("1200"), "Groceries", "Bonus", D, "Groceries")  # card spending: debt grows
    record_transfer(s, Decimal("1200"), "Checking", "Bonus", D, "Card payment")  # paying it off
    tx = record_exchange(s, "Checking", Decimal("40000"), "Assets:FX:USD", Decimal("1000"), D)
    assert len(tx.postings) == 4
    assert tx.note == "Rate: 1 USD = 40.000000 TRY"
    by_code = {}
    for p in tx.postings:
        by_code[p.commodity_code] = by_code.get(p.commodity_code, 0) + p.amount
    assert by_code == {"TRY": 0, "USD": 0}
    pos = {p.account: p.quantity for p in positions_at(s, D)}
    assert pos["Assets:Bank:Checking"] == Decimal("100000") + 50000 - 1200 - 40000
    assert "Liabilities:Credit Card:Bonus" not in pos  # paid off: zero balances are dropped
    assert pos["Assets:FX:USD"] == Decimal("1000")
    with pytest.raises(LedgerError, match="exchange"):
        record_transfer(s, Decimal("1"), "Checking", "Assets:FX:USD", D)
    with pytest.raises(LedgerError, match="transfer"):
        record_exchange(s, "Checking", Decimal("1"), "Joint", Decimal("1"), D)
    with pytest.raises(LedgerError, match="positive"):
        record_expense(s, Decimal("-5"), "Groceries", "Checking", D, "x")
    with pytest.raises(LedgerError, match="Expenses"):
        record_expense(s, Decimal("5"), "Salary", "Checking", D, "x")
    assert all(v == 0 for v in commodity_totals(s).values())


def test_positions_owner_filter_and_dates(household):
    s = household
    opening_balance(s, "Checking", Decimal("100"), D)
    opening_balance(s, "Grams", Decimal("2"), D)
    opening_balance(s, "Joint", Decimal("7"), D + timedelta(days=5))
    assert {p.account for p in positions_at(s, D, owner="dad")} == {"Assets:Gold:Grams"}
    assert {p.account for p in positions_at(s, D + timedelta(days=5), owner="shared")} == {"Assets:Bank:Joint"}
    assert {p.account for p in positions_at(s, D, owner="shared")} == set()
    assert positions_at(s, D - timedelta(days=1)) == []


def test_delete_transaction(household):
    s = household
    tx = record_income(s, Decimal("5"), "Salary", "Checking", D, "x")
    delete_transaction(s, tx.id)
    assert s.get(Transaction, tx.id) is None
    assert positions_at(s, D) == []
    with pytest.raises(LedgerError):
        delete_transaction(s, 999)


amounts = st.decimals(min_value=Decimal("0.01"), max_value=Decimal("50000"), places=2)
operations = st.lists(
    st.tuples(st.sampled_from(["income", "spend", "card", "transfer", "usd", "gold"]), amounts, st.integers(0, 60)),
    min_size=1,
    max_size=25,
)


@settings(max_examples=40, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(ops=operations, probe=st.integers(0, 61))
def test_random_ledgers_stay_consistent(ops, probe):
    """For any sequence of operations:

    1. every commodity sums to zero over the whole ledger;
    2. balances read in one pass over many dates equal balances read per date;
    3. valued at the same prices, all accounts together are worth exactly zero,
       so net worth equals minus the value of equity, income and expenses.
    """
    engine = make_engine(":memory:")
    init_db(engine)
    start = date(2026, 1, 1)
    with session_scope(engine) as s:
        create_account(s, "Assets:Bank", kind="checking")
        create_account(s, "Assets:USD", kind="fx", commodity="USD")
        create_account(s, "Assets:Gold", kind="gold")
        create_account(s, "Liabilities:Card", kind="credit_card")
        create_account(s, "Expenses:General")
        create_account(s, "Income:General")
        for kind, value, offset in ops:
            on = start + timedelta(days=offset)
            if kind == "income":
                record_income(s, value, "Income:General", "Assets:Bank", on, "i")
            elif kind == "spend":
                record_expense(s, value, "Expenses:General", "Assets:Bank", on, "s")
            elif kind == "card":
                record_expense(s, value, "Expenses:General", "Liabilities:Card", on, "c")
            elif kind == "transfer":
                record_transfer(s, value, "Assets:Bank", "Liabilities:Card", on, "t")
            elif kind == "usd":
                record_exchange(s, "Assets:Bank", value, "Assets:USD", (value / 40).quantize(Decimal("0.01")) or Decimal("0.01"), on)
            else:
                grams = (value / 4000).quantize(Decimal("0.0001")) or Decimal("0.0001")
                record_exchange(s, "Assets:Bank", value, "Assets:Gold", grams, on)

        assert all(v == 0 for v in commodity_totals(s).values())

        dates = [start + timedelta(days=k) for k in range(0, 62, 7)] + [start + timedelta(days=probe)]
        all_types = (ASSET, LIABILITY, EQUITY, INCOME, EXPENSE)
        series = positions_over_time(s, dates, types=all_types)
        for d in dates:
            assert series[d] == positions_at(s, d, types=all_types)

        at = start + timedelta(days=probe)
        book = simple_book()
        valued = value_positions(positions_at(s, at, types=all_types), book, at, max_age_days=10_000)
        total = sum((v.value for v in valued), Decimal(0))
        assert total == 0
        balance_sheet = sum((v.value for v in valued if v.position.account_type in (ASSET, LIABILITY)), Decimal(0))
        others = sum((v.value for v in valued if v.position.account_type not in (ASSET, LIABILITY)), Decimal(0))
        assert balance_sheet == -others
    engine.dispose()
