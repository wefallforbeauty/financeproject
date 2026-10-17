"""Credit card lists in PDF: reading the Ziraat Bankası card list and importing it into a card account."""

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import func, select
from typer.testing import CliRunner

from financeproject.cli import app
from financeproject.errors import FinanceError
from financeproject.ingest.banks import read_statement, ziraat_card
from financeproject.ingest.banks.ziraat_card import payee_of, read_description
from financeproject.ingest.importer import Row, import_rows, import_statement
from financeproject.ingest.pdf import TextPiece, lines, read_pdf
from financeproject.ledger.accounts import create_account, find_account
from financeproject.ledger.installments import record_installment_purchase
from financeproject.ledger.models import InstallmentPlan, Transaction
from financeproject.ledger.queries import account_balance, commodity_totals
from financeproject.ledger.transactions import opening_balance

D = Decimal
CARD = "Liabilities:Credit Card:Sample Card"


def test_one_drawing_shown_on_two_pages_is_read_once(fixtures):
    pages = read_pdf(fixtures / "ziraat_card.pdf")
    assert len(pages) == 1
    texts = [" ".join(p.text for p in line) for line in lines(pages[0])]
    assert "Ödeme Teşekkür Ederiz" in texts  # soft hyphens are gone
    assert "DENEME KİTAPEVİ ESKİŞEHİR" in texts  # two pieces, one line


def test_card_list_is_read(fixtures):
    st = read_statement(fixtures / "ziraat_card.pdf")
    assert (st.bank, st.file_format, st.card, st.currency) == ("Ziraat Bankası", "pdf", True, "TRY")
    assert len(st.lines) == 11 and st.start == date(2026, 7, 1) and st.end == date(2026, 7, 28)
    assert [line.date for line in st.lines] == sorted(line.date for line in st.lines)
    kinds = {line.description: line.kind for line in st.lines}
    assert kinds["Ödeme Teşekkür Ederiz"] == "payment"
    assert kinds["5550000001 ÖRNEKTEL FATURA ÖDEME"] == "bill"
    assert kinds["DENEME GİYİM İADE KONYA"] == "refund"
    assert kinds["ÖRNEK ÇOK UZUN ADLI İNTERNET MAĞAZACILIK HİZMETLERİ KONYA"] == "card"  # wrapped onto two lines
    by_payee = {line.payee: line for line in st.lines}
    assert set(by_payee) == {
        "ÖRNEK FIRIN", "ÖRNEK ÇOK UZUN ADLI İNTERNET MAĞAZACILIK HİZMETLERİ", "DENEME GİYİM İADE", "ÖRNEK BEYAZ EŞYA",
        "DENEME KİTAPEVİ", "DENEME ELEKTRONİK", "ÖRNEK MOBİLYA", "ÖRNEKTEL", None, "ÖRNEK MARKET A.Ş.",
    }
    first = by_payee["DENEME ELEKTRONİK"]
    assert (first.kind, first.amount, first.installment, first.installment_count) == ("installment", D("-3150.00"), 1, 3)
    assert (first.purchase_total, first.purchase_date) == (D("9450.00"), date(2026, 7, 15))
    old = by_payee["ÖRNEK BEYAZ EŞYA"]
    assert (old.installment, old.installment_count, old.purchase_date) == (4, 6, date(2026, 4, 12))
    assert by_payee[None].amount == D("7645.30") and by_payee["DENEME KİTAPEVİ"].amount == D("-263.40")
    assert sum(line.amount for line in st.lines) == D("-940.25")


def test_payees_and_kinds():
    assert payee_of("ÖRNEK KAFE KONYA") == "ÖRNEK KAFE"
    assert payee_of("ÖRNEK KAFE KONYA SELCUKLU") == "ÖRNEK KAFE"  # a word after the province
    assert payee_of("03/05 ÖRNEK SHOP 55.SAM SAMSUN") == "ÖRNEK SHOP"
    assert payee_of("SAMSUN") == "SAMSUN"  # nothing would be left
    assert payee_of("ÖRNEK.COM") == "ÖRNEK.COM"
    assert read_description("Ödeme - Teşekkür Ederiz", D(100), False) == ("payment", None)
    assert read_description("12345 FATURA ÖDEME", D(-1), False) == ("bill", None)
    assert read_description("ÖRNEK İADE KONYA", D(5), False) == ("refund", "ÖRNEK İADE")


def piece(x, y, text):
    return TextPiece(x, y, text)


def test_layout_problems_are_reported():
    title = [piece(13, 900, "Kart Hareketleri"), piece(13, 10, "www.ziraatbank.com.tr")]
    block = [piece(519, 800, "12,50"), piece(560, 800, "USD"), piece(115, 774, "SHOP"), piece(54, 765, "3 Eyl"), piece(58, 752, "2026")]
    with pytest.raises(FinanceError, match="only lines in TL"):
        ziraat_card.parse([title + block])
    no_date = [piece(519, 800, "12,50"), piece(560, 800, "TL"), piece(115, 774, "SHOP")]
    with pytest.raises(FinanceError, match="has no date"):
        ziraat_card.parse([title + no_date])
    assert not ziraat_card.sniff([block])
    with pytest.raises(FinanceError, match="Kart Hareketleri"):
        ziraat_card.parse([block])
    with pytest.raises(FinanceError, match="no transactions"):
        ziraat_card.parse([title])
    # a number in a description without a currency next to it is not an amount
    plain = [piece(519, 800, "12,50"), piece(560, 800, "TL"), piece(115, 774, "1,99"), piece(54, 765, "3 Eyl"), piece(58, 752, "2026")]
    assert [line.description for line in ziraat_card.parse([title + plain]).lines] == ["1,99"]


