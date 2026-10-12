"""Income statement: income and expenses by category and month.

Amounts are valued on the day of each transaction (TRY, or real TRY in the
prices of the base month). Income is shown positive, expenses positive, and
net saving = income − expenses. The savings rate is net saving / income.
Accounts are grouped to ``depth`` levels: at depth 2, ``Expenses:Food:Groceries``
counts under ``Expenses:Food``.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from sqlalchemy import select

from financeproject.core.dates import add_months, month_end, month_start
from financeproject.core.kinds import EXPENSE, INCOME
from financeproject.ledger.models import Account, Posting, Transaction
from financeproject.reports.change import make_valuer
from financeproject.reports.context import MarketData, load_market_data


def truncate(path: str, depth: int | None) -> str:
    if not depth or depth < 1:
        return path
    return ":".join(path.split(":")[:depth])


@dataclass
class IncomeStatement:
    months: list[date]
    start: date
    end: date
    unit: str
    income: dict[str, dict[date, Decimal]] = field(default_factory=dict)
    expenses: dict[str, dict[date, Decimal]] = field(default_factory=dict)
    unvalued: list[str] = field(default_factory=list)
    """Postings left out because their commodity had no price that day."""

    @staticmethod
    def _sum(table: dict[str, dict[date, Decimal]], month: date | None) -> Decimal:
        return sum(
            (v for by_month in table.values() for m, v in by_month.items() if month is None or m == month),
            Decimal(0),
        )

    def total_income(self, month: date | None = None) -> Decimal:
        return self._sum(self.income, month)

    def total_expenses(self, month: date | None = None) -> Decimal:
        return self._sum(self.expenses, month)

    def net(self, month: date | None = None) -> Decimal:
        return self.total_income(month) - self.total_expenses(month)

    def savings_rate(self, month: date | None = None) -> float | None:
        income = self.total_income(month)
        return float(self.net(month) / income) if income > 0 else None

    @staticmethod
    def totals(table: dict[str, dict[date, Decimal]]) -> dict[str, Decimal]:
        """Each row's total over the period, largest first."""
        totals = {k: sum(v.values(), Decimal(0)) for k, v in table.items()}
        return dict(sorted(totals.items(), key=lambda kv: (-kv[1], kv[0])))


def months_between(first: date, last: date) -> list[date]:
    out, m = [], month_start(first)
    while m <= month_start(last):
        out.append(m)
        m = add_months(m, 1)
    return out


def income_statement(
    session,
    first_month: date,
    last_month: date,
    depth: int | None = 2,
    unit: str = "try",
    market: MarketData | None = None,
    end: date | None = None,
) -> IncomeStatement:
    """Income and expenses from the first day of ``first_month`` to the end of
    ``last_month`` (or to ``end``, when that is earlier)."""
    market = market or load_market_data(session)
    value = make_valuer(market, unit)
    start = month_start(first_month)
    stop = min(month_end(last_month), end) if end else month_end(last_month)
    statement = IncomeStatement(months_between(start, stop), start, stop, unit)
    income: dict[str, dict[date, Decimal]] = defaultdict(lambda: defaultdict(Decimal))
    expenses: dict[str, dict[date, Decimal]] = defaultdict(lambda: defaultdict(Decimal))
    rows = session.execute(
        select(Transaction.date, Account.path, Account.type, Posting.commodity_code, Posting.amount)
        .join(Posting, Posting.transaction_id == Transaction.id)
        .join(Account, Posting.account_id == Account.id)
        .where(Transaction.date >= start, Transaction.date <= stop, Account.type.in_((INCOME, EXPENSE)))
    )
    for on, path, account_type, commodity, amount in rows:
        unit_value = value(commodity, on)
        if unit_value is None:
            statement.unvalued.append(f"{on} {path} {amount} {commodity}")
            continue
        worth = amount * unit_value
        key, month = truncate(path, depth), month_start(on)
        if account_type == INCOME:
            income[key][month] -= worth
        else:
            expenses[key][month] += worth
    statement.income = {k: dict(v) for k, v in income.items()}
    statement.expenses = {k: dict(v) for k, v in expenses.items()}
    return statement
