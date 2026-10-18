"""A made-up household for trying the program and for tests. No real data.

``build_demo`` writes accounts, opening balances and about thirty transactions a
month:

- salary and time deposit interest;
- rent, utility bills, a phone and an internet subscription, two streaming
  subscriptions (one gets more expensive halfway);
- card purchases at shops with generic names (no real chains), two of them in
  installments (a laptop in 6, a washing machine in 9);
- card payments (the month's purchases plus the installments due), private
  pension contributions, savings in dollars and gold;
- a consumer loan taken before the ledger starts (its terms are recorded, so
  every installment splits into principal, interest and taxes), and money
  borrowed from a friend, paid back in monthly parts;
- a fund bought every month and a share bought every third month, at made-up
  prices; the share splits two for one in the seventh month.

In the last two months a third of the card purchases stay uncategorized, as if
imported without rules, so that ``finance categorize`` has something to learn.
Nominal amounts grow with the CPI when it is loaded, otherwise 2.5 % a month.
``synthetic_prices`` gives offline USD/TRY and gold prices; the fund's and the
share's prices are always made up (``security_prices``).
"""

from __future__ import annotations

import math
import random
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import ROUND_FLOOR, Decimal
from pathlib import Path

from sqlalchemy.orm import Session

from financeproject.core.dates import add_months, month_end, month_start
from financeproject.core.inflation import Deflator
from financeproject.core.loans import same_day_months_later
from financeproject.core.money import quantum
from financeproject.core.prices import PriceBook, Quote
from financeproject.ledger.accounts import create_account
from financeproject.ledger.db import UNCATEGORIZED_EXPENSE
from financeproject.ledger.installments import record_installment_purchase, split_amount
from financeproject.ledger.loans import add_loan, next_number, record_payment
from financeproject.ledger.models import Commodity
from financeproject.ledger.transactions import (
    opening_balance,
    record_exchange,
    record_expense,
    record_income,
    record_split,
    record_transfer,
)
from financeproject.prices.store import upsert_price

SYNTHETIC_SOURCE = "synthetic"
CARD = "Liabilities:Credit Card:Sample Card"
CHECKING = "Assets:Bank:Checking TRY"
LOAN = "Liabilities:Loan:Consumer Loan"
FRIEND = "Liabilities:Personal:Borrowed from a Friend"
FUND, SHARE = "DEMO.FUND", "DEMO.SHARE"
FUND_ACCOUNT, SHARE_ACCOUNT = "Assets:Investments:Demo Fund", "Assets:Investments:Demo Share"
SPLIT_MONTH = 6
"""The share splits in the seventh month (months counted from 0)."""

