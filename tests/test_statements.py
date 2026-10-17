"""Bank statements: the Ziraat Bankası template, statement checks, importing.

The fixtures are made-up statements in Ziraat Bankası's two download formats,
written by ``fixtures/make_ziraat_samples.py``.
"""

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from financeproject.errors import FinanceError
from financeproject.ingest.banks import read_statement, read_table, ziraat
from financeproject.ingest.importer import import_statement
from financeproject.ingest.rules import add_rule
from financeproject.ingest.statement import (
    Statement,
    StatementLine,
    balance_breaks,
    check_totals,
    chronological,
)
from financeproject.ledger.accounts import create_account, find_account
from financeproject.ledger.models import Transaction
from financeproject.ledger.queries import account_balance, commodity_totals
from financeproject.ledger.transactions import opening_balance


@pytest.fixture(params=["ziraat_statement.xlsx", "ziraat_statement.html"])
def statement_path(request, fixtures):
    return fixtures / request.param


def test_ziraat_statement_is_read_in_both_formats(statement_path):
    st = read_statement(statement_path)
    assert st.bank == "Ziraat Bankası" and st.currency == "TRY"
    assert st.period == (date(2026, 9, 1), date(2026, 9, 30))
    assert len(st.lines) == 16
    assert [line.date for line in st.lines] == sorted(line.date for line in st.lines)  # oldest first
    assert st.lines[0].amount == Decimal("45000.00") and st.lines[0].balance == Decimal("57500.00")
    assert st.opening_balance == Decimal("12500.00") and st.closing_balance == Decimal("35086.32")
    assert st.debit_total == Decimal("-30413.68") and st.credit_total == Decimal("53000.00")
    # same-day lines keep their order: receipt numbers F15404, F15405, F15406
    assert [line.reference for line in st.lines[9:12]] == ["F15404", "F15405", "F15406"]
    assert [line.kind for line in st.lines] == [
        None, "fee", "fee", "card", None, "cash", None, "bill", "loan", "card", "card", "card", "transfer", "transfer", None, "card"
    ]
    assert st.lines[3].payee == "MIGROS KONYA"
    assert st.lines[10].payee == "BIM A.S. U123"
    assert st.lines[13].payee == "ALİ VELİ"  # mobile transfer: the other person
    assert st.lines[4].payee is None
    assert st.lines[1].payee == st.lines[2].payee == "Ziraat Bankası" and st.lines[5].payee == "ATM"
    assert st.lines[7].payee == "ÖRNEK ELEKTRİK PERAKENDE"  # bill: the company
    loan = st.lines[8]
    assert (loan.loan_reference, loan.installment, loan.payee) == ("12345678-0001", 5, "Loan 12345678-0001")


def test_both_formats_give_the_same_lines(fixtures):
    xlsx = read_statement(fixtures / "ziraat_statement.xlsx")
    html = read_statement(fixtures / "ziraat_statement.html")
    def fields(line):
        return (line.date, line.amount, line.balance, line.reference, line.payee, line.kind, line.loan_reference, line.installment)

    assert [fields(line) for line in xlsx.lines] == [fields(line) for line in html.lines]


