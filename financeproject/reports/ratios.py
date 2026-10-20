"""Household financial ratios with the benchmarks of Greninger et al. (1996).

Greninger, Hampton, Kitt and Achacoso asked 156 financial planners and
educators in a Delphi study which ratios and values describe a financially
healthy family. Their "financial well-being profile" (Table 2) is used here as
it was published; the benchmarks come from the United States of the 1990s and
are signals for a closer look, not advice. Definitions follow the paper:

- **Liquid assets:** cash and cash equivalents: cash, checking and time
  deposits, foreign currency accounts. A wider measure also counts the other
  assets that can be sold within a week (gold, funds, stocks).
- **Investment assets:** gold, funds, stocks and private pension savings.
- **Monthly expenses:** living expenses including debt repayment: the
  expenses of the period plus the loan principal repaid, per month.
- **Debt payments:** loan principal and the interest and fees paid with it,
  plus the credit card installments that fell due. Paying a card's ordinary
  balance is not a debt payment; those purchases are already expenses.
- **Income:** income as recorded, which for salaries is after tax; the paper's
  insolvency ratios use after-tax income as well.

The flows cover the ``months`` months up to ``end`` and the stocks are taken at
``end``. Inflation protection compares the growth of net worth with the CPI
over the twelve months up to the latest published CPI month.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from financeproject.core.dates import add_months, month_end, month_start
from financeproject.core.kinds import EXPENSE, LIABILITY
from financeproject.ledger.installments import due_between
from financeproject.ledger.models import Posting, Transaction
from financeproject.reports.balance_sheet import build_balance_sheet
from financeproject.reports.change import make_valuer
from financeproject.reports.context import MarketData, load_market_data
from financeproject.reports.income import income_statement

CASH_EQUIVALENTS = ("cash", "checking", "time_deposit", "fx")
INVESTMENTS = ("gold", "fund", "stock", "pension")
LOANS = ("consumer_loan", "vehicle_loan", "other_debt", "personal_debt", "mortgage")
SOURCE = "Greninger et al. 1996"

OK, BELOW, DANGER = "ok", "below", "danger"


@dataclass(frozen=True)
class Ratio:
    name: str
    value: float | None
    unit: str
    """``months``, ``%`` or ``×``."""
    benchmark: str
    status: str | None
    """``ok``, ``below`` (outside the benchmark), ``danger`` or ``None`` (no benchmark)."""
    note: str = ""
    key: str = ""
    """A stable name of the ratio, for an interface that words it in its own language."""
    detail: dict = field(default_factory=dict)
    """The parts of the note that change (inflation protection: its figures, or why there are none)."""


@dataclass
class RatioReport:
    start: date
    end: date
    months: int
    income: Decimal = Decimal(0)
    expenses: Decimal = Decimal(0)
    principal_repaid: Decimal = Decimal(0)
    loan_payments: Decimal = Decimal(0)
    mortgage_payments: Decimal = Decimal(0)
    installments_due: Decimal = Decimal(0)
    liquid: Decimal = Decimal(0)
    liquid_wide: Decimal = Decimal(0)
    investments: Decimal = Decimal(0)
    assets: Decimal = Decimal(0)
    liabilities: Decimal = Decimal(0)
    net_worth: Decimal = Decimal(0)
    ratios: list[Ratio] = field(default_factory=list)

    @property
    def monthly_expenses(self) -> Decimal:
        return (self.expenses + self.principal_repaid) / self.months


def _pct(x: Decimal, y: Decimal) -> float | None:
    return float(x / y) * 100 if y > 0 else None


def _status(value: float | None, ok: float, danger: float | None = None, higher_is_better: bool = True) -> str | None:
    if value is None:
        return None
    if higher_is_better:
        if value >= ok:
            return OK
        return DANGER if danger is not None and value < danger else BELOW
    if value <= ok:
        return OK
    return DANGER if danger is not None and value >= danger else BELOW


def _debt_payments(session: Session, start: date, end: date, market: MarketData) -> tuple[Decimal, Decimal, Decimal]:
    """Loan principal repaid, non-mortgage loan payments and mortgage payments (TRY)."""
    value = make_valuer(market, "try")
    principal = loans = mortgage = Decimal(0)
    txs = session.scalars(
        select(Transaction)
        .where(Transaction.date > start, Transaction.date <= end)
        .options(selectinload(Transaction.postings).selectinload(Posting.account))
    )
    for tx in txs:
        repaid = [p for p in tx.postings if p.account.type == LIABILITY and p.account.kind in LOANS and p.amount > 0]
        if not repaid:
            continue
        charges = [p for p in tx.postings if p.account.type == EXPENSE]
        worth = {p.id: p.amount * (value(p.commodity_code, tx.date) or Decimal(0)) for p in repaid + charges}
        paid = sum(worth.values(), Decimal(0))
        principal += sum((worth[p.id] for p in repaid), Decimal(0))
        if any(p.account.kind == "mortgage" for p in repaid):
            mortgage += paid
        else:
            loans += paid
    return principal, loans, mortgage


def _inflation_protection(session: Session, end: date, market: MarketData) -> tuple[float | None, str, dict]:
    """The ratio, its note, and the note's parts (``reason`` when there is no ratio)."""
    deflator = market.deflator
    if deflator is None:
        return None, "no CPI", {"reason": "no_cpi"}
    last = min(month_start(end), deflator.last_month)
    window_end = min(month_end(last), end)
    window_start = month_end(add_months(last, -12))
    first_cpi, last_cpi = add_months(last, -12), last
    if first_cpi < deflator.first_month:
        return None, "the CPI does not go back a year", {"reason": "short_cpi"}
    nw0 = build_balance_sheet(session, window_start, market=market).net_worth
    nw1 = build_balance_sheet(session, window_end, market=market).net_worth
    if nw0.net.try_ <= 0:
        return None, "no positive net worth a year earlier", {"reason": "no_net_worth"}
    growth = float(nw1.net.try_ / nw0.net.try_ - 1) * 100
    inflation = (deflator.month_value(last_cpi) / deflator.month_value(first_cpi) - 1) * 100
    if inflation <= 0:
        return None, "no inflation in the window", {"reason": "no_inflation"}
    note = f"net worth {growth:+.1f} %, CPI {inflation:+.1f} % ({window_start} – {window_end})"
    return growth / inflation, note, {"growth": growth, "inflation": inflation, "start": window_start, "end": window_end}


