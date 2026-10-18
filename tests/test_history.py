"""Editing transactions, the change history, searching, import records and undo, category review."""

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import func, select
from typer.testing import CliRunner

from financeproject import config
from financeproject.cli import app
from financeproject.errors import FinanceError
from financeproject.ingest.banks import read_statement
from financeproject.ingest.importer import import_statement, import_views, undo_import
from financeproject.ingest.review import accept_confident, categorize, review
from financeproject.ingest.rules import add_rule
from financeproject.ledger.accounts import close_account, create_account, find_account
from financeproject.ledger.db import auto_backup, init_db, make_engine, session_scope
from financeproject.ledger.history import (
    DELETE,
    EDIT,
    IMPORT,
    RECATEGORIZE,
    UNDO_IMPORT,
    history_of,
    recent_changes,
)
from financeproject.ledger.installments import record_installment_purchase
from financeproject.ledger.models import ImportBatch, InstallmentPlan, Transaction
from financeproject.ledger.queries import (
    account_balance,
    find_transactions,
    uncategorized_count,
)
from financeproject.ledger.transactions import (
    OTHER,
    SIMPLE,
    TRANSFER,
    delete_transaction,
    opening_balance,
    recategorize,
    record_exchange,
    record_expense,
    record_income,
    record_transfer,
    shape,
    update_transaction,
)
from financeproject.prices.update import update_all, update_range

D = Decimal


@pytest.fixture
def book(session):
    create_account(session, "Assets:Bank:Checking", kind="checking")
    create_account(session, "Assets:Bank:Savings", kind="checking")
    create_account(session, "Assets:FX:Dollars", kind="fx", commodity="USD")
    create_account(session, "Liabilities:Credit Card:Sample Card", kind="credit_card")
    for path in ("Expenses:Food:Groceries", "Expenses:Food:Restaurants", "Expenses:Home", "Income:Salary"):
        create_account(session, path)
    opening_balance(session, "Checking", D(10000), date(2026, 5, 1))
    return session


def test_editing_a_simple_transaction(book):
    s = book
    tx = record_expense(s, D("450.00"), "Groceries", "Checking", date(2026, 5, 4), "ŞEKER ve un", "Bakkal")
    assert shape(tx) == SIMPLE
    update_transaction(
        s, tx.id, on=date(2026, 5, 5), description="Şeker, un", payee="", note="weekly", amount=D("455.50"),
        category="Restaurants", account="Savings",
    )
    assert (tx.date, tx.description, tx.payee, tx.note) == (date(2026, 5, 5), "Şeker, un", None, "weekly")
    legs = {p.account.path: p.amount for p in tx.postings}
    assert legs == {"Assets:Bank:Savings": D("-455.50"), "Expenses:Food:Restaurants": D("455.50")}
    (change,) = history_of(s, tx.id)
    assert change.action == EDIT and change.before["date"] == "2026-05-04" and change.after["note"] == "weekly"
    assert "date 2026-05-04 → 2026-05-05" in change.summary
    update_transaction(s, tx.id, description="Şeker, un")  # nothing changes: nothing recorded
    assert len(history_of(s, tx.id)) == 1


def test_what_each_shape_allows(book):
    s = book
    transfer = record_transfer(s, D(1000), "Checking", "Savings", date(2026, 5, 6))
    assert shape(transfer) == TRANSFER
    update_transaction(s, transfer.id, amount=D(1200))
    assert sorted(p.amount for p in transfer.postings) == [D(-1200), D(1200)]
    with pytest.raises(FinanceError, match="no category"):
        update_transaction(s, transfer.id, category="Groceries")
    exchange = record_exchange(s, "Checking", D(4000), "Dollars", D(100), date(2026, 5, 7))
    assert shape(exchange) == OTHER
    update_transaction(s, exchange.id, note="for the trip")
    with pytest.raises(FinanceError, match="more than two legs"):
        update_transaction(s, exchange.id, amount=D(1))
    purchase = record_installment_purchase(s, D(6000), "Home", "Sample Card", date(2026, 5, 8), "Sofa", 3)
    update_transaction(s, purchase.id, amount=D(6300))
    assert s.scalars(select(InstallmentPlan)).one().total == D(6300)
    with pytest.raises(FinanceError, match="stays on its card"):
        update_transaction(s, purchase.id, account="Checking")


def test_edits_are_checked(book):
    s = book
    tx = record_expense(s, D(10), "Groceries", "Checking", date(2026, 5, 4), "Milk")
    with pytest.raises(FinanceError, match="decimal places"):
        update_transaction(s, tx.id, amount=D("10.005"))
    assert {p.amount for p in tx.postings} == {D(10), D(-10)} and not history_of(s, tx.id)  # nothing changed
    with pytest.raises(FinanceError, match="holds USD"):
        update_transaction(s, tx.id, account="Dollars")
    with pytest.raises(FinanceError, match="cannot be empty"):
        update_transaction(s, tx.id, description=" ")