@pytest.fixture
def cards(session):
    create_account(session, "Assets:Bank:Checking", kind="checking")
    create_account(session, CARD, kind="credit_card")
    create_account(session, "Expenses:Home:Furniture")
    opening_balance(session, "Checking", D(50000), date(2026, 6, 1))
    # the furniture bought in June is in the ledger with its plan
    record_installment_purchase(session, D(6870), "Furniture", CARD, date(2026, 6, 17), "Mobilya", 6)
    return session


def test_card_import(cards, fixtures):
    s = cards
    path = fixtures / "ziraat_card.pdf"
    result = import_statement(s, read_statement(path), path, "Sample Card", payments_from="Checking")
    assert result.written and not result.errors
    assert result.added == 10 and result.installment_purchases == 1
    assert result.covered_installments == 1 and result.older_installments == 1
    how = {r.payee: r.how for r in result.rows}
    assert how["ÖRNEK MOBİLYA"].startswith("installment 2/6 of #")
    assert how["DENEME ELEKTRONİK"] == "uncategorized; 3 installments" and how[None] == "card payment"
    plan = s.scalars(select(InstallmentPlan).where(InstallmentPlan.total == D("9450.00"))).one()
    assert (plan.count, plan.first_due, plan.transaction.date) == (3, date(2026, 7, 1), date(2026, 7, 15))
    old = s.scalars(select(Transaction).where(Transaction.payee == "ÖRNEK BEYAZ EŞYA")).one()
    assert old.note == "Installment 4/6 of a purchase of 12810.00 on 2026-04-12"
    assert account_balance(s, find_account(s, "Checking"), date(2026, 7, 31)) == {"TRY": D("42354.70")}
    # June's 6,870 and what the list adds up to, with the 3,150 first installment replaced by the
    # whole 9,450 purchase and the 1,145 installment of June's purchase left out
    owed = D(6870) + D("940.25") - D(3150) + D(9450) - D(1145)
    assert account_balance(s, find_account(s, CARD), date(2026, 7, 31)) == {"TRY": -owed}
    assert all(v == 0 for v in commodity_totals(s).values())

    again = import_statement(s, read_statement(path), path, "Sample Card", payments_from="Checking")
    assert again.added == 0 and again.duplicates == 11
    assert again.covered_installments == 2 and again.older_installments == 0


def test_transfers_seen_from_both_sides_are_booked_once(cards, fixtures):
    s = cards
    path = fixtures / "ziraat_card.pdf"
    import_statement(s, read_statement(path), path, "Sample Card", payments_from="Checking")
    count = s.scalar(select(func.count()).select_from(Transaction))
    # the checking account's statement shows the same payment a day later, sent to the card by a rule
    rows = [
        Row(1, date(2026, 7, 27), "KREDI KARTI ODEMESI", D("-7645.30"), counter_account=CARD),
        Row(2, date(2026, 7, 27), "KREDI KARTI ODEMESI", D("-7645.30"), counter_account=CARD),
    ]
    result = import_rows(s, rows, path=path, source="test", transaction_source="test", account="Checking")
    first, second = result.rows
    assert first.duplicate and first.how.startswith("transfer #")
    assert not second.duplicate  # only one payment was in the ledger
    assert s.scalar(select(func.count()).select_from(Transaction)) == count + 1


def test_card_lists_go_into_card_accounts(cards, fixtures):
    s = cards
    path = fixtures / "ziraat_card.pdf"
    with pytest.raises(FinanceError, match="credit card account"):
        import_statement(s, read_statement(path), path, "Checking")
    with pytest.raises(FinanceError, match="another asset or liability"):
        import_statement(s, read_statement(path), path, "Sample Card", payments_from="Sample Card")
    statement = read_statement(fixtures / "ziraat_statement.xlsx")
    with pytest.raises(FinanceError, match="for credit card lists"):
        import_statement(s, statement, fixtures / "ziraat_statement.xlsx", "Checking", payments_from="Sample Card")


def test_import_command_reads_card_lists(tmp_path, fixtures):
    runner = CliRunner(env={"COLUMNS": "200"})
    db = str(tmp_path / "ledger.sqlite")

    def ok(*args):
        result = runner.invoke(app, ["-d", db, *args])
        assert result.exit_code == 0, result.output
        return result.output

    ok("init")
    ok("account", "add", "Assets:Bank:Checking", "--kind", "checking")
    ok("account", "add", CARD, "--kind", "credit_card")
    pdf = str(fixtures / "ziraat_card.pdf")
    out = ok("import", pdf, "--account", "Sample Card", "--payments-from", "Checking", "--dry-run")
    assert "Ziraat Bankası card list, 2026-07-01 – 2026-07-28, 11 lines" in out and "Dry run" in out
    out = ok("import", pdf, "--account", "Sample Card", "--payments-from", "Checking")
    assert "11 transactions added, 1 installment purchase booked in full with its plan" in out
    assert "2 installments of purchases made before the ledger, booked one by one" in out
    out = ok("import", pdf, "--account", "Sample Card")
    assert "11 were already in" in out and "1 card installment of purchases already in the ledger" in out
    csv = tmp_path / "rows.csv"
    csv.write_text("date,description,amount\n2026-07-01,Shop,-10\n", encoding="utf-8")
    result = runner.invoke(app, ["-d", db, "import", str(csv), "--account", "Checking", "--payments-from", "Checking"])
    assert result.exit_code == 1 and "for credit card lists" in result.output