def test_fixtures_match_their_generator(fixtures, tmp_path):
    import importlib.util

    spec = importlib.util.spec_from_file_location("make_ziraat_samples", fixtures / "make_ziraat_samples.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.write_xlsx(tmp_path / "s.xlsx")
    module.write_html(tmp_path / "s.html")
    assert (tmp_path / "s.xlsx").read_bytes() == (fixtures / "ziraat_statement.xlsx").read_bytes()
    assert (tmp_path / "s.html").read_bytes() == (fixtures / "ziraat_statement.html").read_bytes()


def test_the_html_bot_script_is_ignored(fixtures):
    rows, file_format = read_table(fixtures / "ziraat_statement.html")
    assert file_format == "html"
    assert not any("not data" in (cell or "") for row in rows for cell in row)


def test_payee_patterns():
    assert ziraat.payee_of("POS ALIŞVERİŞ KART NO: 4000 **** **** 0000 İŞYERİ: SOK-00001 ORNEK SUBE MUTABAKAT: 1") == "SOK-00001 ORNEK SUBE"
    assert ziraat.payee_of("SANAL POS ALIŞVERİŞ İŞYERİ: ORNEK ODEME/ONLINE MAGAZA MUTABAKAT: 7") == "ORNEK ODEME/ONLINE MAGAZA"
    assert ziraat.payee_of(" AYŞE YILMAZ Ziraat Mobil Havale") == "AYŞE YILMAZ"
    assert ziraat.payee_of("KİRA ÖDEMESİ") is None


def test_line_kinds():
    read = ziraat.read_description
    assert read("99999999-0001 hesap 12. taksit ödeme tutarı") == ziraat.Reading("loan", "Loan 99999999-0001", "99999999-0001", 12)
    assert read(" 99999999-0001  hesap 3 . taksit ödeme tutarı ").installment == 3
    assert read("KOMİSYON TUTARI") == ziraat.Reading("fee", "Ziraat Bankası")
    assert read("ATM QR PARA ÇEKME HESAP:0000 ATM: Z0001 ORNEK").kind == "cash"
    assert read("ORNEK DOGALGAZ/ Abone no: 1 / Fatura no: 2").payee == "ORNEK DOGALGAZ"
    assert read("ORNEK SU IDARESI Abone No: 1").payee == "ORNEK SU IDARESI"  # no slash
    assert read("ORNEK KISI ODEME FAST").kind == "transfer" and read("FASTFOOD").kind is None
    assert read("POS ALIŞVERİŞ İŞYERİ: ORNEK ATM MARKET MUTABAKAT: 1").kind == "card"  # a shop, not a cash machine
    assert read("KİRA ÖDEMESİ") == ziraat.Reading()


def line(day, amount, balance, ref=None):
    return StatementLine(date(2026, 9, day), f"line {ref or day}", Decimal(amount), Decimal(balance) if balance else None, ref)


def test_chronological_order_follows_the_running_balance():
    newest_first = [line(5, "-10", "80"), line(5, "-10", "90"), line(1, "100", "100")]
    ordered = chronological(newest_first)
    assert [x.balance for x in ordered] == [Decimal(100), Decimal(90), Decimal(80)]
    oldest_first = list(reversed(newest_first))
    assert chronological(oldest_first) == ordered
    # one day only: the balances decide the order
    same_day = [line(3, "-5", "95"), line(3, "100", "100")]
    assert [x.amount for x in chronological(same_day)] == [Decimal(100), Decimal(-5)]


def test_a_missing_line_breaks_the_running_balance():
    broken = [line(1, "100", "100"), line(2, "-10", "85"), line(3, "-5", "80")]
    assert balance_breaks(broken) == [1]
    with pytest.raises(FinanceError, match="running balance"):
        chronological(broken)
    with pytest.raises(FinanceError, match="neither ascending"):
        chronological([line(1, "1", None), line(3, "1", None), line(2, "1", None)])


def test_printed_totals_must_match_the_lines():
    lines = (line(1, "100", "100"), line(2, "-10", "90"))
    check_totals(Statement("Bank", "xlsx", lines, debit_total=Decimal("-10"), credit_total=Decimal("100")))
    with pytest.raises(FinanceError, match="money-out"):
        check_totals(Statement("Bank", "xlsx", lines, debit_total=Decimal("-11")))
    with pytest.raises(FinanceError, match="money-in"):
        check_totals(Statement("Bank", "xlsx", lines, credit_total=Decimal("99")))


def test_unknown_files_are_refused(tmp_path, fixtures):
    other = tmp_path / "other.html"
    other.write_text("<table><tr><td>Date</td><td>Text</td></tr></table>", encoding="utf-8")
    with pytest.raises(FinanceError, match="known bank"):
        read_statement(other)
    with pytest.raises(FinanceError, match="Unknown bank"):
        read_statement(fixtures / "ziraat_statement.xlsx", bank="other")
    with pytest.raises(FinanceError, match="No bank template"):
        read_statement(tmp_path / "statement.txt")
    broken = tmp_path / "statement.pdf"
    broken.write_bytes(b"not a pdf")
    with pytest.raises(FinanceError, match="Cannot read the PDF"):
        read_statement(broken)


@pytest.fixture
def bank(session):
    create_account(session, "Assets:Bank:Ziraat Checking", kind="checking")
    create_account(session, "Expenses:Food:Groceries")
    create_account(session, "Income:Salary")
    add_rule(session, "migros", "Groceries")
    add_rule(session, "maas", "Salary")
    return session


def tx_count(session):
    return session.scalar(select(func.count()).select_from(Transaction))


def test_import_with_opening_balance_and_balance_check(bank, fixtures):
    s = bank
    st = read_statement(fixtures / "ziraat_statement.xlsx")
    preview = import_statement(s, st, fixtures / "ziraat_statement.xlsx", "Ziraat Checking", dry_run=True, add_opening=True)
    assert not preview.written and tx_count(s) == 0
    assert preview.opening == (date(2026, 9, 1), Decimal("12500.00"))
    assert preview.balance_check.predicted and preview.balance_check.ok

    result = import_statement(s, st, fixtures / "ziraat_statement.xlsx", "Ziraat Checking", add_opening=True)
    assert result.written and result.added == 16 and tx_count(s) == 17  # 16 lines + opening balance
    assert [r.how for r in result.rows][:4] == ["rule #2", "uncategorized", "uncategorized", "rule #1"]
    assert result.unknown_loans == {"12345678-0001": 1}  # no loan with this number is defined
    check = result.balance_check
    assert check.ok and not check.predicted and check.on == date(2026, 9, 30)
    assert account_balance(s, find_account(s, "Ziraat Checking"), date(2026, 9, 30)) == {"TRY": Decimal("35086.32")}
    assert all(v == 0 for v in commodity_totals(s).values())

    # The same statement downloaded as HTML adds nothing: the receipt numbers match.
    again = import_statement(s, read_statement(fixtures / "ziraat_statement.html"), fixtures / "ziraat_statement.html", "Ziraat Checking")
    assert again.added == 0 and again.duplicates == 16 and again.balance_check.ok
    # Two equal BIM purchases on the same day are both kept (different receipt numbers).
    bim = [r for r in result.rows if r.payee == "BIM A.S. U123"]
    assert len(bim) == 2 and not any(r.duplicate for r in bim)


def test_balance_check_reports_a_difference(bank, fixtures):
    st = read_statement(fixtures / "ziraat_statement.xlsx")
    result = import_statement(bank, st, fixtures / "ziraat_statement.xlsx", "Ziraat Checking")
    assert result.written
    assert result.balance_check.difference == Decimal("-12500.00")  # the opening balance is missing


def test_statement_import_rules(bank, fixtures):
    s = bank
    st = read_statement(fixtures / "ziraat_statement.xlsx")
    path = fixtures / "ziraat_statement.xlsx"
    create_account(s, "Liabilities:Credit Card:Bonus", kind="credit_card")
    with pytest.raises(FinanceError, match="asset accounts"):
        import_statement(s, st, path, "Bonus")
    create_account(s, "Assets:FX:USD", kind="fx", commodity="USD")
    with pytest.raises(FinanceError, match="statement is in TRY"):
        import_statement(s, st, path, "Assets:FX:USD")
    opening_balance(s, "Ziraat Checking", Decimal("1"), date(2026, 8, 1))
    with pytest.raises(FinanceError, match="already has transactions"):
        import_statement(s, st, path, "Ziraat Checking", add_opening=True)