ACCOUNTS = (
    # path, kind, commodity, owner, institution
    (CHECKING, "checking", "TRY", "Me", "Sample Bank"),
    ("Assets:Bank:Time Deposit TRY", "time_deposit", "TRY", "Me", "Sample Bank"),
    ("Assets:FX:USD Account", "fx", "USD", "Me", "Sample Bank"),
    ("Assets:Gold:Gram Gold", "gold", "XAU_G", "Me", "Sample Bank"),
    ("Assets:Cash:Wallet", "cash", "TRY", "Me", None),
    ("Assets:Pension:BES", "pension", "TRY", "Me", "Sample Pension"),
    ("Assets:Bank:Dad's Checking", "checking", "TRY", "Dad", "Other Bank"),
    (CARD, "credit_card", "TRY", "Me", "Sample Bank"),
    (LOAN, "consumer_loan", "TRY", "Me", "Sample Bank"),
    (FRIEND, "personal_debt", "TRY", "Me", None),
    (FUND_ACCOUNT, "fund", FUND, "Me", "Sample Broker"),
    (SHARE_ACCOUNT, "stock", SHARE, "Me", "Sample Broker"),
)
CATEGORIES = (
    "Expenses:Rent",
    "Expenses:Food:Groceries",
    "Expenses:Food:Restaurants",
    "Expenses:Bills:Electricity",
    "Expenses:Bills:Natural Gas",
    "Expenses:Bills:Phone",
    "Expenses:Bills:Internet",
    "Expenses:Subscriptions",
    "Expenses:Transport",
    "Expenses:Clothing",
    "Expenses:Health",
    "Expenses:Home",
    "Expenses:Electronics",
    "Expenses:Interest and Fees",
    "Income:Salary",
    "Income:Interest",
)
OPENING = (
    (CHECKING, "40000"),
    ("Assets:Bank:Time Deposit TRY", "150000"),
    ("Assets:FX:USD Account", "1500"),
    ("Assets:Gold:Gram Gold", "20"),
    ("Assets:Cash:Wallet", "3000"),
    ("Assets:Pension:BES", "60000"),
    ("Assets:Bank:Dad's Checking", "25000"),
    (CARD, "8000"),
    (FRIEND, "20000"),
)
SHOPS = (
    # category, base amount, shops (weights: groceries twice)
    ("Expenses:Food:Groceries", 2200, ("SUPERMARKET", "DISCOUNT MARKET", "NEIGHBORHOOD MARKET", "GREENGROCER")),
    ("Expenses:Food:Groceries", 2200, ("SUPERMARKET", "DISCOUNT MARKET", "NEIGHBORHOOD MARKET", "GREENGROCER")),
    ("Expenses:Food:Restaurants", 1100, ("KEBAB HOUSE", "BAKERY CAFE", "BURGER RESTAURANT")),
    ("Expenses:Transport", 800, ("GAS STATION", "FUEL STATION", "TRANSIT CARD TOP-UP")),
    ("Expenses:Clothing", 2500, ("CLOTHING STORE", "FASHION OUTLET", "SHOE SHOP")),
    ("Expenses:Health", 1500, ("CITY PHARMACY", "CORNER PHARMACY")),
    ("Expenses:Home", 2000, ("HARDWARE STORE", "FURNITURE STORE", "HOME DECOR")),
)
SUBSCRIPTIONS = (
    # payee, category, first price, later price (from the 7th month), day
    ("VIDEO STREAMING", "Expenses:Subscriptions", "229.99", "279.99", 7),
    ("MUSIC STREAMING", "Expenses:Subscriptions", "99.99", "99.99", 12),
)
INSTALLMENT_PURCHASES = (
    # month index, day, payee, category, base amount, installments
    (2, 14, "ELECTRONICS STORE", "Expenses:Electronics", 36000, 6),
    (7, 22, "APPLIANCE STORE", "Expenses:Home", 24000, 9),
)


@dataclass
class DemoSummary:
    start: date
    end: date
    transactions: int
    skipped_exchanges: int


def synthetic_prices(start: date, end: date, seed: int = 7) -> list[Quote]:
    """Daily USD/TRY and gold (USD per gram) on weekdays: smooth drift plus noise."""
    rng = random.Random(seed)
    quotes: list[Quote] = []
    usd, gold = 35.0, 85.0
    d = start
    while d <= end:
        if d.weekday() < 5:
            usd *= math.exp(0.0009 + rng.gauss(0, 0.003))
            gold *= math.exp(0.0006 + rng.gauss(0, 0.009))
            quotes.append(Quote("USD", "TRY", d, Decimal(f"{usd:.4f}"), SYNTHETIC_SOURCE))
            quotes.append(Quote("XAU_G", "USD", d, Decimal(f"{gold:.6f}"), SYNTHETIC_SOURCE))
        d += timedelta(days=1)
    return quotes


def security_prices(start: date, end: date, split_on: date | None, seed: int = 7) -> list[Quote]:
    """Daily prices of the demo fund and share on weekdays; the share's price halves at its split."""
    rng = random.Random(seed + 1)
    quotes: list[Quote] = []
    fund, share = 1.0, 80.0
    d = start
    while d <= end:
        if d.weekday() < 5:
            fund *= math.exp(0.0009 + rng.gauss(0, 0.006))
            share *= math.exp(0.0007 + rng.gauss(0, 0.018))
            raw = share / 2 if split_on and d >= split_on else share
            quotes.append(Quote(FUND, "TRY", d, Decimal(f"{fund:.6f}"), SYNTHETIC_SOURCE))
            quotes.append(Quote(SHARE, "TRY", d, Decimal(f"{raw:.2f}"), SYNTHETIC_SOURCE))
        d += timedelta(days=1)
    return quotes


def _money(value: float) -> Decimal:
    return Decimal(str(round(value, 2))).quantize(quantum(2))


