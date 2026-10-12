"""CSV import, duplicate detection and categorization rules."""

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from financeproject.core.kinds import ASSET, EXPENSE, INCOME, LIABILITY
from financeproject.errors import FinanceError
from financeproject.ingest.csvfile import (
    CsvFormat,
    Row,
    import_csv,
    import_key,
    parse_column_map,
    read_rows,
)
from financeproject.ingest.rules import (
    add_rule,
    apply_rules_to_uncategorized,
    load_rules,
    match,
)
from financeproject.ledger.accounts import create_account
from financeproject.ledger.models import Transaction
from financeproject.ledger.queries import commodity_totals, positions_at
from financeproject.ledger.transactions import Leg, add_transaction

STANDARD = """date,description,amount,account,counter_account,commodity,payee,note
2026-10-01,MİGROS KONYA,-450.00,Bonus,,,,
2026-10-02,October salary,"65,000.00",Checking,Income:Salary,,Employer,
2026-10-03,Card payment,-450.00,Checking,Bonus,,,
2026-10-04,KAHVE DÜNYASI,-95.50,Bonus,,,,
2026-10-04,KAHVE DÜNYASI,-95.50,Bonus,,,,second coffee
2026-10-05,UNKNOWN PLACE,-10.00,Bonus,,,,
"""


@pytest.fixture
def ledger(session):
    create_account(session, "Assets:Bank:Checking", kind="checking")
    create_account(session, "Liabilities:Credit Card:Bonus", kind="credit_card")
    create_account(session, "Expenses:Food:Groceries")
    create_account(session, "Expenses:Food:Coffee")
    create_account(session, "Income:Salary")
    add_rule(session, "migros", "Groceries")
    add_rule(session, r"kahve\s+dunyasi", "Coffee", is_regex=True)
    return session


def write(tmp_path, text, name="statement.csv", encoding="utf-8"):
    path = tmp_path / name
    path.write_bytes(text.encode(encoding))
    return path


def tx_count(session):
    return session.scalar(select(func.count()).select_from(Transaction))


def test_standard_import_with_rules_and_duplicates(ledger, tmp_path):
    s = ledger
    path = write(tmp_path, STANDARD)
    result = import_csv(s, path)
    assert result.errors == [] and result.written
    assert result.added == 6 and result.duplicates == 0 and result.uncategorized == 1
    how = [r.how for r in result.rows]
    assert how[0].startswith("rule #") and how[1] == "file" and how[3].startswith("rule #") and how[5] == "uncategorized"
    balances = {p.account: p.quantity for p in positions_at(s, date(2026, 10, 31), types=(ASSET, LIABILITY, EXPENSE, INCOME))}
    assert balances["Expenses:Food:Groceries"] == Decimal("450.00")
    assert balances["Expenses:Food:Coffee"] == Decimal("191.00")  # both identical coffees are kept
    assert balances["Expenses:Uncategorized"] == Decimal("10.00")
    assert balances["Liabilities:Credit Card:Bonus"] == Decimal("-201.00")  # 450 + 191 + 10 spent, 450 paid
    assert balances["Assets:Bank:Checking"] == Decimal("64550.00")
    assert all(v == 0 for v in commodity_totals(s).values())

    again = import_csv(s, path)
    assert again.duplicates == 6 and again.added == 0
    assert tx_count(s) == 6


def test_dry_run_writes_nothing(ledger, tmp_path):
    result = import_csv(ledger, write(tmp_path, STANDARD), dry_run=True)
    assert len(result.rows) == 6 and not result.written and result.added == 0
    assert tx_count(ledger) == 0


def test_invalid_rows_abort_the_whole_import(ledger, tmp_path):
    text = STANDARD + "2026-10-06,Broken row,fifteen lira,Bonus,,,,\n2026-13-01,Wrong date,-5,Bonus,,,,\n"
    result = import_csv(ledger, write(tmp_path, text))
    assert len(result.errors) == 2 and not result.written
    assert result.errors[0].startswith("line 8")
    assert tx_count(ledger) == 0


def test_unknown_account_is_an_error(ledger, tmp_path):
    result = import_csv(ledger, write(tmp_path, "date;description;amount;account\n2026-10-01;x;-1;No Such\n"))
    assert result.errors and "No account matches" in result.errors[0]


