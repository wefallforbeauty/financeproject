"""Debts: what is owed, what it costs, and what it weighs once inflation is counted.

Every liability with a balance is listed. For a loan defined in the ledger the
report adds its rates, its annual cost rate, the installments left and their
value in today's money: each installment discounted at the expected inflation
π, Σ payment · (1 + π)^−t with t in years from the report date. The real
interest rate is (1 + effective annual rate) / (1 + π) − 1 (Fisher). π
defaults to the CPI's change over the last twelve published months.

``payoff_debts`` gathers the same debts for a repayment simulation
(``core.loans.payoff``).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy.orm import Session

from financeproject.core.dates import month_start
from financeproject.core.inflation import annual_pct, fisher_real_rate
from financeproject.core.kinds import LIABILITY
from financeproject.core.loans import Debt, cents, effective_annual, year_fraction
from financeproject.errors import FinanceError
from financeproject.ledger.accounts import find_account
from financeproject.ledger.installments import not_billed
from financeproject.ledger.loans import (
    LoanState,
    all_loans,
    cost_rate,
    loan_state,
    terms_of,
)
from financeproject.ledger.queries import account_balance, positions_at
from financeproject.prices.store import load_index


@dataclass(frozen=True)
class Inflation:
    rate: float
    """Expected inflation a year, as a fraction."""
    source: str


def trailing_inflation(session: Session) -> Inflation | None:
    """The CPI's change over the last twelve published months."""
    annual = annual_pct(load_index(session))
    if not annual:
        return None
    last = max(annual)
    return Inflation(annual[last] / 100, f"CPI, 12 months to {last:%Y-%m}")


def present_value_at(state: LoanState, on: date, rate: float) -> Decimal:
    """The installments left, each discounted at ``rate`` a year from its due date back to ``on``."""
    total = sum(float(i.payment) * (1 + rate) ** -max(year_fraction(on, i.due), 0.0) for i in state.remaining)
    return cents(Decimal(repr(total)))


@dataclass(frozen=True)
class DebtLine:
    account: str
    kind: str | None
    commodity: str
    owed: Decimal
    state: LoanState | None = None
    """For a loan defined in the ledger."""
    cost_rate: float | None = None
    """Annual cost rate of the loan's terms (fees included)."""
    effective_rate: float | None = None
    """Interest and taxes compounded over a year."""
    real_rate: float | None = None
    """``effective_rate`` after inflation."""
    today_value: Decimal | None = None
    """The installments left in today's money."""
    later_installments: Decimal = Decimal(0)
    """Cards: installments of purchases billed in later months (part of ``owed``)."""


@dataclass(frozen=True)
class DebtReport:
    on: date
    inflation: Inflation | None
    lines: tuple[DebtLine, ...]

    def _sum(self, values) -> Decimal:
        return sum(values, Decimal(0))

    @property
    def owed(self) -> Decimal:
        return self._sum(line.owed for line in self.lines if line.commodity == "TRY")

    @property
    def still_to_pay(self) -> Decimal:
        return self._sum(line.state.still_to_pay for line in self.lines if line.state is not None)

    @property
    def today_value(self) -> Decimal | None:
        values = [line.today_value for line in self.lines if line.state is not None]
        return None if any(v is None for v in values) else self._sum(values)


def debt_report(session: Session, on: date, inflation: float | None = None) -> DebtReport:
    """Every debt on ``on``; ``inflation`` (a fraction a year) overrides the CPI's last twelve months."""
    expected = Inflation(inflation, "given") if inflation is not None else trailing_inflation(session)
    loans = {loan.account_id: loan for loan in all_loans(session)}
    later = not_billed(session, month_start(on), on)
    lines = []
    for p in positions_at(session, on, types=(LIABILITY,)):
        owed = -p.quantity
        if owed <= 0:
            continue
        loan = loans.get(p.account_id)
        if loan is None:
            lines.append(DebtLine(p.account, p.kind, p.commodity, owed, later_installments=later.get(p.account, Decimal(0))))
            continue
        state = loan_state(session, loan, on)
        effective = effective_annual(float(terms_of(loan).gross_rate))
        lines.append(
            DebtLine(
                p.account,
                p.kind,
                p.commodity,
                owed,
                state=state,
                cost_rate=cost_rate(loan),
                effective_rate=effective,
                real_rate=fisher_real_rate(effective, expected.rate) if expected else None,
                today_value=present_value_at(state, on, expected.rate) if expected else None,
            )
        )
    return DebtReport(on, expected, tuple(lines))


def payoff_debts(
    session: Session, on: date, rates: dict[str, Decimal], minimums: dict[str, Decimal]
) -> list[Debt]:
    """The loans defined in the ledger, plus the other debts named in ``rates`` or ``minimums``.

    A loan's monthly rate includes its taxes and its least payment is its next
    installment. ``rates`` (fractions a month) and ``minimums`` are keyed by
    account names; a debt named in only one of them gets rate 0 or no
    least payment.
    """
    debts: list[Debt] = []
    loan_accounts = set()
    for loan in all_loans(session):
        loan_accounts.add(loan.account_id)
        state = loan_state(session, loan, on)
        if state.owed > 0:
            least = state.next.payment if state.next else state.owed
            debts.append(Debt(loan.name, state.owed, terms_of(loan).gross_rate, min(least, state.owed)))
    named: dict[int, tuple] = {}
    for query in list(rates) + list(minimums):
        acc = find_account(session, query)
        if acc.id in loan_accounts:
            raise FinanceError(f"{acc.path} is a loan: its rate and installment come from its terms.")
        if acc.type != LIABILITY:
            raise FinanceError(f"{acc.path} is not a debt.")
        if (acc.commodity_code or "TRY") != "TRY":
            raise FinanceError(f"{acc.path} is in {acc.commodity_code}; the repayment plan works in TRY.")
        _, rate, least = named.get(acc.id, (acc, None, None))
        if query in rates:
            rate = rates[query]
        if query in minimums:
            least = minimums[query]
        named[acc.id] = (acc, rate, least)
    leaves = [acc.path.split(":")[-1] for acc, _, _ in named.values()] + [d.name for d in debts]
    for acc, rate, least in named.values():
        owed = -account_balance(session, acc, on).get("TRY", Decimal(0))
        if owed <= 0:
            raise FinanceError(f"{acc.path} owes nothing on {on}.")
        leaf = acc.path.split(":")[-1]
        debts.append(Debt(leaf if leaves.count(leaf) == 1 else acc.path, owed, rate or Decimal(0), least or Decimal(0)))
    return debts