def build_demo(
    session: Session,
    start: date,
    end: date,
    book: PriceBook,
    deflator: Deflator | None = None,
    seed: int = 7,
) -> DemoSummary:
    rng = random.Random(seed)
    session.add(Commodity(code=FUND, name="Demo fund (made up)", kind="fund", decimals=6, price_source="manual"))
    session.add(Commodity(code=SHARE, name="Demo share (made up)", kind="stock", decimals=0, price_source="manual"))
    session.flush()
    split_on = add_months(month_start(start), SPLIT_MONTH) + timedelta(days=14)
    split_on = split_on if start < split_on <= end else None
    for q in security_prices(start - timedelta(days=7), end, split_on, seed):
        upsert_price(session, q.commodity, q.quote, q.on, q.value, SYNTHETIC_SOURCE)
    securities = PriceBook(security_prices(start - timedelta(days=7), end, split_on, seed))
    for path, kind, commodity, owner, institution in ACCOUNTS:
        create_account(session, path, kind=kind, commodity=commodity, owner=owner, institution=institution)
    for path in CATEGORIES:
        create_account(session, path)
    count = 0
    for path, balance in OPENING:
        opening_balance(session, path, Decimal(balance), start)
        count += 1
    # 24 installments on the 20th, the first five months before the ledger starts
    loan = add_loan(
        session,
        "Consumer loan",
        LOAN,
        principal=Decimal(75000),
        monthly_rate=Decimal("0.0299"),
        count=24,
        first_due=same_day_months_later(start.replace(day=20), -5),
        opening_on=start,
    )
    count += 1

    def scale(d: date) -> float:
        if deflator is not None:
            return deflator.level(d) / deflator.level(start)
        months = (d.year - start.year) * 12 + (d.month - start.month)
        return 1.025**months

    skipped = 0
    card_spent = Decimal(dict(OPENING)[CARD])
    installments_due: dict[date, Decimal] = defaultdict(Decimal)
    friend_left = Decimal(dict(OPENING)[FRIEND])
    last_month = month_start(end)
    month = month_start(start)
    index = 0
    while month <= end:
        def on(day: int) -> date:
            return min(month + timedelta(days=day - 1), month_end(month))

        def due(day: int) -> bool:
            return start <= on(day) <= end

        f = scale(on(1))
        recent = month >= add_months(last_month, -1)
        if due(1):
            record_income(session, _money(70000 * f), "Income:Salary", CHECKING, on(1), "Salary", payee="SAMPLE EMPLOYER LTD")
            count += 1
        if due(3):
            record_expense(session, _money(25000 * f), "Expenses:Rent", CHECKING, on(3), "Rent", payee="Landlord")
            count += 1
        if due(15) and (card_spent > 0 or installments_due[month] > 0):
            payment = card_spent + installments_due[month]
            record_transfer(session, payment, CHECKING, CARD, on(15), "Credit card payment")
            card_spent = Decimal(0)
            count += 1
        for _ in range(rng.randint(7, 11)):
            day = rng.randint(1, 28)
            category, base, shops = rng.choice(SHOPS)
            shop = rng.choice(shops)
            amount = _money(base * f * rng.uniform(0.6, 1.6))
            if not due(day):
                continue
            target = UNCATEGORIZED_EXPENSE if recent and rng.random() < 1 / 3 else category
            record_expense(session, amount, target, CARD, on(day), f"POS {shop}", payee=shop)
            card_spent += amount
            count += 1
        for payee, category, first, later, day in SUBSCRIPTIONS:
            if due(day):
                price = Decimal(later if index >= 6 else first)
                record_expense(session, price, category, CARD, on(day), f"{payee} subscription", payee=payee)
                card_spent += price
                count += 1
        for month_index, day, payee, category, base, n in INSTALLMENT_PURCHASES:
            if index == month_index and due(day):
                amount = _money(base * f)
                record_installment_purchase(
                    session, amount, category, CARD, on(day), f"{payee} ({n} installments)", n, payee=payee
                )
                for k, part in enumerate(split_amount(amount, n)):
                    installments_due[add_months(month, k + 1)] += part
                count += 1
        winter = month.month in (12, 1, 2, 3)
        for category, base, day, payee in (
            ("Expenses:Bills:Electricity", 1200, 10, "ELECTRICITY COMPANY"),
            ("Expenses:Bills:Natural Gas", 3500 if winter else 500, 12, "GAS COMPANY"),
            ("Expenses:Bills:Phone", 400, 9, "MOBILE OPERATOR"),
        ):
            if due(day):
                amount = _money(base * f * rng.uniform(0.85, 1.15))
                record_expense(session, amount, category, CHECKING, on(day), f"{payee} bill", payee=payee)
                count += 1
        if due(18):
            record_expense(session, Decimal("649.00"), "Expenses:Bills:Internet", CHECKING, on(18), "INTERNET PROVIDER bill", payee="INTERNET PROVIDER")
            count += 1
        if due(20) and next_number(loan) is not None:
            record_payment(session, loan, on(20), CHECKING)
            count += 1
        if due(22) and friend_left > 0:
            part = min(Decimal(1250), friend_left)
            record_transfer(session, part, CHECKING, FRIEND, on(22), "Paid back to a friend")
            friend_left -= part
            count += 1
        if due(25):
            record_transfer(session, _money(2000 * f), CHECKING, "Pension:BES", on(25), "Pension contribution")
            count += 1
        if due(28):
            record_income(session, _money(150000 * 0.035 * f), "Income:Interest", "Time Deposit TRY", on(28), "Time deposit interest")
            count += 1
        if due(5):
            d = on(5)
            if month.month % 2:
                target, commodity, decimals, spend = "USD Account", "USD", 2, 8000 * f
            else:
                target, commodity, decimals, spend = "Gram Gold", "XAU_G", 4, 8000 * f
            price = book.resolve(commodity, d)
            if price is None:
                skipped += 1
            else:
                paid = _money(spend)
                got = (paid / price.value).quantize(quantum(decimals))
                record_exchange(session, CHECKING, paid, target, got, d, f"{commodity} savings")
                count += 1
        if due(10):
            d = on(10)
            paid = _money(4000 * f)
            units = (paid / securities.resolve(FUND, d).value).quantize(quantum(6))
            record_exchange(session, CHECKING, paid, FUND_ACCOUNT, units, d, "Monthly fund purchase")
            count += 1
        if index % 3 == 0 and due(12):
            d = on(12)
            price = securities.resolve(SHARE, d).value
            shares = (_money(9000 * f) / price).to_integral_value(rounding=ROUND_FLOOR)
            if shares > 0:
                record_exchange(session, CHECKING, _money(float(shares * price)), SHARE_ACCOUNT, shares, d, "Share purchase")
                count += 1
        if split_on is not None and month == month_start(split_on):
            record_split(session, SHARE_ACCOUNT, Decimal(2), split_on)
            count += 1
        month = add_months(month, 1)
        index += 1
    return DemoSummary(start, end, count, skipped)