def household_ratios(session: Session, end: date, months: int = 12, market: MarketData | None = None) -> RatioReport:
    market = market or load_market_data(session)
    last_month = month_start(end)
    first_month = add_months(last_month, -(months - 1))
    start = first_month - timedelta(days=1)
    report = RatioReport(start=start, end=end, months=months)

    statement = income_statement(session, first_month, last_month, depth=1, market=market, end=end)
    report.income = statement.total_income()
    report.expenses = statement.total_expenses()
    report.principal_repaid, report.loan_payments, report.mortgage_payments = _debt_payments(session, start, end, market)
    report.installments_due = sum((i.amount for i in due_between(session, first_month, last_month)), Decimal(0))

    bs = build_balance_sheet(session, end, market=market)
    for v in bs.assets:
        if v.value is None:
            continue
        kind = v.position.kind
        if kind in CASH_EQUIVALENTS:
            report.liquid += v.value
        if v.position.tier in (0, 1):
            report.liquid_wide += v.value
        if kind in INVESTMENTS:
            report.investments += v.value
    report.assets = bs.net_worth.assets
    report.liabilities = bs.net_worth.liabilities
    report.net_worth = bs.net_worth.net.try_

    monthly = report.monthly_expenses
    emergency = float(report.liquid / monthly) if monthly > 0 else None
    emergency_wide = float(report.liquid_wide / monthly) if monthly > 0 else None
    savings = _pct(report.income - report.expenses, report.income)
    nonmortgage = _pct(report.loan_payments + report.installments_due, report.income)
    total_debt = _pct(report.loan_payments + report.mortgage_payments + report.installments_due, report.income)
    liquid_share = _pct(report.liquid, report.net_worth)
    investment_share = _pct(report.investments, report.net_worth)
    protection, protection_note, protection_detail = _inflation_protection(session, end, market)

    report.ratios = [
        Ratio("Emergency fund", emergency, "months", "≥ 2.5 months", _status(emergency, 2.5),
              "liquid assets / monthly expenses (incl. loan principal)", "emergency"),
        Ratio("Emergency fund, wider", emergency_wide, "months", "", None,
              "also counting gold, funds and stocks (sellable within a week)", "emergency_wide"),
        Ratio("Savings rate", savings, "%", "≥ 10 %", _status(savings, 10, 0),
              "(income − expenses) / income; below zero = spending more than income", "savings"),
        Ratio("Non-mortgage debt payments", nonmortgage, "%", "≤ 15 %; danger ≥ 20 %",
              _status(nonmortgage, 15, 20, higher_is_better=False),
              "loan payments and card installments / income", "nonmortgage"),
        Ratio("All debt payments", total_debt, "%", "≤ 35 %; danger ≥ 45 %",
              _status(total_debt, 35, 45, higher_is_better=False), "including mortgage payments", "all_debt"),
        Ratio("Liquid assets / net worth", liquid_share, "%", "≥ 15 %", _status(liquid_share, 15), "", "liquid_share"),
        Ratio("Investment assets / net worth", investment_share, "%", "≥ 50 %", _status(investment_share, 50),
              "gold, funds, stocks, private pension", "investment_share"),
        Ratio("Inflation protection", protection, "×", "≥ 2", _status(protection, 2, 1),
              f"growth of net worth / CPI inflation; {protection_note}", "inflation", protection_detail),
        Ratio("Debt / assets", _pct(report.liabilities, report.assets), "%", "", None,
              "solvency: net worth below zero means insolvent (DeVaney & Lytton 1995)", "debt_assets"),
    ]
    return report
