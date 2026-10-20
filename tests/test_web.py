"""The web interface (``finance ui``): who may use it, every page, the main tasks, the translation.

The application runs in the test process (FastAPI's test client): nothing
listens on a port and nothing goes to the internet. Templates run with
``StrictUndefined``, so a name a page uses but its view does not give fails
here instead of showing as an empty space.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import sqlite3
import stat
import threading
import time
from datetime import date, timedelta
from decimal import Decimal as D
from html import unescape
from importlib import resources
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from jinja2 import StrictUndefined
from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError
from typer.testing import CliRunner

from financeproject import config
from financeproject.cli import app as cli_app
from financeproject.core.dates import add_months, month_start
from financeproject.core.kinds import KINDS
from financeproject.core.recurring import PERIODS
from financeproject.errors import FinanceError
from financeproject.ledger.accounts import create_account, find_account
from financeproject.ledger.db import create_ledger, make_engine, session_scope
from financeproject.ledger.models import Account, Change, ImportBatch, Transaction
from financeproject.ledger.queries import account_balance
from financeproject.ledger.transactions import (
    opening_balance,
    record_expense,
    record_income,
)
from financeproject.web import security, server, shortcut
from financeproject.web.app import ACTION_LABELS, create_app, templates
from financeproject.web.forms import kind_groups
from financeproject.web.i18n import LANGUAGES, Translator, catalog, translator
from financeproject.web.jobs import Jobs
from financeproject.web.views import imports as imports_view
from financeproject.web.views.accounts import SECTIONS
from financeproject.web.views.dashboard import UNITS

TOKEN = "token-for-the-tests-" + "x" * 23
BASE = "http://127.0.0.1:8765"
CSRF = security.csrf_for(TOKEN)
WEB = Path(str(resources.files("financeproject.web")))
TODAY = date.today()
LAST_MONTH = add_months(month_start(TODAY), -1)


@pytest.fixture(autouse=True)
def strict_templates(monkeypatch):
    monkeypatch.setattr(templates.env, "undefined", StrictUndefined)


def make_client(db: Path, *, login: bool = True) -> TestClient:
    client = TestClient(create_app(db, token=TOKEN, port=8765), base_url=BASE)
    if login:
        assert client.get("/login", params={"token": TOKEN}, follow_redirects=False).status_code == 303
    return client


@contextlib.contextmanager
def opened(path: Path):
    engine = make_engine(path)
    try:
        with session_scope(engine) as s:
            yield s
    finally:
        engine.dispose()


@pytest.fixture
def ledger() -> Path:
    """A small household: a checking account, a credit card, a wallet; a salary and some spending last month."""
    path = config.default_db_path()
    create_ledger(path)
    with opened(path) as s:
        create_account(s, "Assets:Bank:Checking", kind="checking", institution="Bank A")
        create_account(s, "Liabilities:Credit Card:Bonus", kind="credit_card")
        create_account(s, "Assets:Cash:Wallet", kind="cash")
        create_account(s, "Expenses:Food:Groceries")
        create_account(s, "Income:Salary")
        start = add_months(LAST_MONTH, -2)
        opening_balance(s, "Checking", D(20000), start)
        opening_balance(s, "Wallet", D(500), start)
        record_income(s, D(40000), "Salary", "Checking", LAST_MONTH, "Salary")
        record_expense(s, D("1250.40"), "Groceries", "Bonus", LAST_MONTH + timedelta(days=2), "MARKET 1234", "Market")
        record_expense(s, D(300), "Expenses:Uncategorized", "Wallet", LAST_MONTH + timedelta(days=4), "KIOSK 99")
    return path


@pytest.fixture
def web(ledger) -> TestClient:
    return make_client(ledger)


def post(client: TestClient, url: str, data: dict | None = None, **kwargs):
    return client.post(url, data={"csrf_token": CSRF, **(data or {})}, follow_redirects=False, **kwargs)


def count(path: Path, model) -> int:
    with opened(path) as s:
        return s.scalar(select(func.count()).select_from(model))


def transaction_id(path: Path, description: str) -> int:
    with opened(path) as s:
        return s.scalar(select(Transaction.id).where(Transaction.description == description))


def hidden_fields(page: str, action: str) -> dict[str, str]:
    """The hidden inputs of the form that posts to ``action``."""
    start = page.index(f'action="{action}"')
    form = page[start : page.index("</form>", start)]
    return {k: unescape(v) for k, v in re.findall(r'<input type="hidden" name="([^"]+)" value="([^"]*)">', form)}


def wait_for_job(client: TestClient, seconds: float = 60.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        job = client.app.state.jobs.current
        if job is not None and not job.running:
            return job
        time.sleep(0.05)
    raise AssertionError("the task did not finish")


# --------------------------------------------------------------------------- who may use it


def test_pages_need_the_login_link(ledger):
    client = make_client(ledger, login=False)
    answer = client.get("/")
    assert answer.status_code == 401 and "finance ui" in answer.text
    assert client.post("/settings/backup", data={"csrf_token": CSRF}).status_code == 401
    assert client.get("/ping").text == "financeproject"
    assert client.get("/static/css/app.css").status_code == 200
    assert client.get("/login", params={"token": "wrong"}).status_code == 401
    answer = client.get("/login", params={"token": TOKEN}, follow_redirects=False)
    assert answer.status_code == 303 and answer.headers["location"] == "/"
    cookie = answer.headers["set-cookie"].lower()
    assert cookie.startswith("fp_session_8765=") and "httponly" in cookie and "samesite=strict" in cookie
    assert client.get("/").status_code == 200


def test_the_login_link_leads_only_to_this_site(ledger):
    client = make_client(ledger, login=False)
    for target, expected in [
        ("/transactions?q=a", "/transactions?q=a"),
        ("//evil.example/", "/"),
        ("/\\evil.example", "/"),
        ("https://evil.example/", "/"),
    ]:
        answer = client.get("/login", params={"token": TOKEN, "next": target}, follow_redirects=False)
        assert answer.headers["location"] == expected, target


def test_other_addresses_are_refused(web):
    assert web.get("/", headers={"host": "evil.example:8765"}).status_code == 400
    assert web.get("/", headers={"host": "127.0.0.1:9999"}).status_code == 400
    assert web.get("/", headers={"host": "localhost:8765"}).status_code == 200


def test_changes_need_the_form_token_and_this_site(web):
    assert web.post("/settings/backup", follow_redirects=False).status_code == 403
    assert web.post("/settings/backup", data={"csrf_token": "x"}, follow_redirects=False).status_code == 403
    assert web.post("/settings/backup", headers={"X-CSRF-Token": CSRF}, follow_redirects=False).status_code == 303
    assert post(web, "/settings/backup").status_code == 303
    assert post(web, "/settings/backup", headers={"Origin": "http://evil.example"}).status_code == 403
    assert post(web, "/settings/backup", headers={"Origin": "null"}).status_code == 403
    assert post(web, "/settings/backup", headers={"Origin": BASE}).status_code == 303


def test_responses_forbid_framing_outside_scripts_and_caching(web):
    answer = web.get("/")
    policy = answer.headers["content-security-policy"]
    assert "script-src 'self'" in policy and "frame-ancestors 'none'" in policy and "unsafe-eval" not in policy
    assert answer.headers["x-frame-options"] == "DENY" and answer.headers["cache-control"] == "no-store"
    # not no-referrer: then browsers send "Origin: null" with form posts, and the origin check refuses them
    assert answer.headers["referrer-policy"] == "same-origin" and '<meta name="referrer" content="same-origin">' in answer.text
    assert "no-store" not in web.get("/static/js/app.js").headers.get("cache-control", "")


def test_templates_have_no_inline_scripts():
    """The Content-Security-Policy would block them, so pages must not need them."""
    for path in sorted((WEB / "templates").glob("*.html")):
        text = path.read_text(encoding="utf-8")
        for tag in re.findall(r"<script\b[^>]*>", text):
            assert "src=" in tag or 'type="application/json"' in tag, (path.name, tag)
        assert not re.search(r"\son[a-z]+\s*=", text), path.name
        assert "javascript:" not in text and "hx-on" not in text and 'style="' not in text, path.name


def test_too_large_requests_are_refused(web, monkeypatch):
    monkeypatch.setattr(security, "MAX_BODY", 1000)
    answer = post(web, "/import/preview", files={"file": ("big.csv", b"x" * 5000, "text/csv")})
    assert answer.status_code == 413


# --------------------------------------------------------------------------- pages


@pytest.mark.parametrize("language", ["en", "tr"])
def test_every_page_renders(web, ledger, language):
    config.save_setting("display", "language", language)
    with opened(ledger) as s:
        transactions = list(s.scalars(select(Transaction.id)))
        accounts = list(s.scalars(select(Account.id)))
    pages = [
        "/", "/transactions", "/transactions?q=market", "/transactions?uncategorized=1", "/transactions?page=2",
        "/transactions/new", "/transactions/new?kind=income", "/transactions/new?kind=transfer",
        "/transactions/new?kind=exchange", "/transactions/new?kind=nonsense", "/import", "/imports", "/categorize",
        "/accounts", "/history", "/settings",
        *[f"/transactions/{n}" for n in transactions], *[f"/accounts/{n}" for n in accounts],
    ]
    for page in pages:
        answer = web.get(page)
        assert answer.status_code == 200, (page, answer.text[:300])
        assert f'<html lang="{language}"' in answer.text, page


def test_the_overview(web):
    page = web.get("/").text
    assert "58,950" in page  # 20,000 + 40,000 + 500 − 300 − 1,250.40 owed on the card
    assert "1 transaction needs a category." in page and 'href="/categorize"' in page
    found = re.search(r'<script type="application/json" id="networth-data">(.*?)</script>', page, re.DOTALL)
    chart = json.loads(found.group(1))
    assert chart["unit"] == "nominal_try" and chart["dates"][-1] == TODAY.isoformat()
    assert chart["units"]["nominal_try"][-1] == pytest.approx(58949.60)
    assert "usd" not in chart["summaries"]  # no dollar price in this ledger
    assert chart["summaries"]["nominal_try"].startswith("Net worth in TRY from ")
    assert 'data-unit-table="nominal_try"' in page  # the same numbers as a table
    assert "40,000" in page  # last month's income


def test_an_empty_ledger_and_errors(ledger):
    web = make_client(ledger)
    assert web.get("/transactions/99999").status_code == 404
    assert "Not found" in web.get("/accounts/99999").text
    missing = web.get("/no/such/page")
    assert missing.status_code == 404 and "<html" in missing.text and "Not found" in missing.text
    icon = make_client(ledger, login=False).get("/favicon.ico")  # browsers ask before logging in
    assert icon.status_code == 200 and icon.headers["content-type"].startswith("image/svg+xml")
    fresh = config.data_dir() / "other.sqlite"
    create_ledger(fresh)
    page = make_client(fresh).get("/").text
    assert "Start by adding your accounts" in page and "The chart appears once there are transactions." in page


def test_the_welcome_page_until_there_is_a_ledger():
    path = config.default_db_path()
    web = make_client(path)
    answer = web.get("/", follow_redirects=False)
    assert answer.status_code == 303 and answer.headers["location"] == "/welcome"
    assert "Create my ledger" in web.get("/welcome").text
    answer = post(web, "/welcome/create")
    assert answer.status_code == 303 and answer.headers["location"] == "/accounts"
    assert path.exists()
    if os.name == "posix":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert "Your ledger is ready" in web.get("/accounts").text
    assert web.get("/welcome", follow_redirects=False).headers["location"] == "/"


def test_a_file_that_is_not_a_ledger():
    path = config.data_dir() / "notes.sqlite"
    path.parent.mkdir(parents=True, exist_ok=True)
    with contextlib.closing(sqlite3.connect(path)) as con:
        con.execute("CREATE TABLE notes (text TEXT)")
        con.commit()
    answer = make_client(path).get("/")
    assert answer.status_code == 400 and "is not a financeproject ledger" in answer.text


def test_a_ledger_made_by_version_03_gets_the_new_tables(ledger):
    with contextlib.closing(sqlite3.connect(ledger)) as con:  # as 03 left it: no history, no import records
        con.executescript("DROP TABLE import_transactions; DROP TABLE imports; DROP TABLE changes;")
    web = make_client(ledger)
    assert web.get("/history").status_code == 200 and web.get("/imports").status_code == 200
    with contextlib.closing(sqlite3.connect(ledger)) as con:
        tables = {row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert {"changes", "imports", "import_transactions"} <= tables


def test_a_busy_ledger(web, monkeypatch):
    from financeproject.web.views import history

    def locked(*args, **kwargs):
        raise OperationalError("SELECT", {}, sqlite3.OperationalError("database is locked"))

    monkeypatch.setattr(history, "recent_changes", locked)
    answer = web.get("/history")
    assert answer.status_code == 503 and "The ledger is busy" in answer.text


def test_numbers_and_accounts_as_pages_show_them():
    from financeproject.web.app import money, percent, route, row_amount

    assert money(D("-1250.4")) == "\u22121,250.40" and money(None) == "—"
    assert (percent(0.063, 1, sign=True), percent(-0.0412, 1), percent(0.5), percent(None)) == ("+6.3%", "\u22124.1%", "50%", "—")
    config.save_setting("display", "language", "tr")
    assert percent(0.063, 1, sign=True) == "+%6.3"
    row = lambda amount, accounts, categories=(): SimpleNamespace(
        amount=D(amount), accounts=accounts, categories=categories, commodity="TRY"
    )
    payment = row("22312.82", ("Liabilities:Credit Card:Bonus", "Assets:Bank:Checking"))
    assert route(payment) == ["Bank:Checking", "Credit Card:Bonus"]  # money out first
    assert row_amount(payment) == "22,312.82"  # a transfer has no sign
    assert row_amount(row("-649", ("Assets:Bank:Checking",), ("Expenses:Bills",))) == "\u2212649.00"
    assert row_amount(row("100", ("Assets:Bank:Checking",), ("Income:Salary",))) == "+100.00"


def test_upcoming_payments_in_the_interface_language():
    from financeproject.reports.recurring import Upcoming
    from financeproject.web.views.dashboard import coming_text

    month = month_start(TODAY)
    card = Upcoming(month, "Card installments (Bonus)", D(100), "TRY", -1, "installments", "due this month", account="Bonus")
    later = Upcoming(add_months(month, 1), "…", D(100), "TRY", -1, "installments", "due this month", account="Bonus")
    loan = Upcoming(TODAY, "Car: installment 3", D(5), "TRY", -1, "loan", "3 of 24", loan="Car", number=3, count=24)
    rent = Upcoming(TODAY, "Landlord", D(9), "TRY", -1, "recurring", "monthly, about", period="monthly", late=True)
    en, tr = translator("en"), translator("tr")
    assert coming_text(card, en, month) == ("Card installments (Bonus)", "due this month")
    assert coming_text(later, en, month)[1] == f"due in {add_months(month, 1):%Y-%m}"
    assert coming_text(loan, en, month) == ("Car: installment 3", "3 of 24")
    assert coming_text(rent, en, month) == ("Landlord", "late; monthly, about")
    assert coming_text(card, tr, month) == ("Kart taksitleri (Bonus)", "bu ay ödenecek")
    assert coming_text(loan, tr, month) == ("Car: 3. taksit", "3 / 24")
    assert coming_text(rent, tr, month) == ("Landlord", "gecikti; aylık, yaklaşık")


# --------------------------------------------------------------------------- transactions


def test_add_edit_and_delete_a_transaction(web, ledger):
    answer = post(web, "/transactions/new", {
        "kind": "expense", "amount": "125,50", "date": TODAY.isoformat(), "category": "Expenses:Food:Groceries",
        "account": "Assets:Cash:Wallet", "description": "Bakery", "payee": "Corner bakery",
    })
    assert answer.status_code == 303
    number = int(answer.headers["location"].rsplit("/", 1)[1])
    page = web.get(f"/transactions/{number}").text
    assert f"Transaction #{number} added." in page and "Corner bakery" in page and "125.50" in page

    answer = post(web, f"/transactions/{number}", {
        "date": TODAY.isoformat(), "description": "Bakery and coffee", "payee": "Corner bakery", "note": "with a friend",
        "amount": "140", "category": "Expenses:Food:Groceries", "account": "Assets:Bank:Checking",
    })
    assert answer.status_code == 303
    page = web.get(f"/transactions/{number}").text
    assert "Saved." in page and "Bakery and coffee" in page and "140.00" in page and "with a friend" in page
    with opened(ledger) as s:
        tx = s.get(Transaction, number)
        assert {p.account.path: p.amount for p in tx.postings} == {"Expenses:Food:Groceries": D(140), "Assets:Bank:Checking": D(-140)}
    post(web, f"/transactions/{number}", {
        "date": TODAY.isoformat(), "description": "Bakery and coffee", "payee": "Corner bakery", "note": "with a friend",
        "amount": "140.00", "category": "Expenses:Food:Groceries", "account": "Assets:Bank:Checking",
    })
    assert "Nothing changed." in web.get(f"/transactions/{number}").text

    answer = post(web, f"/transactions/{number}", {"date": "not a date", "description": "x", "amount": "1"})
    assert answer.status_code == 400 and "Cannot read the date" in answer.text and 'value="not a date"' in answer.text

    answer = post(web, f"/transactions/{number}/delete")
    assert answer.status_code == 303 and answer.headers["location"] == "/transactions"
    assert "A backup was made first" in web.get("/transactions").text
    assert web.get(f"/transactions/{number}").status_code == 404
    assert list(config.backup_dir().glob("*.sqlite"))
    history = web.get("/history").text
    assert history.count(">Edited<") == 1 and ">Deleted<" in history
    with opened(ledger) as s:
        assert {c.action for c in s.scalars(select(Change))} == {"edit", "delete"}


def test_new_transactions_of_every_kind(web, ledger):
    with opened(ledger) as s:
        create_account(s, "Assets:FX:Dollars", kind="fx", commodity="USD")
    answer = post(web, "/transactions/new", {
        "kind": "income", "amount": "2.500,00", "category": "", "new_category": "Bonus", "account": "Assets:Bank:Checking",
    })
    assert answer.status_code == 303
    with opened(ledger) as s:
        assert find_account(s, "Income:Bonus") is not None
    answer = post(web, "/transactions/new", {
        "kind": "expense", "amount": "9000", "category": "Expenses:Food:Groceries", "account": "Liabilities:Credit Card:Bonus",
        "description": "Fridge", "installments": "3", "first_due": add_months(month_start(TODAY), 1).strftime("%Y-%m"),
    })
    assert answer.status_code == 303
    page = web.get(answer.headers["location"]).text
    assert "Installment plan" in page and "1/3" in page and "3,000.00" in page
    assert post(web, "/transactions/new", {
        "kind": "transfer", "amount": "1000", "from": "Assets:Bank:Checking", "to": "Liabilities:Credit Card:Bonus",
    }).status_code == 303
    answer = post(web, "/transactions/new", {
        "kind": "exchange", "from": "Assets:Bank:Checking", "paid": "4100", "to": "Assets:FX:Dollars", "received": "100",
        "description": "Dollars",
    })
    assert answer.status_code == 303
    page = web.get(answer.headers["location"]).text
    assert "only its date, description, payee and note can change" in page
    with opened(ledger) as s:
        assert account_balance(s, find_account(s, "Dollars"), TODAY) == {"USD": D(100)}

    answer = post(web, "/transactions/new", {"kind": "expense", "amount": "", "description": "Lost", "account": "Assets:Cash:Wallet"})
    assert answer.status_code == 400 and "The amount is missing." in answer.text and 'value="Lost"' in answer.text
    answer = post(web, "/transactions/new", {"kind": "expense", "amount": "-5", "category": "Expenses:Food:Groceries", "account": "Assets:Cash:Wallet"})
    assert answer.status_code == 400 and "must be more than zero" in answer.text


def test_amounts_like_ten_thousand_follow_the_number_format(web, ledger):
    post(web, "/settings/display", {"language": "en", "theme": "auto", "number_format": "1.234,56"})
    answer = post(web, "/transactions/new", {
        "kind": "expense", "amount": "10.000", "category": "Expenses:Food:Groceries", "account": "Assets:Bank:Checking",
        "description": "Ten thousand",
    })
    assert answer.status_code == 303
    with opened(ledger) as s:
        tx = s.get(Transaction, transaction_id(ledger, "Ten thousand"))
        assert max(p.amount for p in tx.postings) == D(10000)
    assert "10.000,00" in web.get(answer.headers["location"]).text


def test_search_and_filters(web):
    page = web.get("/transactions", params={"q": "market"}).text
    assert "Market" in page and "KIOSK 99" not in page
    assert "KIOSK 99" in web.get("/transactions", params={"uncategorized": "1"}).text
    assert "1,250.40" in web.get("/transactions", params={"q": "1250,40"}).text
    page = web.get("/transactions", params={"account": "Expenses:Food:Groceries"}).text
    assert "Market" in page and "Salary" not in page.split('id="tx-results"')[1]
    partial = web.get("/transactions", params={"q": "kiosk"}, headers={"HX-Request": "true", "HX-Target": "tx-results"}).text
    assert "<html" not in partial and "KIOSK 99" in partial
    assert "No transactions found" in web.get("/transactions", params={"q": "nothing like this"}).text
    assert web.get("/transactions", params={"start": "not a date"}).status_code == 400


# --------------------------------------------------------------------------- importing


def preview(web, path: Path, **options):
    data = {"learn": "1", **options}
    return post(web, "/import/preview", data, files={"file": (path.name, path.read_bytes(), "application/octet-stream")})


def test_import_a_statement_then_undo_it(web, ledger, fixtures):
    with opened(ledger) as s:
        create_account(s, "Assets:Bank:Ziraat Checking", kind="checking")
    before = count(ledger, Transaction)
    answer = preview(web, fixtures / "ziraat_statement.xlsx", account="Assets:Bank:Ziraat Checking", opening="1")
    assert answer.status_code == 200, answer.text[:500]
    page = answer.text
    assert "Import 16 transactions" in page and "Ziraat Bankası" in page and "matches" in page
    fields = hidden_fields(page, "/import/confirm")
    upload = imports_view.uploads_dir() / fields["upload_id"]
    saved = next(upload.iterdir())
    assert saved.name == "ziraat_statement.xlsx"
    if os.name == "posix":
        assert stat.S_IMODE(saved.stat().st_mode) == 0o600 and stat.S_IMODE(upload.stat().st_mode) == 0o700
    assert count(ledger, Transaction) == before  # a preview writes nothing

    page = post(web, "/import/confirm", fields).text
    assert "16 transactions added." in page and "the ledger&#39;s is the same" in page
    assert "--reference 12345678-0001" in page  # no loan with this number yet
    assert not upload.exists()  # the uploaded copy is gone
    assert count(ledger, Transaction) == before + 17  # and the opening balance
    with opened(ledger) as s:
        batch = s.scalars(select(ImportBatch)).one()
        assert (batch.file_name, batch.added) == ("ziraat_statement.xlsx", 17)  # with the opening balance

    imports = web.get("/imports").text
    assert "ziraat_statement.xlsx" in imports and f'action="/imports/{batch.id}/undo"' in imports
    assert f"Added by import #{batch.id}" in web.get("/transactions", params={"import": batch.id}).text

    again = preview(web, fixtures / "ziraat_statement.html", account="Assets:Bank:Ziraat Checking").text
    assert "Everything in this file is already in the ledger." in again

    answer = post(web, f"/imports/{batch.id}/undo")
    assert answer.status_code == 303
    assert "17 transactions deleted" in web.get("/imports").text
    assert count(ledger, Transaction) == before  # the opening balance it added is gone too
    assert "undone" in web.get("/imports").text


def test_categories_can_be_changed_in_the_preview(web, ledger, tmp_path):
    path = tmp_path / "card.csv"
    path.write_text("date;description;amount\n2026-09-01;BAKERY 12;-45,00\n2026-09-02;BOOKSHOP;-80\n", encoding="utf-8")
    page = preview(web, path, account="Liabilities:Credit Card:Bonus", learn="0").text
    assert "Import 2 transactions" in page and "no category" in page
    fields = hidden_fields(page, "/import/confirm")
    fields["cat_2"] = "Expenses:Food:Groceries"  # line 1 is the header
    page = post(web, "/import/confirm", fields).text
    assert "2 transactions added." in page
    with opened(ledger) as s:
        tx = s.get(Transaction, transaction_id(ledger, "BAKERY 12"))
        assert {p.account.path for p in tx.postings} == {"Expenses:Food:Groceries", "Liabilities:Credit Card:Bonus"}
        other = s.get(Transaction, transaction_id(ledger, "BOOKSHOP"))
        assert "Expenses:Uncategorized" in {p.account.path for p in other.postings}


def test_a_card_list_with_installments(web, ledger, fixtures):
    with opened(ledger) as s:
        create_account(s, "Liabilities:Credit Card:Sample Card", kind="credit_card")
    page = preview(web, fixtures / "ziraat_card.pdf", account="Liabilities:Credit Card:Sample Card",
                   payments_from="Assets:Bank:Checking").text
    assert "3 installments" in page and "card payment" in page
    page = post(web, "/import/confirm", hidden_fields(page, "/import/confirm")).text
    assert "transactions added." in page and "Installment purchases booked in full with their plans" in page


def test_import_mistakes(web, fixtures):
    answer = post(web, "/import/preview", {"account": "Assets:Bank:Checking"})
    assert answer.status_code == 400 and "Choose a file." in answer.text
    answer = preview(web, fixtures / "ziraat_statement.xlsx")
    assert answer.status_code == 400 and "Choose the account the statement belongs to." in answer.text
    assert not any(imports_view.uploads_dir().iterdir())  # nothing is kept
    answer = post(web, "/import/confirm", {"upload_id": "0123456789abcdef"})
    assert answer.status_code == 400 and "upload it again" in answer.text
    assert post(web, "/import/confirm", {"upload_id": "../x"}).status_code == 400

    page = preview(web, fixtures / "ziraat_statement.xlsx", account="Assets:Bank:Checking").text
    fields = hidden_fields(page, "/import/cancel")
    assert post(web, "/import/cancel", fields).headers["location"] == "/import"
    assert not (imports_view.uploads_dir() / fields["upload_id"]).exists()


def test_old_uploads_are_removed():
    folder = imports_view.uploads_dir() / "0123456789abcdef"
    folder.mkdir()
    (folder / "statement.xlsx").write_bytes(b"x")
    assert imports_view.clean_uploads(time.time()) == 0
    assert imports_view.clean_uploads(time.time() + 2 * 24 * 3600) == 1 and not folder.exists()


def test_uploaded_file_names_are_kept_plain():
    assert imports_view.safe_name("../../etc/passwd") == "passwd"
    assert imports_view.safe_name("C:\\Users\\me\\Ekstre Ağustos.pdf") == "Ekstre Ağustos.pdf"
    assert imports_view.safe_name("..") == imports_view.safe_name("") == "statement"
    assert imports_view.safe_name("a;b|c.csv") == "a_b_c.csv"
    long = imports_view.safe_name("x" * 300 + ".xlsx")
    assert len(long) <= 120 and long.endswith(".xlsx")


def test_how_each_row_was_categorized():
    _ = translator("en")
    row = lambda how, duplicate=False: SimpleNamespace(how=how, duplicate=duplicate)
    assert imports_view.how_label(row("uncategorized"), _) == ("no category", "warning")
    assert imports_view.how_label(row("rule #4"), _) == ("rule #4", "")
    assert imports_view.how_label(row("model 0.93"), _) == ("learned, 93%", "")
    assert imports_view.how_label(row("uncategorized; 3 installments"), _) == ("no category · 3 installments", "warning")
    assert imports_view.how_label(row("installment 2/6 of #12", True), _)[0] == "counted with its purchase"
    assert imports_view.how_label(row("transfer #31", True), _)[0] == "already in as #31"
    assert imports_view.how_label(row("rule #4", True), _)[0] == "already in"
    assert imports_view.how_label(row("loan Car #3"), _)[0] == "loan installment"
    assert imports_view.how_label(row("card payment"), _)[0] == "card payment"
    assert imports_view.how_label(row("chosen"), _)[0] == "chosen by you"
    config.save_setting("display", "language", "tr")
    assert imports_view.how_label(row("model 0.93"), translator("tr"))[0] == "öğrenilmiş, %93"


# --------------------------------------------------------------------------- categories


def test_categorize_one_at_a_time_and_with_a_rule(web, ledger):
    with opened(ledger) as s:
        for day, text in [(5, "KIOSK 17"), (6, "BOOKSHOP 3"), (7, "BOOKSHOP 4")]:
            record_expense(s, D(10), "Expenses:Uncategorized", "Wallet", LAST_MONTH + timedelta(days=day), text)
        create_account(s, "Expenses:Books")
    page = web.get("/categorize").text
    assert "4 transactions without a category." in page and "KIOSK 99" in page
    number = transaction_id(ledger, "KIOSK 99")
    answer = web.post(f"/categorize/{number}", data={"account": "Expenses:Food:Groceries"},
                      headers={"HX-Request": "true", "X-CSRF-Token": CSRF})
    assert answer.status_code == 200 and '<tr class="done">' in answer.text and "Food:Groceries" in answer.text
    answer = post(web, "/categorize/rule", {"pattern": "bookshop", "account": "Expenses:Books"})
    assert answer.status_code == 303
    page = web.get("/categorize").text
    assert "Rule #1 added; 2 transactions categorized by the rules." in page
    assert "1 transaction without a category." in page
    number = transaction_id(ledger, "KIOSK 17")
    assert post(web, f"/categorize/{number}", {"account": "Expenses:Food:Groceries"}).headers["location"] == "/categorize"
    assert "Every transaction has a category." in web.get("/categorize").text
    assert post(web, "/categorize/rule", {"pattern": "", "account": "Expenses:Books"}).status_code == 400
    assert post(web, "/categorize/accept").status_code == 303
    assert "0 transactions categorized." in web.get("/categorize").text
    with opened(ledger) as s:
        assert {c.action for c in s.scalars(select(Change))} == {"recategorize"}


def test_a_refund_goes_back_to_its_expense_category(web, ledger):
    with opened(ledger) as s:
        create_account(s, "Expenses:Clothing")
        refund = record_income(s, D(250), "Income:Uncategorized", "Bonus", LAST_MONTH + timedelta(days=9), "REFUND SHOE SHOP")
        number = refund.id
    page = web.get("/categorize").text
    row = page[page.index(f'action="/categorize/{number}"') :]
    row = row[: row.index("</select>")]
    assert row.index('label="Income"') < row.index('label="Expenses"')  # income first for money in
    assert 'value="Expenses:Clothing"' in row  # but the expense categories are there too
    assert post(web, f"/categorize/{number}", {"account": "Expenses:Clothing"}).status_code == 303
    with opened(ledger) as s:
        legs = {p.account.path: p.amount for p in s.get(Transaction, number).postings}
        assert legs == {"Expenses:Clothing": D(-250), "Liabilities:Credit Card:Bonus": D(250)}


# --------------------------------------------------------------------------- accounts


def test_add_and_close_accounts(web, ledger):
    answer = post(web, "/accounts", {"kind": "time_deposit", "name": "Savings", "institution": "Bank B",
                                     "opening": "15.000,00", "opening_date": TODAY.isoformat()})
    assert answer.status_code == 303
    page = web.get("/accounts").text
    assert "Account added: Assets:Bank:Savings" in page and "15,000.00" in page and "Time deposit" in page
    assert post(web, "/accounts", {"kind": "expense", "name": "Food:Coffee"}).status_code == 303
    answer = post(web, "/accounts", {"kind": "checking", "name": ""})
    assert answer.status_code == 400 and "The name is missing." in answer.text
    answer = post(web, "/accounts", {"kind": "checking", "name": "Savings2", "opening": "abc"})
    assert answer.status_code == 400 and "Cannot read the opening balance" in answer.text

    with opened(ledger) as s:
        savings = find_account(s, "Savings").id
        empty = create_account(s, "Assets:Cash:Old Wallet", kind="cash").id
    answer = post(web, f"/accounts/{savings}/close", {"date": TODAY.isoformat()})
    assert answer.status_code == 400  # it still holds money
    answer = post(web, f"/accounts/{empty}/close", {"date": TODAY.isoformat()})
    assert answer.status_code == 303
    page = web.get(f"/accounts/{empty}").text
    assert "Closed: Assets:Cash:Old Wallet" in page and "Close the account" not in page


# --------------------------------------------------------------------------- settings


def test_language_theme_and_number_format(web):
    answer = post(web, "/settings/display", {"language": "tr", "theme": "dark", "number_format": "1.234,56"})
    assert answer.status_code == 303
    page = web.get("/").text
    assert '<html lang="tr" data-theme="dark" data-number-format="1.234,56">' in page
    assert translator("tr")("Net worth") in page and "58.950" in page
    post(web, "/settings/display", {"language": "xx", "theme": "neon", "number_format": "1 234,56"})
    assert (config.language(), config.theme(), config.number_format()) == ("tr", "dark", "1.234,56")
    post(web, "/settings/display", {"language": "en", "theme": "auto", "number_format": "1,234.56"})
    assert '<html lang="en" data-theme="auto"' in web.get("/").text


def test_backups(web):
    assert post(web, "/settings/backup").status_code == 303
    page = web.get("/settings").text
    name = re.search(r'href="/settings/backups/([^"]+)"', page).group(1)
    assert "Backup made: " in page
    download = web.get(f"/settings/backups/{name}")
    assert download.status_code == 200 and download.content.startswith(b"SQLite format 3")
    assert web.get("/settings/backups/nothing.sqlite").status_code == 404
    assert web.get("/settings/backups/..%2Fledger.sqlite").status_code == 404


def test_the_evds_key_is_kept_but_not_shown(web):
    post(web, "/settings/evds", {"key": "secret-key-123"})
    assert config.evds_api_key() == "secret-key-123"
    page = web.get("/settings").text
    assert "EVDS key saved." in page and "secret-key-123" not in page and "Remove the key" in page
    post(web, "/settings/evds", {"key": ""})
    assert config.evds_api_key() is None


def test_switching_to_the_demo_and_back(web, ledger):
    demo = config.demo_db_path()
    assert post(web, "/settings/ledger", {"which": "demo"}).status_code == 400  # not built yet
    create_ledger(demo)
    assert post(web, "/settings/ledger", {"which": "demo"}).status_code == 303
    page = web.get("/").text
    assert 'class="badge demo"' in page and "You are looking at the demo household now." in page
    post(web, "/settings/ledger", {"which": "own"})
    assert 'class="badge demo"' not in web.get("/").text


def test_updating_prices_in_the_background(web, monkeypatch):
    import financeproject.prices.http as http_module
    import financeproject.prices.update as update_module

    def update_all(session, client, start, end, *, api_key=None, today=None, progress=None, **kwargs):
        progress("TCMB exchange rates")
        return SimpleNamespace(added=3, updated=1, messages=["TCMB: 3 days"], warnings=[])

    monkeypatch.setattr(update_module, "update_all", update_all)
    monkeypatch.setattr(http_module, "make_client", lambda *a, **k: contextlib.nullcontext(object()))
    assert post(web, "/settings/prices").status_code == 303
    job = wait_for_job(web)
    assert job.status == "done" and "3 new, 1 updated records." in job.result
    state = web.get("/jobs/current")
    assert "hx-trigger" not in state.text and "TCMB: 3 days" in state.text
    assert "hx-refresh" not in state.headers  # only the page that was waiting reloads
    assert web.get("/jobs/current", headers={"HX-Request": "true"}).headers["hx-refresh"] == "true"


def test_building_the_demo_from_the_interface():
    web = make_client(config.default_db_path())
    assert "Build it with real prices" in web.get("/welcome").text
    assert post(web, "/settings/demo", {"prices": "offline"}).status_code == 303
    job = wait_for_job(web, 120)
    assert job.status == "done", job.error
    assert config.demo_db_path().exists()
    page = web.get("/").text
    assert 'class="badge demo"' in page and "Net worth" in page


def test_tasks_run_one_at_a_time_and_report_failures():
    jobs = Jobs()
    gate = threading.Event()
    first = jobs.start("first", lambda progress: gate.wait(5) and "ok")
    with pytest.raises(FinanceError, match="Another task is running: first"):
        jobs.start("second", lambda progress: "never")
    gate.set()
    deadline = time.monotonic() + 5
    while first.running and time.monotonic() < deadline:
        time.sleep(0.01)
    assert first.status == "done" and first.result == "ok"

    def broken(progress):
        progress("step 1")
        raise ValueError("no network")

    failed = jobs.start("third", broken)
    while failed.running and time.monotonic() < deadline + 5:
        time.sleep(0.01)
    assert (failed.status, failed.error, failed.last) == ("failed", "no network", "step 1")


def test_the_shortcut_from_settings(web, monkeypatch):
    made = [Path("/somewhere/financeproject.desktop")]
    monkeypatch.setattr(shortcut, "create_shortcut", lambda db=None, platform=None: made)
    assert post(web, "/settings/shortcut").status_code == 303
    assert "Shortcut created: " in web.get("/settings").text


def test_stopping_the_interface(web):
    web.app.state.server = SimpleNamespace(should_exit=False)
    answer = post(web, "/quit")
    assert answer.status_code == 200 and "The interface has stopped" in answer.text
    assert web.app.state.server.should_exit


# --------------------------------------------------------------------------- shortcut and server


def test_desktop_shortcut_on_linux(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "share"))
    desktop = tmp_path / "Desktop"
    desktop.mkdir()
    monkeypatch.setattr(shortcut, "_desktop_dir", lambda home: desktop)
    cmd = shortcut.command(tmp_path / "my ledger.sqlite", python="/usr/bin/python3")
    made = shortcut.create_linux(cmd, home=tmp_path)
    assert made == [tmp_path / "share" / "applications" / "financeproject.desktop", desktop / "financeproject.desktop"]
    entry = made[0].read_text(encoding="utf-8")
    assert f'Exec=/usr/bin/python3 -m financeproject --db "{tmp_path / "my ledger.sqlite"}" ui' in entry
    assert "Terminal=false" in entry and Path(entry.split("Icon=")[1].splitlines()[0]).is_file()
    assert stat.S_IMODE(made[0].stat().st_mode) == 0o755
    assert shortcut.command(None, python="py") == ["py", "-m", "financeproject", "ui"]


def test_shortcut_arguments_are_quoted():
    assert shortcut._exec_arg("/usr/bin/python3") == "/usr/bin/python3"
    assert shortcut._exec_arg('/a b/"c"$d') == '"/a b/\\"c\\"\\$d"'
    entry = shortcut.desktop_entry(["/x/python", "-m", "financeproject", "--db", "/y/100%.sqlite", "ui"], Path("/i.svg"))
    assert "Exec=/x/python -m financeproject --db /y/100%%.sqlite ui" in entry


def test_the_desktop_folder(tmp_path, monkeypatch):
    def no_tool(*args, **kwargs):
        raise OSError("xdg-user-dir not found")

    monkeypatch.setattr(shortcut.subprocess, "run", no_tool)
    assert shortcut._desktop_dir(tmp_path) is None  # no Desktop folder
    (tmp_path / "Desktop").mkdir()
    assert shortcut._desktop_dir(tmp_path) == tmp_path / "Desktop"
    monkeypatch.setattr(shortcut.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=0, stdout=f"{tmp_path}\n"))
    assert shortcut._desktop_dir(tmp_path) is None  # some systems answer the home folder itself


def test_the_server_state_file():
    running = server.Running(8765, "tok", os.getpid(), "/x.sqlite")
    server.write_state(running)
    assert server.read_state() == running and running.login_url == "http://127.0.0.1:8765/login?token=tok"
    if os.name == "posix":
        assert stat.S_IMODE(server.state_file().stat().st_mode) == 0o600
    server.clear_state(pid=os.getpid() + 1)  # another server's
    assert server.read_state() == running
    server.clear_state(pid=os.getpid())
    assert server.read_state() is None
    server.state_file().write_text("not json", encoding="utf-8")
    assert server.read_state() is None


def test_a_port_in_use_is_not_offered():
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as busy:
        if os.name == "posix":
            busy.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)  # as servers do
        busy.bind(("127.0.0.1", 0))
        busy.listen()
        port = busy.getsockname()[1]
        assert server.free_port(port) != port
    assert server.free_port(port) == port


def test_stop_asks_the_server_to_quit_and_waits(monkeypatch):
    running = server.Running(8765, "tok", 1, "/x.sqlite")
    server.write_state(running)
    answers = iter([True, True, True, False])
    monkeypatch.setattr(server, "alive", lambda r, timeout=1.0: next(answers))
    sent = []
    monkeypatch.setattr(server.httpx, "post", lambda url, **kw: sent.append((url, kw["headers"], kw["cookies"])))
    assert server.stop() is True
    url, headers, cookies = sent[0]
    assert url == "http://127.0.0.1:8765/quit" and headers["X-CSRF-Token"] == security.csrf_for("tok")
    assert headers["Origin"] == "http://127.0.0.1:8765" and cookies == {"fp_session_8765": "tok"}
    assert next(answers, "used up") == "used up"  # it waited until the server stopped answering


def test_serve_starts_the_server_once(monkeypatch, ledger):
    import uvicorn

    seen = {}

    class FakeServer:
        def __init__(self, config):
            seen["config"] = config
            self.started, self.should_exit = True, False

        def run(self):
            seen["state"] = server.read_state()

    opened_urls = []
    monkeypatch.setattr(uvicorn, "Server", FakeServer)
    monkeypatch.setattr(server, "free_port", lambda preferred: 54321)
    monkeypatch.setattr(server.webbrowser, "open", opened_urls.append)
    lines: list[str] = []
    server.serve(ledger, open_browser=False, echo=lines.append)
    assert (seen["config"].host, seen["config"].port) == ("127.0.0.1", 54321)
    assert seen["state"].port == 54321 and seen["state"].ledger == str(ledger)
    assert server.read_state() is None  # cleared when the server stopped
    assert "http://127.0.0.1:54321" in lines[0] and "/login?token=" in lines[1] and not opened_urls

    already = server.Running(1234, "tok", 1, str(ledger))
    server.write_state(already)
    monkeypatch.setattr(server, "alive", lambda running, timeout=1.0: True)
    seen.clear()
    server.serve(ledger, open_browser=True, echo=lines.append)
    assert "already running" in lines[-1] and opened_urls == [already.login_url] and not seen


def test_ui_command(monkeypatch, tmp_path):
    runner = CliRunner()
    monkeypatch.setattr(server, "alive", lambda running, timeout=1.0: False)
    result = runner.invoke(cli_app, ["-d", str(tmp_path / "l.sqlite"), "ui", "--stop"])
    assert result.exit_code == 0 and "The interface was not running." in result.output
    made = []
    monkeypatch.setattr(shortcut, "create_shortcut", lambda db=None, platform=None: made.append(db) or [tmp_path / "x.desktop"])
    result = runner.invoke(cli_app, ["-d", str(tmp_path / "l.sqlite"), "ui", "--shortcut"])
    assert result.exit_code == 0 and "Shortcut:" in result.output and made == [tmp_path / "l.sqlite"]
    started = []
    monkeypatch.setattr(server, "serve", lambda db, **kwargs: started.append((db, kwargs["port"], kwargs["open_browser"])))
    result = runner.invoke(cli_app, ["-d", str(tmp_path / "l.sqlite"), "ui", "--no-browser", "--port", "9000"])
    assert result.exit_code == 0 and started == [(tmp_path / "l.sqlite", 9000, False)]


# --------------------------------------------------------------------------- 04b: reports, debts, investments, rules


@pytest.fixture(scope="session")
def demo_file(tmp_path_factory) -> Path:
    """The made-up demo household with made-up prices, built once; tests use copies of it."""
    from financeproject.demo import create_demo_ledger

    path = tmp_path_factory.mktemp("demo") / "demo.sqlite"
    create_demo_ledger(path, add_months(month_start(TODAY), -12), TODAY, synthetic=True)
    return path


@pytest.fixture
def demo_web(demo_file) -> TestClient:
    import shutil

    path = config.data_dir() / "demo-copy.sqlite"
    path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(demo_file, path)
    return make_client(path)


REPORT_PAGES = [
    "/reports/balance-sheet", "/reports/income", "/reports/income?real=1&depth=1&months=3", "/reports/cash-flow",
    "/reports/cash-flow?real=1", "/reports/sankey", "/reports/change", "/reports/change?unit=usd", "/reports/change?unit=real",
    "/reports/ratios", "/reports/recurring", "/reports/recurring?all=1", "/debts", "/debts/loans/new", "/debts/installments",
    "/debts/payoff", "/debts/calculators", "/investments", "/investments/risk?window=60", "/investments/concentration",
    "/rules",
]


@pytest.mark.parametrize("language", ["en", "tr"])
def test_every_report_page_renders(demo_web, language):
    config.save_setting("display", "language", language)
    assert demo_web.get("/reports", follow_redirects=False).headers["location"] == "/reports/balance-sheet"
    loan = demo_web.get("/debts").text
    loans = sorted(set(re.findall(r'href="/debts/loans/(\d+)"', loan)))
    assert loans, "the demo has a loan"
    for page in [*REPORT_PAGES, *(f"/debts/loans/{n}" for n in loans)]:
        answer = demo_web.get(page)
        assert answer.status_code == 200, (page, answer.text[:300])
        assert f'<html lang="{language}"' in answer.text, page
    if language == "tr":
        assert "Acil durum fonu" in demo_web.get("/reports/ratios").text


def test_reports_on_a_small_ledger(web):
    page = web.get("/reports/balance-sheet").text
    assert "58,950" in page and "Bank:Checking" in page and "Credit Card:Bonus" in page and "1,250.40" in page
    assert "Liquid net assets" in page
    page = web.get("/reports/income", params={"start": LAST_MONTH.strftime("%Y-%m"), "end": LAST_MONTH.strftime("%Y-%m")}).text
    chart = json.loads(re.search(r'<script type="application/json" id="income-data">(.*?)</script>', page, re.DOTALL).group(1))
    assert chart["categories"] == [LAST_MONTH.strftime("%Y-%m")]
    assert [s["values"] for s in chart["series"]] == [[40000.0], [1550.4]]
    assert "Food" in page and "40,000" in page and "+38,450" in page  # net saving
    page = web.get("/reports/sankey", params={"month": LAST_MONTH.strftime("%Y-%m")}).text
    data = json.loads(re.search(r'<script type="application/json" id="sankey-data">(.*?)</script>', page, re.DOTALL).group(1))
    assert [n["label"] for n in data["left"]] == ["Salary"] and data["budget"]["value"] == 40000.0
    assert {n["label"]: n["role"] for n in data["right"]}["Saved"] == "savings"
    page = web.get("/reports/change", params={"month": LAST_MONTH.strftime("%Y-%m")}).text
    assert "+40,000" in page and "Net saving" in page
    assert web.get("/reports/income", params={"start": "2026-09", "end": "2026-03"}).status_code == 400
    assert web.get("/reports/change", params={"unit": "euro"}).status_code == 400
    assert web.get("/reports/income", params={"start": "not a month"}).status_code == 400


def test_add_pay_and_delete_a_loan(web, ledger):
    first_due = add_months(month_start(TODAY), 1) + timedelta(days=14)
    answer = post(web, "/debts/loans/new", {
        "name": "Car loan", "new_account": "Liabilities:Loans:Car", "kind": "vehicle_loan", "principal": "120000",
        "count": "24", "first_due": first_due.isoformat(), "terms": "rate", "rate": "2.99", "method": "annuity",
        "start": "new", "paid_into": "Assets:Bank:Checking", "reference": "99-1",
    })
    assert answer.status_code == 303, answer.text[:500]
    page = web.get(answer.headers["location"]).text
    assert "Loan added: Car loan" in page and "Payment plan" in page and "Annual cost rate" in page and ">24<" in page
    number = int(answer.headers["location"].rsplit("/", 1)[1])
    with opened(ledger) as s:
        assert account_balance(s, find_account(s, "Liabilities:Loans:Car"), TODAY) == {"TRY": D(-120000)}
    answer = post(web, f"/debts/loans/{number}/pay", {"from": "Assets:Bank:Checking", "date": TODAY.isoformat()})
    assert answer.status_code == 303
    page = web.get(f"/debts/loans/{number}").text
    assert "Payment #" in page and "interest and taxes" in page and "paid" in page
    assert "Car loan" in web.get("/debts").text
    answer = post(web, f"/debts/loans/{number}/delete")
    assert answer.headers["location"] == "/debts" and list(config.backup_dir().glob("*.sqlite"))
    assert "The terms of Car loan were deleted" in web.get("/debts").text
    page = web.get("/debts").text
    assert "Car loan" not in page and "Loans:Car" in page  # the terms are gone, the account and its debt stay
    with opened(ledger) as s:
        assert find_account(s, "Liabilities:Loans:Car") is not None


def test_a_loan_from_its_installment_and_form_mistakes(web):
    first_due = add_months(month_start(TODAY), 1)
    base = {"name": "Old loan", "principal": "100000", "count": "24", "first_due": first_due.isoformat(), "start": "ledger"}
    answer = post(web, "/debts/loans/new", {**base, "terms": "rate", "rate": "3"})
    assert answer.status_code == 400 and "Choose the loan&#39;s account" in answer.text and 'value="Old loan"' in answer.text
    answer = post(web, "/debts/loans/new", {**base, "new_account": "Loans:Old", "terms": "rate", "rate": "3"})
    assert answer.status_code == 400 and "full path" in answer.text
    answer = post(web, "/debts/loans/new", {**base, "new_account": "Liabilities:Loans:Old", "terms": "installment",
                                            "installment": "6924.20", "method": "equal-principal"})
    assert answer.status_code == 400 and "equal installments only" in answer.text
    answer = post(web, "/debts/loans/new", {**base, "new_account": "Liabilities:Loans:Old", "terms": "installment", "installment": "6924.20"})
    assert answer.status_code == 303
    page = web.get(answer.headers["location"]).text
    assert "3.490%" in page and "4.537%" in page  # the rate worked out from the installment, without and with the taxes


def test_paying_off_and_the_calculators(web, ledger):
    post(web, "/debts/loans/new", {
        "name": "Car loan", "new_account": "Liabilities:Loans:Car", "kind": "vehicle_loan", "principal": "50000",
        "count": "12", "first_due": (add_months(month_start(TODAY), 1)).isoformat(), "terms": "rate", "rate": "3",
        "start": "new", "paid_into": "Assets:Bank:Checking",
    })
    page = web.get("/debts/payoff").text
    assert "Give a monthly budget" in page and 'name="rate:Liabilities:Credit Card:Bonus"' in page
    page = web.get("/debts/payoff", params={"budget": "9000", "rate:Liabilities:Credit Card:Bonus": "4.25",
                                            "min:Liabilities:Credit Card:Bonus": "500"}).text
    assert "Avalanche: highest rate first" in page and "Snowball: smallest debt first" in page and "months" in page
    answer = web.get("/debts/payoff", params={"budget": "100"})
    assert answer.status_code == 400 and "does not cover the least payments" in answer.text
    # İş Bankası's worked example of the annual cost rate: 13.75 %
    page = web.get("/debts/calculators", params={"calc": "offer", "amount": "10000", "count": "12", "mode": "installment",
                                                 "installment": "888.49", "last": "888.47", "fees": "50", "first_in": "1"}).text
    assert "13.75%" in page
    page = web.get("/debts/calculators", params={"calc": "cash", "cash": "34000", "installment": "6000", "count": "6",
                                                 "first_in": "1", "rate": "2.5"}).text
    assert "The installments are cheaper" in page and "33,048.75" in page
    assert web.get("/debts/calculators", params={"calc": "offer", "amount": "1000", "count": "0"}).status_code == 400


def test_card_installments_page(web, ledger):
    from financeproject.ledger.installments import record_installment_purchase

    with opened(ledger) as s:
        record_installment_purchase(s, D(9000), "Groceries", "Bonus", LAST_MONTH + timedelta(days=3), "Fridge", 3)
    page = web.get("/debts/installments").text
    assert "Fridge" in page and "3,000.00" in page and "Due in the coming months" in page


def test_rules_from_the_interface(web, ledger):
    assert "No rules yet" in web.get("/rules").text
    answer = post(web, "/rules", {"pattern": "kiosk", "account": "Expenses:Food:Groceries", "priority": "5"})
    assert answer.status_code == 303
    page = web.get("/rules").text
    assert "Rule #1: &#39;kiosk&#39; → Expenses:Food:Groceries" in page and "<code>kiosk</code>" in page
    assert post(web, "/rules/apply").headers["location"] == "/rules"
    assert "1 transaction categorized by the rules." in web.get("/rules").text
    with opened(ledger) as s:
        tx = s.get(Transaction, transaction_id(ledger, "KIOSK 99"))
        assert "Expenses:Food:Groceries" in {p.account.path for p in tx.postings}
    assert post(web, "/rules", {"pattern": "(unclosed", "account": "Expenses:Food:Groceries", "regex": "1"}).status_code == 400
    assert post(web, "/rules", {"pattern": "x", "account": "Expenses:Food:Groceries", "priority": "high"}).status_code == 400
    assert post(web, "/rules/1/delete").status_code == 303
    assert "Rule #1 deleted." in web.get("/rules").text and "No rules yet" in web.get("/rules").text
    assert 'href="/rules"' in web.get("/categorize").text  # the review links to the rules


def test_investments_with_and_without_investment_accounts(web, demo_web):
    page = web.get("/investments").text
    assert "No investment accounts" in page
    page = demo_web.get("/investments").text
    assert "Time-weighted" in page and "Money-weighted a year" in page and "Real TRY" in page
    page = demo_web.get("/investments", params={"account": ["Assets:Investments"]}).text
    assert "Investments:Demo Fund" in page and "Gold:Gram Gold" not in page.split("<tbody>", 1)[1]
    page = demo_web.get("/investments/risk").text
    assert "Volatility a year" in page and "Expected shortfall" in page
    page = demo_web.get("/investments/concentration", params={"threshold": "10"}).text
    assert "Herfindahl–Hirschman" in page and "in one" in page and "Deposit insurance" in page


def test_correlation_matrix_and_inflation_source():
    from financeproject.reports.debt import Inflation
    from financeproject.web.views.debts import inflation_source
    from financeproject.web.views.investments import correlation_matrix

    rows = correlation_matrix({("A", "B"): 0.5, ("A", "C"): None, ("B", "C"): -0.25})
    assert [name for name, _ in rows] == ["A", "B", "C"]
    assert rows[0][1] == [1.0, 0.5, None] and rows[2][1] == [None, -0.25, 1.0]
    tr = translator("tr")
    cpi = Inflation(0.297, "CPI, 12 months to 2026-09", date(2026, 9, 1))
    assert inflation_source(cpi, tr) == "TÜFE, 2026-09 itibarıyla son 12 ay"
    assert inflation_source(cpi, translator("en")) == "CPI, 12 months to 2026-09"
    assert inflation_source("given", tr) == inflation_source(Inflation(0.2, "given"), tr) == "girilen"


def test_concentration_flags_in_turkish():
    from financeproject.reports.investments import InsuredDeposit, Share
    from financeproject.web.views.investments import concentration_flags

    config.save_setting("display", "language", "tr")
    report = SimpleNamespace(
        by_holding=[Share("DEMO.FUND", D(70), 0.7)], by_institution=[Share("Bank A", D(70), 0.7)], by_currency=[],
        currencies=frozenset(), threshold=0.5, deposits=[InsuredDeposit("shared", "Bank A", D(1500000), D(1200000))],
    )
    flags = concentration_flags(report, translator("tr"))
    assert flags[0] == "Tek bir varlıkta %70: DEMO.FUND" and flags[1].startswith("Tek bir kurumda %70")
    assert "300.000,00" not in flags[2] and "300,000.00" in flags[2]  # the number format, not the language


# --------------------------------------------------------------------------- translation and bundled files

CALL = re.compile(
    r"""_(\.n)?\(\s*(?:"((?:[^"\\]|\\.)*)"|'((?:[^'\\]|\\.)*)')(?:\s*,\s*(?:"((?:[^"\\]|\\.)*)"|'((?:[^'\\]|\\.)*)'))?"""
)


