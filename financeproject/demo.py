"""A made-up household for trying the program and for tests. No real data.

``build_demo`` writes accounts, opening balances and about twenty transactions a
month: salary, rent, card spending, bills, card and loan payments, savings in
dollars and gold. Nominal amounts grow with the CPI when it is loaded, otherwise
2.5 % a month. ``synthetic_prices`` gives offline USD/TRY and gold prices.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy.orm import Session

from financeproject.core.dates import add_months, month_end, month_start
from financeproject.core.inflation import Deflator
from financeproject.core.money import quantum
from financeproject.core.prices import PriceBook, Quote
from financeproject.ledger.accounts import create_account
from financeproject.ledger.transactions import (
    Leg,
    add_transaction,
    opening_balance,
    record_exchange,
    record_expense,
    record_income,
    record_transfer,
)

SYNTHETIC_SOURCE = "synthetic"

ACCOUNTS = (
    # path, kind, commodity, owner, institution
    ("Assets:Bank:Checking TRY", "checking", "TRY", "Me", "Sample Bank"),
    ("Assets:Bank:Time Deposit TRY", "time_deposit", "TRY", "Me", "Sample Bank"),
    ("Assets:FX:USD Account", "fx", "USD", "Me", "Sample Bank"),
    ("Assets:Gold:Gram Gold", "gold", "XAU_G", "Me", "Sample Bank"),
    ("Assets:Cash:Wallet", "cash", "TRY", "Me", None),
    ("Assets:Pension:BES", "pension", "TRY", "Me", "Sample Pension"),
    ("Assets:Bank:Dad's Checking", "checking", "TRY", "Dad", "Other Bank"),
    ("Liabilities:Credit Card:Sample Card", "credit_card", "TRY", "Me", "Sample Bank"),
    ("Liabilities:Loan:Consumer Loan", "consumer_loan", "TRY", "Me", "Sample Bank"),
)
CATEGORIES = (
    "Expenses:Rent",
    "Expenses:Food:Groceries",
    "Expenses:Food:Restaurants",
    "Expenses:Bills:Electricity",
    "Expenses:Bills:Natural Gas",
    "Expenses:Transport",
    "Expenses:Clothing",
    "Expenses:Health",
    "Expenses:Home",
    "Expenses:Interest and Fees",
    "Income:Salary",
    "Income:Interest",
)
OPENING = (
    ("Assets:Bank:Checking TRY", "40000"),
    ("Assets:Bank:Time Deposit TRY", "150000"),
    ("Assets:FX:USD Account", "1500"),
    ("Assets:Gold:Gram Gold", "20"),
    ("Assets:Cash:Wallet", "3000"),
    ("Assets:Pension:BES", "60000"),
    ("Assets:Bank:Dad's Checking", "25000"),
    ("Liabilities:Credit Card:Sample Card", "8000"),
    ("Liabilities:Loan:Consumer Loan", "60000"),
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
    for path, kind, commodity, owner, institution in ACCOUNTS:
        create_account(session, path, kind=kind, commodity=commodity, owner=owner, institution=institution)
    for path in CATEGORIES:
        create_account(session, path)
    count = 0
    for path, balance in OPENING:
        opening_balance(session, path, Decimal(balance), start)
        count += 1

    def scale(d: date) -> float:
        if deflator is not None:
            return deflator.level(d) / deflator.level(start)
        months = (d.year - start.year) * 12 + (d.month - start.month)
        return 1.025**months

    skipped = 0
    card_spent = Decimal(dict(OPENING)["Liabilities:Credit Card:Sample Card"])
    loan_left = Decimal(dict(OPENING)["Liabilities:Loan:Consumer Loan"])
    month = month_start(start)
    while month <= end:
        def on(day: int) -> date:
            return min(month + timedelta(days=day - 1), month_end(month))

        def due(day: int) -> bool:
            return start <= on(day) <= end

        f = scale(on(1))
        if due(1):
            record_income(session, _money(70000 * f), "Income:Salary", "Checking TRY", on(1), "Salary")
            count += 1
        if due(3):
            record_expense(session, _money(25000 * f), "Expenses:Rent", "Checking TRY", on(3), "Rent", payee="Landlord")
            count += 1
        if due(15) and card_spent > 0:
            record_transfer(session, card_spent, "Checking TRY", "Sample Card", on(15), "Credit card payment")
            card_spent = Decimal(0)
            count += 1
        for _ in range(rng.randint(7, 11)):
            day = rng.randint(1, 28)
            if not due(day):
                continue
            category, base = rng.choice(
                (
                    ("Expenses:Food:Groceries", 2200),
                    ("Expenses:Food:Groceries", 2200),
                    ("Expenses:Food:Restaurants", 1100),
                    ("Expenses:Transport", 800),
                    ("Expenses:Clothing", 2500),
                    ("Expenses:Health", 1500),
                    ("Expenses:Home", 2000),
                )
            )
            amount = _money(base * f * rng.uniform(0.6, 1.6))
            record_expense(session, amount, category, "Sample Card", on(day), category.split(":")[-1])
            card_spent += amount
            count += 1
        winter = month.month in (12, 1, 2, 3)
        for category, base, day in (
            ("Expenses:Bills:Electricity", 1200, 10),
            ("Expenses:Bills:Natural Gas", 3500 if winter else 500, 12),
        ):
            if due(day):
                record_expense(
                    session, _money(base * f * rng.uniform(0.85, 1.15)), category, "Checking TRY", on(day), category.split(":")[-1]
                )
                count += 1
        if due(20) and loan_left > 0:
            principal = min(Decimal(3000), loan_left)
            interest = _money(float(loan_left) * 0.03)
            add_transaction(
                session,
                on(20),
                "Loan installment",
                [
                    Leg("Consumer Loan", principal),
                    Leg("Expenses:Interest and Fees", interest),
                    Leg("Checking TRY", -(principal + interest)),
                ],
            )
            loan_left -= principal
            count += 1
        if due(25):
            record_transfer(session, _money(2000 * f), "Checking TRY", "Pension:BES", on(25), "Pension contribution")
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
                record_exchange(session, "Checking TRY", paid, target, got, d, f"{commodity} savings")
                count += 1
        month = add_months(month, 1)
    return DemoSummary(start, end, count, skipped)