def test_bank_layout_with_mapping(ledger, tmp_path):
    """Windows-1254 file, two lines of account details above the header,
    separate debit/credit columns, Turkish dates and decimal commas."""
    text = (
        "Hesap No: TR00 0000;;;\n"
        "Dönem: Ekim 2026;;;\n"
        "İşlem Tarihi;Açıklama;Borç;Alacak\n"
        "01.10.2026;MİGROS ŞUBE;1.250,75;\n"
        "02.10.2026;MAAŞ;;65.000,00\n"
    )
    path = write(tmp_path, text, encoding="cp1254")
    fmt = CsvFormat(
        date_format="%d.%m.%Y",
        columns=parse_column_map("date=İşlem Tarihi,description=Açıklama,outflow=Borç,inflow=Alacak"),
        skip_rows=2,
    )
    rows, errors, encoding = read_rows(path, fmt)
    assert errors == [] and encoding == "cp1254"
    assert [(r.date, r.amount) for r in rows] == [(date(2026, 10, 1), Decimal("-1250.75")), (date(2026, 10, 2), Decimal("65000.00"))]
    result = import_csv(ledger, path, fmt, account="Checking")
    assert result.written and result.added == 2
    assert [r.counter_account for r in result.rows] == ["Expenses:Food:Groceries", "Income:Uncategorized"]


def test_decimal_separator_is_detected_per_file(ledger, tmp_path):
    turkish = "date;description;amount\n2026-10-01;MIGROS;-1.234,50\n2026-10-02;X;-7\n"
    rows, errors, _ = read_rows(write(tmp_path, turkish), CsvFormat())
    assert errors == [] and [r.amount for r in rows] == [Decimal("-1234.50"), Decimal("-7")]
    english = "date,description,amount\n2026-10-01,MIGROS,\"-1,234.50\"\n"
    rows, _, _ = read_rows(write(tmp_path, english), CsvFormat())
    assert rows[0].amount == Decimal("-1234.50")
    ambiguous = "date,description,amount\n2026-10-01,MIGROS,-1.234\n"
    with pytest.raises(FinanceError, match="decimal separator"):
        read_rows(write(tmp_path, ambiguous), CsvFormat())
    rows, _, _ = read_rows(write(tmp_path, ambiguous), CsvFormat(decimal_sep=","))
    assert rows[0].amount == Decimal("-1234")


def test_invert_sign_and_autodetected_delimiter(ledger, tmp_path):
    text = "date,description,amount\n2026-10-01,MIGROS,\"1.000,00\"\n"
    fmt = CsvFormat(invert_sign=True)
    result = import_csv(ledger, write(tmp_path, text), fmt, account="Bonus")
    assert result.written and result.rows[0].amount == Decimal("-1000.00")


def test_layout_errors(tmp_path):
    with pytest.raises(FinanceError, match="date and description"):
        read_rows(write(tmp_path, "a;b\n1;2\n"), CsvFormat())
    with pytest.raises(FinanceError, match="'Amount X' column"):
        read_rows(write(tmp_path, "date;description\n"), CsvFormat(delimiter=";", columns={"amount": "Amount X"}))
    with pytest.raises(FinanceError, match="amount"):
        read_rows(write(tmp_path, "date;description\n2026-10-01;x\n"), CsvFormat())
    with pytest.raises(FinanceError):
        parse_column_map("date")
    with pytest.raises(FinanceError, match="Unknown field"):
        parse_column_map("amout=Amount")


def test_import_key_is_stable_and_order_sensitive():
    row = Row(2, date(2026, 10, 1), "MİGROS", Decimal("-450.00"), None, None, None, None, None)
    same = Row(9, date(2026, 10, 1), "migros", Decimal("-450.0"), None, None, None, None, None)
    assert import_key("Liabilities:Card", row, 1) == import_key("liabilities:card", same, 1)
    assert import_key("Liabilities:Card", row, 1) != import_key("Liabilities:Card", row, 2)


def test_rules_priority_folding_and_recategorization(ledger):
    s = ledger
    create_account(s, "Expenses:Food:Restaurants")
    add_rule(s, "MİGROS JET", "Restaurants", priority=5)  # more specific, tried first
    rules = load_rules(s)
    assert match(rules, "migros jet kadikoy").account.path == "Expenses:Food:Restaurants"
    assert match(rules, "Migros Konya").account.path == "Expenses:Food:Groceries"
    assert match(rules, "elsewhere") is None
    with pytest.raises(FinanceError):
        add_rule(s, "(", "Groceries", is_regex=True)
    with pytest.raises(FinanceError):
        add_rule(s, "  ", "Groceries")

    add_transaction(s, date(2026, 10, 1), "BİM MARKET", [Leg("Expenses:Uncategorized", Decimal("80")), Leg("Bonus", None)])
    assert apply_rules_to_uncategorized(s) == []
    add_rule(s, "bim", "Groceries")
    moved = apply_rules_to_uncategorized(s)
    assert [(tx.description, acc.path) for tx, acc in moved] == [("BİM MARKET", "Expenses:Food:Groceries")]