def _literal(text: str) -> str:
    return text.replace("\\'", "'").replace('\\"', '"')


def interface_texts() -> tuple[set[str], set[str]]:
    """Every English text of the interface (the ``_()`` calls, and the labels translated through a
    variable), and of those the singular forms of ``_.n()``, which always get ``{n}``."""
    texts: set[str] = set()
    singular: set[str] = set()
    files = [*(WEB / "templates").glob("*.html"), *(WEB / "views").glob("*.py"), WEB / "app.py"]
    for path in files:
        for m in CALL.finditer(path.read_text(encoding="utf-8")):
            plural, *strings = m.groups()
            first = _literal(strings[0] if strings[0] is not None else strings[1])
            texts.add(first)
            if plural:
                singular.add(first)
                texts.add(_literal(strings[2] if strings[2] is not None else strings[3]))
    texts |= {kind.label for kind in KINDS.values()}
    texts |= {group for group, _ in kind_groups()} | {label for _, kinds in kind_groups() for _, label in kinds}
    texts |= {title for _, title in SECTIONS} | {label for _, label in UNITS} | set(ACTION_LABELS.values())
    texts |= {period.name for period in PERIODS}
    from financeproject.reports.context import SOURCE_LABELS
    from financeproject.web.views import debts, investments, reports, rules

    for module in (reports, debts, investments, rules):
        texts |= set(module.TEXTS)
    texts |= set(SOURCE_LABELS.values())
    return texts, singular


