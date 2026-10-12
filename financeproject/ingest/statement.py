"""Bank statements: their lines, running balances and totals.

A statement read by a bank template is checked before anything is imported:

- **Order.** Banks list the newest line first or last. The lines are put oldest
  first, and when the statement has a balance column, the order in which every
  balance equals the previous balance plus the line's amount is the one taken.
- **Running balance.** If no order makes the balances add up, a line is
  missing or was read wrongly, and the import stops.
- **Totals.** If the statement prints the sum of its debits and credits, both
  must equal the sums of the lines read.

The balance before the first line (``opening_balance``) and after the last
(``closing_balance``) let the import start an account at the right balance and
compare the ledger with the bank afterwards.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from financeproject.errors import FinanceError


@dataclass(frozen=True, slots=True)
class StatementLine:
    date: date
    description: str
    amount: Decimal
    """Signed change of the account: negative = money out."""
    balance: Decimal | None = None
    """Balance after this line, as printed by the bank."""
    reference: str | None = None
    """The bank's own number for the line (receipt or reference number)."""
    payee: str | None = None
    """Merchant or person, when the template can read it from the description."""


@dataclass(frozen=True)
class Statement:
    bank: str
    file_format: str
    lines: tuple[StatementLine, ...]
    """Oldest first."""
    currency: str | None = None
    period: tuple[date, date] | None = None
    debit_total: Decimal | None = None
    """Sum of the money-out lines as printed by the bank (negative or zero)."""
    credit_total: Decimal | None = None
    """Sum of the money-in lines as printed by the bank."""

    @property
    def has_balances(self) -> bool:
        return bool(self.lines) and all(line.balance is not None for line in self.lines)

    @property
    def opening_balance(self) -> Decimal | None:
        """Balance before the first line."""
        if not self.has_balances:
            return None
        first = self.lines[0]
        return first.balance - first.amount

    @property
    def closing_balance(self) -> Decimal | None:
        return self.lines[-1].balance if self.has_balances else None

    @property
    def start(self) -> date:
        return self.period[0] if self.period else self.lines[0].date

    @property
    def end(self) -> date:
        return self.period[1] if self.period else self.lines[-1].date


def _dates_ascending(lines: list[StatementLine]) -> bool:
    return all(a.date <= b.date for a, b in zip(lines, lines[1:]))


def balance_breaks(lines: list[StatementLine]) -> list[int]:
    """Indexes ``k`` where ``balance[k] != balance[k-1] + amount[k]``."""
    return [
        k
        for k in range(1, len(lines))
        if lines[k].balance is not None
        and lines[k - 1].balance is not None
        and lines[k].balance != lines[k - 1].balance + lines[k].amount
    ]


def chronological(lines: list[StatementLine]) -> list[StatementLine]:
    """The lines oldest first; with balances, the order in which they add up."""
    if len(lines) < 2:
        return list(lines)
    orders = [order for order in (list(lines), list(reversed(lines))) if _dates_ascending(order)]
    if not orders:
        raise FinanceError("The statement's dates are in neither ascending nor descending order.")
    if any(line.balance is None for line in lines):
        return orders[0]
    for order in orders:
        if not balance_breaks(order):
            return order
    order = orders[0]
    k = balance_breaks(order)[0]
    prev, line = order[k - 1], order[k]
    raise FinanceError(
        "The running balance does not add up, so a line may be missing or misread: "
        f"{prev.balance} {line.amount:+} ≠ {line.balance} ({line.date}, {line.description[:60]!r})."
    )


def check_totals(statement: Statement) -> None:
    """Compare the printed debit and credit totals with the lines."""
    debits = sum((line.amount for line in statement.lines if line.amount < 0), Decimal(0))
    credits = sum((line.amount for line in statement.lines if line.amount > 0), Decimal(0))
    if statement.debit_total is not None and debits != statement.debit_total:
        raise FinanceError(
            f"The money-out lines add up to {debits}, but the statement's total is {statement.debit_total}."
        )
    if statement.credit_total is not None and credits != statement.credit_total:
        raise FinanceError(
            f"The money-in lines add up to {credits}, but the statement's total is {statement.credit_total}."
        )
