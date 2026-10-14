"""The command line interface, end to end on temporary ledgers (no network)."""

import csv
from importlib import resources

import httpx
import pytest
from typer.testing import CliRunner

from financeproject import config
from financeproject.cli import app
from financeproject.prices import http as http_module

runner = CliRunner(env={"COLUMNS": "200"})  # wide enough that tables do not wrap


@pytest.fixture
def db(tmp_path):
    return str(tmp_path / "ledger.sqlite")


def run(db, *args):
    return runner.invoke(app, ["-d", db, *args])


def ok(db, *args):
    result = run(db, *args)
    assert result.exit_code == 0, result.output
    return result.output


def test_full_flow(db, tmp_path):
    assert "created" in ok(db, "init")
    assert "already existed" in ok(db, "init")
    ok(db, "account", "add", "Assets:Bank:Checking", "--kind", "checking", "--owner", "Me", "--institution", "Bank A")
    ok(db, "account", "add", "Assets:FX:USD Account", "--kind", "fx", "--commodity", "USD")
    ok(db, "account", "add", "Liabilities:Credit Card:Bonus", "--kind", "credit_card")
    ok(db, "account", "add", "Expenses:Food:Groceries")
    ok(db, "account", "add", "Income:Salary")
    ok(db, "opening", "Checking", "100,000.00", "--date", "2026-10-01")
    ok(db, "opening", "Bonus", "2500", "--date", "2026-10-01")
    ok(db, "income", "65000", "Salary", "--to", "Checking", "--date", "2026-10-02")
    ok(db, "expense", "450.5", "groceries", "--from", "bonus", "--date", "2026-10-03")
    ok(db, "transfer", "2950.50", "--from", "Checking", "--to", "Bonus", "--date", "2026-10-04")
    out = ok(db, "exchange", "--from", "Checking", "--paid", "49126.70", "--to", "USD Account", "--received", "1000", "--date", "2026-10-08")
    assert "Rate: 1 USD = 49.126700 TRY" in out
    ok(db, "txn", "add", "--description", "By hand", "-p", "Groceries=10", "-p", "Checking", "--date", "2026-10-08")
    ok(db, "price", "add", "USD", "49.1267", "--date", "2026-10-08")
    ok(db, "price", "add", "XAU_G", "130", "--date", "2026-10-08", "--quote", "USD")

    out = ok(db, "balance-sheet", "--date", "2026-10-08")
    assert "Net worth" in out and "Real TRY" in out and "USD" in out and "Grams of gold" in out
    # 100,000 + 65,000 − 2,950.50 − 49,126.70 − 10 = 112,912.80 TRY plus 1,000 USD × 49.1267
    assert "162,039.50" in out

    assert "ok" in ok(db, "status")
    # Account paths are printed as they are: ":Bank:" must not become an emoji
    assert "Assets:Bank:Checking" in ok(db, "account", "list", "--date", "2026-10-08")
    assert "Assets:Bank:Checking" in ok(db, "account", "add", "--help")
    assert "By hand" in ok(db, "txn", "list")
    assert "September 2026" in ok(db, "cpi", "--last", "3")
    ok(db, "networth", "--start", "2026-10-01", "--end", "2026-10-08", "--frequency", "day", "--csv", str(tmp_path / "n.csv"))
    assert (tmp_path / "n.csv").exists()
    ok(db, "chart", "--end", "2026-10-08", "--file", str(tmp_path / "c.png"))
    assert (tmp_path / "c.png").exists()
    assert "Backup" in ok(db, "backup", "--target", str(tmp_path / "backups"))


def test_number_format_setting(db):
    ok(db, "init")
    ok(db, "account", "add", "Assets:Bank:Checking", "--kind", "checking")
    ok(db, "opening", "Checking", "1234567.5", "--date", "2026-10-01")
    assert "1,234,567.50" in ok(db, "account", "list", "--date", "2026-10-01")
    assert "1.234.567,89" in ok(db, "config", "number-format", "1.234,56")
    assert "1.234.567,50" in ok(db, "account", "list", "--date", "2026-10-01")
    assert "1.234,56" in ok(db, "config", "show")
    result = run(db, "config", "number-format", "1 234,56")
    assert result.exit_code == 1