@dataclass
class DemoLedger:
    path: Path
    summary: DemoSummary
    prices: str
    """Where the exchange rates and gold prices came from."""
    warning: str | None = None


def create_demo_ledger(
    path: Path,
    start: date,
    end: date,
    *,
    synthetic: bool = False,
    api_key: str | None = None,
    overwrite: bool = False,
    progress: Callable[[str], None] | None = None,
) -> DemoLedger:
    """Build the demo household in its own ledger at ``path``.

    Real exchange rates and gold prices are downloaded unless ``synthetic``;
    if the download fails, made-up prices are used. The user's own ledger is
    never written to.
    """
    import httpx

    from financeproject import config
    from financeproject.errors import FinanceError
    from financeproject.ledger.db import init_db, make_engine, session_scope
    from financeproject.prices.http import make_client
    from financeproject.prices.update import load_bundled_cpi, update_fx, update_gold
    from financeproject.reports.context import load_market_data

    path = Path(path)
    if path.resolve() == config.default_db_path().resolve():
        raise FinanceError("Demo data never goes into your real ledger; choose another file.")
    if path.exists():
        if not overwrite:
            raise FinanceError(f"{path} already exists; build it again to replace it.")
        path.unlink()
    warning = None
    engine = make_engine(path)
    try:
        init_db(engine)
        with session_scope(engine) as s:
            load_bundled_cpi(s)
            source = "made up"
            if not synthetic:
                try:
                    with make_client() as client:
                        update_fx(s, client, start - timedelta(days=7), end, ["USD"], api_key, pause=0.1, progress=progress)
                        update_gold(s, client, start - timedelta(days=7), end, progress=progress)
                    source = "TCMB + Frankfurter"
                except (httpx.HTTPError, FinanceError) as exc:
                    warning = f"Download failed ({exc}); made-up prices were used."
                    s.rollback()
                    load_bundled_cpi(s)
                    synthetic = True
            if synthetic:
                for q in synthetic_prices(start - timedelta(days=7), end):
                    upsert_price(s, q.commodity, q.quote, q.on, q.value, SYNTHETIC_SOURCE)
            s.flush()
            if progress:
                progress("Building the household")
            market = load_market_data(s)
            summary = build_demo(s, start, end, market.book, market.deflator)
    finally:
        engine.dispose()
    path.chmod(0o600)
    return DemoLedger(path, summary, source, warning)