def test_the_turkish_translation_is_complete():
    pairs = json.loads((WEB / "locales" / "tr.json").read_text(encoding="utf-8"), object_pairs_hook=list)
    keys = [k for k, _ in pairs]
    assert len(keys) == len(set(keys)), "a text is in the file twice"
    tr = dict(pairs)
    texts, singular = interface_texts()
    assert sorted(texts - tr.keys()) == [], "texts without a translation"
    assert sorted(tr.keys() - texts) == [], "translations of texts no page uses"
    fields = re.compile(r"\{(\w*)\}")
    for english, turkish in tr.items():
        assert turkish.strip() and turkish == turkish.strip(), english
        en, tr_ = set(fields.findall(english)), set(fields.findall(turkish))
        if english in singular:  # "Accept the suggestion …" may say the count in Turkish
            assert en <= tr_ <= en | {"n"}, english
        else:
            assert en == tr_, english
    assert set(LANGUAGES) == {"en", "tr"}


def test_plural_forms():
    _ = translator("en")
    assert isinstance(_, Translator)
    assert _.n("{n} line", "{n} lines", 1) == "1 line" and _.n("{n} line", "{n} lines", 3) == "3 lines"
    assert _.n("Import #{n}: {count} row", "Import #{n}: {count} rows", 2, n=7, count=2) == "Import #7: 2 rows"
    turkish = catalog("tr")
    assert turkish["{n} line"] == turkish["{n} lines"]  # Turkish uses one form after a number
    assert translator("tr").n("{n} line", "{n} lines", 4) == turkish["{n} lines"].format(n=4)
    assert translator("xx")("Overview") == "Overview"  # an unknown language stays English


def test_bundled_libraries_match_their_recorded_hashes():
    vendor = WEB / "static" / "vendor"
    recorded = dict(re.findall(r"\| `([\w.]+\.js)` \|.*\| `([0-9a-f]{64})` \|", (vendor / "VENDOR.md").read_text(encoding="utf-8")))
    assert set(recorded) == {"htmx.min.js", "echarts.min.js"}
    for name, digest in recorded.items():
        assert hashlib.sha256((vendor / name).read_bytes()).hexdigest() == digest, name
    for name in ("htmx.LICENSE", "echarts.LICENSE", "echarts.NOTICE"):
        assert (vendor / name).stat().st_size > 0