def test_errors_are_short_messages(db):
    result = run(db, "balance-sheet")
    assert result.exit_code == 1 and "finance init" in result.output
    ok(db, "init")
    result = run(db, "expense", "10", "Nothing", "--from", "Checking")
    assert result.exit_code == 1 and "No account matches" in result.output
    result = run(db, "account", "add", "Bank:X")
    assert result.exit_code == 1 and "start with one of" in result.output
    result = run(db, "txn", "add", "--description", "x", "-p", "Expenses:Uncategorized=5", "-p", "Income:Uncategorized=4")
    assert result.exit_code == 1 and "does not balance" in result.output
    result = run(db, "networth", "--frequency", "year")
    assert result.exit_code == 1 and "--frequency" in result.output


def test_import_preview_and_rules(db, tmp_path):
    ok(db, "init")
    ok(db, "account", "add", "Liabilities:Credit Card:Bonus", "--kind", "credit_card")
    ok(db, "account", "add", "Expenses:Food:Groceries")
    path = tmp_path / "s.csv"
    path.write_text("date;description;amount\n2026-10-01;MİGROS;-450,00\n2026-10-02;BİM;-80\n", encoding="utf-8")
    out = ok(db, "import", str(path), "--account", "Bonus", "--dry-run")
    assert "Dry run" in out and "2 new" in out
    out = ok(db, "import", str(path), "--account", "Bonus")
    assert "2 transactions added" in out and "2 uncategorized" in out
    out = ok(db, "import", str(path), "--account", "Bonus")
    assert "0 transactions added" in out and "2 were already in" in out
    ok(db, "rule", "add", "migros", "Groceries")
    assert "migros" in ok(db, "rule", "list")
    assert "1 transactions categorized" in ok(db, "rule", "apply")


def test_price_update_with_mocked_sources(db, monkeypatch):
    text = resources.files("financeproject.data").joinpath("cpi_tcmb_monthly.csv").read_text(encoding="utf-8")
    rows = list(csv.DictReader(text.splitlines()))
    page = "<table>" + "".join(
        f"<tr><td>{r['month'][5:]}-{r['month'][:4]}</td><td>{r['annual_change_pct']}</td><td>{r['monthly_change_pct']}</td></tr>"
        for r in rows
    ) + "</table>"
    real_make_client = http_module.make_client
    monkeypatch.setattr(
        http_module, "make_client", lambda: real_make_client(transport=httpx.MockTransport(lambda r: httpx.Response(200, text=page)))
    )
    ok(db, "init")
    out = ok(db, "price", "update", "--only", "cpi")
    assert "TCMB table" in out


def test_demo_goes_to_a_separate_ledger(tmp_path, monkeypatch):
    out = runner.invoke(app, ["demo", "--synthetic", "--start", "2026-01-01", "--end", "2026-03-31"])
    assert out.exit_code == 0, out.output
    assert config.demo_db_path().exists()
    assert not config.default_db_path().exists()  # the real ledger was not created
    again = runner.invoke(app, ["demo", "--synthetic"])
    assert again.exit_code == 1 and "--overwrite" in again.output
    real = runner.invoke(app, ["-d", str(config.default_db_path()), "demo", "--synthetic"])
    assert real.exit_code == 1 and "real ledger" in real.output


def test_settings_key_file_is_private(tmp_path):
    result = runner.invoke(app, ["config", "evds-key", "abc123"])
    assert result.exit_code == 0, result.output
    path = config.settings_path()
    assert path.stat().st_mode & 0o777 == 0o600
    assert config.evds_api_key() == "abc123"


def test_bank_text_with_brackets_is_not_markup(db, tmp_path):
    ok(db, "init")
    ok(db, "account", "add", "Assets:Bank:Checking", "--kind", "checking")
    ok(db, "expense", "5", "Expenses:Uncategorized", "--from", "Checking", "--description", "[POS] MIGROS [/x] [bold]")
    assert "[POS] MIGROS [/x] [bold]" in ok(db, "txn", "list")


def test_broken_settings_file_is_a_clear_error(db):
    path = config.settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("[evds\napi_key = ", encoding="utf-8")
    result = run(db, "config", "show")
    assert result.exit_code == 1 and "Cannot read the settings file" in result.output