def test_deleting_and_recategorizing_are_recorded(book):
    s = book
    tx = record_expense(s, D(80), "Groceries", "Checking", date(2026, 5, 4), "Bread")
    recategorize(s, tx.id, "Restaurants")
    number = tx.id
    delete_transaction(s, number)
    actions = [c.action for c in history_of(s, number)]
    assert actions == [DELETE, RECATEGORIZE]
    deleted = history_of(s, number)[0]
    assert deleted.before["description"] == "Bread" and deleted.after is None
    assert {leg["account"] for leg in deleted.before["legs"]} == {"Assets:Bank:Checking", "Expenses:Food:Restaurants"}


def test_search(book):
    s = book
    record_expense(s, D("450.00"), "Groceries", "Checking", date(2026, 5, 4), "ŞEKER", "İYİ BAKKAL")
    record_expense(s, D("12.50"), "Restaurants", "Checking", date(2026, 5, 9), "Simit")
    record_income(s, D(30000), "Salary", "Checking", date(2026, 5, 1), "Salary")
    record_expense(s, D(99), "Expenses:Uncategorized", "Checking", date(2026, 5, 10), "Unknown shop")
    assert [r.description for r in find_transactions(s, text="seker")[0]] == ["ŞEKER"]
    assert [r.description for r in find_transactions(s, text="iyi bakkal")[0]] == ["ŞEKER"]
    assert [r.description for r in find_transactions(s, text="450")[0]] == ["ŞEKER"]
    assert [r.description for r in find_transactions(s, text="12,5")[0]] == ["Simit"]
    rows, total = find_transactions(s, limit=2)
    assert total == 5 and [r.date for r in rows] == [date(2026, 5, 10), date(2026, 5, 9)]
    assert find_transactions(s, start=date(2026, 5, 2), end=date(2026, 5, 9))[1] == 2
    salary = find_account(s, "Salary")
    assert [r.description for r in find_transactions(s, account_id=salary.id)[0]] == ["Salary"]
    rows, _ = find_transactions(s, uncategorized=True)
    assert [r.description for r in rows] == ["Unknown shop"] and uncategorized_count(s) == 1
    simit = find_transactions(s, text="simit")[0][0]
    assert (simit.amount, simit.accounts, simit.categories) == (D("-12.50"), ("Assets:Bank:Checking",), ("Expenses:Food:Restaurants",))


@pytest.fixture
def bank(session):
    create_account(session, "Assets:Bank:Ziraat Checking", kind="checking")
    create_account(session, "Expenses:Food:Groceries")
    create_account(session, "Expenses:Transport")
    create_account(session, "Income:Salary")
    add_rule(session, "maas", "Salary")
    return session


def test_imports_are_recorded_and_can_be_undone(bank, fixtures):
    s = bank
    path = fixtures / "ziraat_statement.xlsx"
    statement = read_statement(path)
    preview = import_statement(s, statement, path, "Ziraat Checking", add_opening=True, dry_run=True)
    line = next(r.line for r in preview.rows if r.how == "uncategorized")
    result = import_statement(s, statement, path, "Ziraat Checking", add_opening=True, overrides={line: "Transport"})
    assert result.written and result.batch_id is not None
    chosen = next(r for r in result.rows if r.line == line)
    assert chosen.how == "chosen" and chosen.counter_account == "Expenses:Transport"
    batch = s.get(ImportBatch, result.batch_id)
    assert (batch.added, batch.statement_end, batch.statement_balance) == (17, date(2026, 9, 30), D("35086.32"))
    assert len(batch.links) == 17 and batch.file_sha256 and batch.file_name == "ziraat_statement.xlsx"
    assert find_transactions(s, import_id=batch.id)[1] == 17
    (view,) = import_views(s)
    assert (view.added, view.left, view.edited, view.account) == (17, 17, 0, "Assets:Bank:Ziraat Checking")
    some = find_transactions(s, import_id=batch.id)[0][0]
    update_transaction(s, some.id, note="checked")
    assert import_views(s)[0].edited == 1
    # the same file again adds nothing, so it records no import
    again = import_statement(s, statement, path, "Ziraat Checking")
    assert again.added == 0 and again.batch_id is None
    assert undo_import(s, batch.id) == 17
    assert s.scalar(select(func.count()).select_from(Transaction)) == 0
    assert import_views(s)[0].undone_at is not None and import_views(s)[0].left == 0
    with pytest.raises(FinanceError, match="already undone"):
        undo_import(s, batch.id)
    assert [c.action for c in recent_changes(s)][:2] == [UNDO_IMPORT, EDIT] and recent_changes(s)[-1].action == IMPORT
    # undone, the file can be imported again
    assert import_statement(s, statement, path, "Ziraat Checking", add_opening=True).added == 16


