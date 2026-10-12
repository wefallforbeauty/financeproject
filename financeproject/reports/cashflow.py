"""Cash flow statement by month: where the money in the cash accounts came from and went.

Cash means the asset accounts usable the same day (liquidity tier 0: cash,
checking and foreign currency accounts). Each movement is grouped by its
counterpart, as in a company's cash flow statement (IAS 7):

- **operating:** income received and expenses paid in cash;
- **investing:** money put into or taken out of other assets (time deposits,
  gold, funds, private pension);
- **card payments** and **loans** (financing): paying credit cards and the
  overdraft, borrowing and repaying loans.

A purchase made with a credit card is not a cash outflow on the day it is made;
the cash leaves when the card is paid. That is why operating cash flow differs
from the income statement's net saving, and why card payments get their own
line. The statement closes with the exchange results of conversions, opening
balances, and the revaluation of foreign currency held as cash (IAS 7 §28,
"effect of exchange rate changes on cash").
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy.orm import Session

from financeproject.core.dates import month_end, month_start
from financeproject.core.decompose import Decomposition
from financeproject.reports.change import cash_change
from financeproject.reports.context import MarketData, load_market_data
from financeproject.reports.income import months_between

LINES = (
    # key, label, flow categories
    ("income", "Income received", ("income",)),
    ("expenses", "Expenses paid", ("expenses",)),
    ("operating", "Operating cash flow", ("income", "expenses")),
    ("investing", "Investing (other assets)", ("investing",)),
    ("cards", "Card payments", ("cards",)),
    ("loans", "Loans", ("loans",)),
    ("exchange", "Exchange results", ("exchange",)),
    ("opening", "Opening balances", ("opening",)),
    ("other", "Other", ("equity", "unvalued")),
)


@dataclass(frozen=True)
class CashFlowMonth:
    month: date
    start: date
    """Cash is measured at the end of this day…"""
    end: date
    """…and at the end of this one."""
    decomposition: Decomposition

    @property
    def beginning(self) -> Decimal:
        return self.decomposition.start_value

    @property
    def ending(self) -> Decimal:
        return self.decomposition.end_value

    @property
    def revaluation(self) -> Decimal:
        return self.decomposition.revaluation_total

    def line(self, key: str) -> Decimal:
        categories = next(c for k, _, c in LINES if k == key)
        return sum((self.decomposition.flows.get(c, Decimal(0)) for c in categories), Decimal(0))


def cash_flow(
    session: Session,
    first_month: date,
    last_month: date,
    unit: str = "try",
    market: MarketData | None = None,
    end: date | None = None,
) -> list[CashFlowMonth]:
    """One cash flow statement per month; the last month stops at ``end`` if given."""
    market = market or load_market_data(session)
    out: list[CashFlowMonth] = []
    for m in months_between(first_month, last_month):
        start = month_start(m) - timedelta(days=1)
        stop = month_end(m) if end is None else min(month_end(m), end)
        if stop <= start:
            break
        out.append(CashFlowMonth(m, start, stop, cash_change(session, start, stop, unit, market)))
    return out