def test_statement_import(db, fixtures, tmp_path):
    ok(db, "init")
    ok(db, "account", "add", "Assets:Bank:Ziraat Checking", "--kind", "checking")
    xlsx, html = str(fixtures / "ziraat_statement.xlsx"), str(fixtures / "ziraat_statement.html")
    result = run(db, "import", xlsx)
    assert result.exit_code == 1 and "--account" in result.output
    out = ok(db, "import", xlsx, "--account", "Ziraat", "--opening", "--dry-run")
    assert "Ziraat Bankası statement" in out and "Dry run" in out and "would be the same" in out
    out = ok(db, "import", xlsx, "--account", "Ziraat", "--opening")
    assert "16 transactions added" in out and "the ledger's is the same" in out and "12,500.00" in out
    assert "Loan 12345678-0001:" in out and "--reference 12345678-0001" in out  # no loan with this number yet
    out = ok(db, "import", html, "--account", "Ziraat")
    assert "0 transactions added" in out and "16 were already in" in out
    csv_path = tmp_path / "s.csv"
    csv_path.write_text("date,description,amount\n2026-10-01,X,-1\n", encoding="utf-8")
    result = run(db, "import", str(csv_path), "--account", "Ziraat", "--opening")
    assert result.exit_code == 1 and "--opening" in result.output


@pytest.fixture
def demo_db(db):
    out = runner.invoke(app, ["-d", db, "demo", "--synthetic", "--start", "2025-10-01", "--end", "2026-09-30"])
    assert out.exit_code == 0, out.output
    return db


def test_reports(demo_db, tmp_path):
    db = demo_db
    out = ok(db, "income-statement", "--start", "2026-07", "--end", "2026-09")
    assert "Total income" in out and "Net saving" in out and "Savings rate" in out
    ok(db, "income-statement", "--start", "2026-07", "--end", "2026-09", "--real", "--csv", str(tmp_path / "i.csv"))
    assert (tmp_path / "i.csv").exists()
    out = ok(db, "cash-flow", "--start", "2026-07", "--end", "2026-09")
    assert "Cash at the start" in out and "Card payments" in out and "Cash at the end" in out
    ok(db, "sankey", "--month", "2026-09", "--file", str(tmp_path / "s.png"))
    assert (tmp_path / "s.png").exists()
    out = ok(db, "ratios", "--date", "2026-09-30")
    assert "Emergency fund" in out and "Greninger" in out
    for unit in ("try", "real", "usd", "gold"):
        out = ok(db, "change", "--month", "2026-09", "--unit", unit)
        assert "Net saving" in out and "Revaluation" in out
    assert "NETFLIX.COM" in ok(db, "recurring", "--date", "2026-09-30")
    assert "Card installments" in ok(db, "upcoming", "--date", "2026-09-30")
    out = ok(db, "installments", "--date", "2026-09-30")
    assert "ARCELIK" in out and "Still to come" in out


def test_categorize_and_recategorize(demo_db):
    db = demo_db
    assert "prequential" in ok(db, "categorize", "--evaluate")
    assert "Dry run" in ok(db, "categorize", "--dry-run")
    assert "categorized" in ok(db, "categorize")
    ok(db, "account", "add", "Expenses:Gifts")
    ok(db, "expense", "300", "Expenses:Uncategorized", "--from", "Sample Card", "--date", "2026-09-29", "--payee", "FLORIST")
    out = ok(db, "txn", "list", "--last", "1")
    number = out.split("│")[1].strip()
    assert "Expenses:Gifts" in ok(db, "txn", "recategorize", number, "Gifts")


def test_installment_expense(db):
    ok(db, "init")
    ok(db, "account", "add", "Liabilities:Credit Card:Bonus", "--kind", "credit_card")
    ok(db, "account", "add", "Expenses:Electronics")
    out = ok(db, "expense", "9000", "Electronics", "--from", "Bonus", "--installments", "3", "--date", "2026-10-05")
    assert "in 3 installments from 2026-11" in out
    result = run(db, "expense", "10", "Electronics", "--from", "Bonus", "--first-due", "2026-11")
    assert result.exit_code == 1 and "--installments" in result.output
    out = ok(db, "installments", "--date", "2026-10-31")
    assert "3,000.00" in out and "0/3" in out