def test_review_suggests_and_categorizes(bank, fixtures):
    s = bank
    path = fixtures / "ziraat_statement.xlsx"
    import_statement(s, read_statement(path), path, "Ziraat Checking", add_opening=True, use_model=False)
    items = review(s)
    assert len(items) == uncategorized_count(s) and all(not item.how.startswith("rule") for item in items)
    add_rule(s, "opet", "Transport")
    by_rule = [item for item in review(s) if item.how.startswith("rule")]
    assert len(by_rule) == 1 and by_rule[0].suggestion.path == "Expenses:Transport" and by_rule[0].confident
    target = next(item for item in review(s) if not item.how and item.amount < 0)
    categorize(s, target.transaction.id, "Groceries")
    assert history_of(s, target.transaction.id)[0].action == RECATEGORIZE
    with pytest.raises(FinanceError, match="no uncategorized leg"):
        categorize(s, target.transaction.id, "Groceries")
    before = uncategorized_count(s)
    assert accept_confident(s) == 1 and uncategorized_count(s) == before - 1


def test_a_refund_goes_back_to_its_expense_category(bank):
    s = bank
    refund = record_income(s, Decimal(120), "Income:Uncategorized", "Ziraat Checking", date(2026, 9, 29), "REFUND")
    categorize(s, refund.id, "Groceries")  # money back from a shop: less spent on groceries
    assert {p.account.path: p.amount for p in refund.postings}["Expenses:Food:Groceries"] == Decimal(-120)
    other = record_expense(s, Decimal(10), "Expenses:Uncategorized", "Ziraat Checking", date(2026, 9, 29), "X")
    with pytest.raises(FinanceError, match="income or expense"):
        categorize(s, other.id, "Ziraat Checking")


def test_automatic_backups_keep_the_newest(tmp_path):
    db = tmp_path / "ledger.sqlite"
    engine = make_engine(db)
    init_db(engine)
    engine.dispose()
    target = tmp_path / "backups"
    made = [auto_backup(db, target, keep=3) for _ in range(5)]
    left = sorted(target.glob("ledger-auto-*.sqlite"))
    assert left == made[-3:] and all(p.stat().st_mode & 0o777 == 0o600 for p in left)


def test_closing_an_account(book):
    s = book
    with pytest.raises(FinanceError, match="not zero"):
        close_account(s, "Checking", date(2026, 5, 31))
    record_transfer(s, D(10000), "Checking", "Savings", date(2026, 5, 20))
    assert close_account(s, "Checking", date(2026, 5, 31)).closed_on == date(2026, 5, 31)
    with pytest.raises(FinanceError, match="already closed"):
        close_account(s, "Checking", date(2026, 6, 1))
    with pytest.raises(FinanceError, match="was closed on"):
        update_transaction(s, s.scalars(select(Transaction).order_by(Transaction.id.desc())).first().id, on=date(2026, 6, 2))
    assert account_balance(s, find_account(s, "Savings"), date(2026, 6, 1)) == {"TRY": D(10000)}


def test_price_update_range_and_sources(session):
    assert update_range(session, None, None, date(2026, 5, 31)) == (date(2026, 5, 1), date(2026, 5, 31))
    with pytest.raises(FinanceError, match="before the start"):
        update_range(session, date(2026, 5, 2), date(2026, 5, 1), date(2026, 5, 31))
    with pytest.raises(FinanceError, match="Unknown price source"):
        update_all(session, None, date(2026, 5, 1), date(2026, 5, 2), only="stocks")


def test_commands(tmp_path, fixtures):
    runner = CliRunner(env={"COLUMNS": "200"})
    db = str(tmp_path / "ledger.sqlite")

    def ok(*args):
        result = runner.invoke(app, ["-d", db, *args])
        assert result.exit_code == 0, result.output
        return result.output

    ok("init")
    ok("account", "add", "Assets:Bank:Ziraat", "--kind", "checking")
    out = ok("import", str(fixtures / "ziraat_statement.xlsx"), "--account", "Ziraat", "--opening")
    assert "Recorded as import #1; to undo it: finance imports undo 1" in out
    assert len(list(config.backup_dir().glob("ledger-auto-*.sqlite"))) == 1
    assert "ziraat_statement.xlsx" in ok("imports", "list")
    out = ok("txn", "list", "--search", "netflix")
    assert "NETFLIX.COM" in out
    number = next(line.split("│")[1].strip() for line in out.splitlines() if "NETFLIX" in line)
    ok("account", "add", "Expenses:Subscriptions")
    ok("txn", "edit", number, "--category", "Subscriptions", "--note", "streaming")
    out = ok("txn", "history", number)
    assert "edit" in out and "streaming" in out
    assert runner.invoke(app, ["-d", db, "txn", "edit", number]).exit_code == 1
    out = ok("imports", "undo", "1")
    assert "17 transactions deleted" in out
    assert "undo import" in ok("txn", "history")
    assert runner.invoke(app, ["-d", db, "imports", "undo", "1"]).exit_code == 1
    engine = make_engine(db)
    with session_scope(engine) as s:
        assert s.scalar(select(func.count()).select_from(Transaction)) == 0
    engine.dispose()
