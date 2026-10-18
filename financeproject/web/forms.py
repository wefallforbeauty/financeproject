"""Reading form values and offering accounts to choose from."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation

from sqlalchemy import select
from sqlalchemy.orm import Session

from financeproject.core.dates import parse_date
from financeproject.core.kinds import ASSET, EXPENSE, INCOME, KINDS, LIABILITY
from financeproject.core.money import (
    get_number_format,
    is_ambiguous_amount,
    parse_decimal,
)
from financeproject.errors import FinanceError
from financeproject.ledger.db import TRADING_ROOT
from financeproject.ledger.models import Account, Commodity


def day_value(text: str | None, field: str = "date") -> date:
    """A date from a form (``2026-10-18`` from a date input, or the other formats ``parse_date`` reads)."""
    if not text or not text.strip():
        return date.today()
    try:
        return parse_date(text.strip())
    except (FinanceError, ValueError) as exc:
        raise FinanceError(f"Cannot read the {field} {text!r}.") from exc


def amount_value(text: str | None, field: str = "amount", positive: bool = True) -> Decimal:
    """An amount typed into a form.

    ``125,50`` and ``125.50`` are both 125.5, and ``1.234,56`` and ``1,234.56`` are
    read by their last separator. Only an amount with one separator and three
    digits after it (``10.000``) depends on the number format chosen in Settings:
    ten thousand with ``1.234,56``, ten with ``1,234.56``.
    """
    if not text or not text.strip():
        raise FinanceError(f"The {field} is missing.")
    raw = text.strip()
    decimal_sep = None
    if is_ambiguous_amount(raw) and not re.match(r"^[-+(]?0[.,]", raw):
        decimal_sep = "," if get_number_format() == "1.234,56" else "."
    try:
        value = parse_decimal(raw, decimal_sep)
    except (FinanceError, InvalidOperation, ValueError) as exc:
        raise FinanceError(f"Cannot read the {field} {text!r}.") from exc
    if positive and value <= 0:
        raise FinanceError(f"The {field} must be more than zero.")
    return value


def optional(text: str | None) -> str | None:
    return text.strip() if text and text.strip() else None


def short(path: str) -> str:
    """An account path without its root: ``Expenses:Food:Groceries`` → ``Food:Groceries``."""
    return path.split(":", 1)[1] if ":" in path else path


@dataclass(frozen=True)
class Option:
    value: str
    label: str
    commodity: str | None = None


@dataclass(frozen=True)
class Choices:
    money: list[Option]
    """Open asset and liability accounts."""
    expense: list[Option]
    income: list[Option]

    @property
    def categories(self) -> list[Option]:
        return self.expense + self.income


def choices(session: Session) -> Choices:
    accounts = list(session.scalars(select(Account).where(Account.closed_on.is_(None)).order_by(Account.path)))

    def options(types) -> list[Option]:
        return [Option(a.path, short(a.path), a.commodity_code) for a in accounts if a.type in types]

    return Choices(
        [o for o in options((ASSET, LIABILITY)) if not o.value.startswith(TRADING_ROOT)],
        options((EXPENSE,)),
        options((INCOME,)),
    )


def commodities(session: Session) -> list[Commodity]:
    return list(session.scalars(select(Commodity).order_by(Commodity.kind, Commodity.code)))


PARENTS = {
    "cash": "Assets:Cash",
    "checking": "Assets:Bank",
    "time_deposit": "Assets:Bank",
    "fx": "Assets:FX",
    "gold": "Assets:Gold",
    "fund": "Assets:Investments",
    "stock": "Assets:Investments",
    "pension": "Assets:Pension",
    "receivable": "Assets:Receivables",
    "real_estate": "Assets:Property",
    "vehicle": "Assets:Property",
    "other_asset": "Assets:Other",
    "credit_card": "Liabilities:Credit Card",
    "overdraft": "Liabilities:Overdraft",
    "personal_debt": "Liabilities:Personal",
    "consumer_loan": "Liabilities:Loans",
    "mortgage": "Liabilities:Loans",
    "vehicle_loan": "Liabilities:Loans",
    "other_debt": "Liabilities:Other",
    "expense": "Expenses",
    "income": "Income",
}
"""Where a new account of each kind goes; ``expense`` and ``income`` are categories."""


def account_path(kind: str, name: str) -> str:
    """The path of a new account: ``checking`` and ``Ziraat`` → ``Assets:Bank:Ziraat``.

    A name that already starts with a root (``Assets:…``) is taken as it is.
    """
    name = name.strip().strip(":")
    if not name:
        raise FinanceError("The name is missing.")
    if name.split(":")[0] in ("Assets", "Liabilities", "Equity", "Income", "Expenses"):
        return name
    if kind not in PARENTS:
        raise FinanceError(f"Unknown kind {kind!r}.")
    return f"{PARENTS[kind]}:{name}"


def kind_groups() -> list[tuple[str, list[tuple[str, str]]]]:
    """The kinds of a new account by group, as (key, label) pairs."""
    assets = [(k.key, k.label) for k in KINDS.values() if k.type == ASSET]
    debts = [(k.key, k.label) for k in KINDS.values() if k.type == LIABILITY]
    return [("Money and assets", assets), ("Debts", debts), ("Categories", [("expense", "Expense category"), ("income", "Income category")])]
