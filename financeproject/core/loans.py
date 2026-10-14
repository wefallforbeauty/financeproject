"""Loans: payment schedules, the annual cost rate, present values, repayment plans.

Turkish banks quote a monthly contractual interest rate ``r``. On consumer loans
the state also takes KKDF (Resource Utilization Support Fund) and BSMV (Banking
and Insurance Transactions Tax) as fractions of every interest payment, so the
borrower pays interest at the gross rate ``g = r (1 + kkdf + bsmv)``, and the
installments are computed with it:

- annuity (equal installments): ``A = P g / (1 − (1 + g)^−n)``;
- equal principal: ``P / n`` of principal each month plus interest and taxes.

Every month the interest is the balance times ``r``, the taxes are fractions of
that interest, and the rest of the installment repays principal. Each part is
rounded to the kuruş and the last installment takes what is left, which
reproduces the payment plans banks publish (the İş Bankası example in the
tests: 10,000 TRY at 1 % a month for 12 months is 888.49 a month, 888.47 last).

The annual cost rate (yıllık maliyet oranı, the APR of EU Directive 2008/48/EC)
is the rate ``X`` at which the money received and the money paid back have the
same present value, with time measured in years as the regulation prescribes
(``year_fraction``). Methods and references: docs/METHOD.md.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from financeproject.core.dates import add_months, days_in_month

CENT = Decimal("0.01")
ANNUITY, EQUAL_PRINCIPAL = "annuity", "equal_principal"
METHODS = (ANNUITY, EQUAL_PRINCIPAL)


def cents(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def same_day_months_later(start: date, months: int) -> date:
    """The date ``months`` months after ``start``, on the same day of the month if it exists."""
    m = add_months(start.replace(day=1), months)
    return m.replace(day=min(start.day, days_in_month(m)))


@dataclass(frozen=True)
class LoanTerms:
    principal: Decimal
    monthly_rate: Decimal
    """Contractual interest rate per month, as a fraction (3.49 % → 0.0349)."""
    count: int
    """Number of installments."""
    first_due: date
    """Date of the first installment; the others follow monthly on the same day."""
    method: str = ANNUITY
    kkdf: Decimal = Decimal(0)
    bsmv: Decimal = Decimal(0)
    """KKDF and BSMV as fractions of the interest (15 % → 0.15)."""

    def __post_init__(self):
        if self.principal <= 0:
            raise ValueError("The principal must be positive.")
        if self.monthly_rate < 0:
            raise ValueError("The interest rate cannot be negative.")
        if self.count < 1:
            raise ValueError("A loan needs at least one installment.")
        if self.method not in METHODS:
            raise ValueError(f"Unknown method {self.method!r}; use one of: {', '.join(METHODS)}")
        if self.kkdf < 0 or self.bsmv < 0:
            raise ValueError("KKDF and BSMV cannot be negative.")

    @property
    def tax_factor(self) -> Decimal:
        return 1 + self.kkdf + self.bsmv

    @property
    def gross_rate(self) -> Decimal:
        """Interest and taxes per month: ``r (1 + kkdf + bsmv)``."""
        return self.monthly_rate * self.tax_factor

    def due_date(self, number: int) -> date:
        return same_day_months_later(self.first_due, number - 1)


@dataclass(frozen=True)
class Installment:
    number: int
    due: date
    payment: Decimal
    principal: Decimal
    interest: Decimal
    kkdf: Decimal
    bsmv: Decimal
    balance: Decimal
    """Principal still owed after this installment."""

    @property
    def taxes(self) -> Decimal:
        return self.kkdf + self.bsmv

    @property
    def cost(self) -> Decimal:
        """Interest and taxes: what the installment pays beyond principal."""
        return self.interest + self.taxes


def annuity_payment(principal: Decimal, rate: Decimal, count: int) -> Decimal:
    """``P g / (1 − (1 + g)^−n)``, unrounded; ``P / n`` when the rate is zero."""
    if rate == 0:
        return principal / count
    return principal * rate / (1 - (1 + rate) ** -count)


def split_interest(terms: LoanTerms, balance: Decimal) -> tuple[Decimal, Decimal, Decimal]:
    """One month's interest on ``balance`` and the KKDF and BSMV on it, each to the kuruş."""
    interest = cents(balance * terms.monthly_rate)
    return interest, cents(interest * terms.kkdf), cents(interest * terms.bsmv)


def schedule(terms: LoanTerms) -> list[Installment]:
    """The payment plan: every installment with its principal, interest, taxes and balance."""
    out: list[Installment] = []
    balance = terms.principal
    level = cents(annuity_payment(terms.principal, terms.gross_rate, terms.count))
    for k in range(1, terms.count + 1):
        interest, kkdf, bsmv = split_interest(terms, balance)
        if k == terms.count:
            principal = balance
        elif terms.method == ANNUITY:
            principal = min(level - interest - kkdf - bsmv, balance)
        else:
            principal = min(cents(terms.principal / terms.count), balance)
        balance -= principal
        out.append(Installment(k, terms.due_date(k), principal + interest + kkdf + bsmv, principal, interest, kkdf, bsmv, balance))
    return out


def balance_after(terms: LoanTerms, paid: int) -> Decimal:
    """Principal owed after the first ``paid`` installments."""
    if paid <= 0:
        return terms.principal
    plan = schedule(terms)
    return plan[min(paid, len(plan)) - 1].balance


# --------------------------------------------------------------------------- time and rates


def _year_length(last: date) -> int:
    """Days in the year counted back from ``last`` to the same day a year earlier (365 or 366)."""
    try:
        earlier = last.replace(year=last.year - 1)
    except ValueError:  # 29 February
        earlier = last.replace(year=last.year - 1, day=28)
    return (last - earlier).days


def year_fraction(start: date, end: date) -> float:
    """Time from ``start`` to ``end`` in years, as the annual cost rate rules count it.

    Whole months are counted back from ``end`` (a month is 1/12 year); the days
    left over are divided by the length of the year that ends on the last of
    them (365, or 366 when it holds 29 February). Directive 2008/48/EC Annex I
    and the Turkish consumer credit regulation use the same rule.
    """
    if end < start:
        return -year_fraction(end, start)
    months = 0
    while True:
        candidate = add_months(end.replace(day=1), -(months + 1))
        candidate = candidate.replace(day=min(end.day, days_in_month(candidate)))
        if candidate < start:
            break
        months += 1
    last = add_months(end.replace(day=1), -months)
    last = last.replace(day=min(end.day, days_in_month(last)))
    days = (last - start).days
    return months / 12 + (days / _year_length(last) if days else 0.0)


def solve(f: Callable[[float], float], low: float, high: float, tolerance: float = 1e-12) -> float:
    """A root of the monotonic function ``f`` between ``low`` and ``high`` (bisection)."""
    f_low, f_high = f(low), f(high)
    if f_low == 0:
        return low
    if f_high == 0:
        return high
    if (f_low > 0) == (f_high > 0):
        raise ValueError("No rate solves the equation in the range searched.")
    for _ in range(300):
        mid = (low + high) / 2
        f_mid = f(mid)
        if f_mid == 0 or high - low < tolerance:
            return mid
        if (f_mid > 0) == (f_low > 0):
            low, f_low = mid, f_mid
        else:
            high = mid
    return (low + high) / 2


Flow = tuple[date, Decimal]


def _cost_rate(timed: Sequence[tuple[float, float]]) -> float:
    """The annual rate at which flows (years from now, amount; paid +, received −) are worth zero."""
    if not any(a > 0 for _, a in timed) or not any(a < 0 for _, a in timed):
        raise ValueError("Money must flow both ways: what is received and what is paid back.")

    def pv(x: float) -> float:
        return sum(a * (1 + x) ** -t for t, a in timed)

    return solve(pv, -0.99, 1_000.0)


def annual_cost_rate(received: Sequence[Flow], paid: Sequence[Flow]) -> float:
    """The annual rate ``X`` with Σ received (1+X)^−t = Σ paid (1+X)^−t (yıllık maliyet oranı).

    ``received`` is what the borrower gets (the credit paid out), ``paid`` what
    the borrower pays (installments, fees, insurance), each with its date; ``t``
    is measured in years from the first date, by ``year_fraction``.
    """
    flows = [(d, -float(a)) for d, a in received] + [(d, float(a)) for d, a in paid]
    if not flows:
        raise ValueError("No cash flows.")
    origin = min(d for d, _ in flows)
    return _cost_rate([(year_fraction(origin, d), a) for d, a in flows])


def offer_cost_rate(
    amount: Decimal, payments: Sequence[Decimal], fees: Decimal = Decimal(0), first_in: int = 1
) -> float:
    """The annual cost rate of an offer: ``amount`` received and ``fees`` paid now,
    ``payments[k]`` paid ``first_in + k`` months later (a month is 1/12 year)."""
    timed = [(0.0, -float(amount)), (0.0, float(fees))]
    timed += [((first_in + k) / 12, float(p)) for k, p in enumerate(payments)]
    return _cost_rate([(t, a) for t, a in timed if a])


def implied_monthly_rate(principal: Decimal, payment: Decimal, count: int, tax_factor: Decimal = Decimal(1)) -> Decimal:
    """The contractual monthly rate behind a level ``payment`` (taxes included in it).

    Solves ``P = A (1 − (1 + g)^−n) / g`` for the gross rate ``g`` and divides by
    ``1 + kkdf + bsmv``. Useful when the bank's papers show the installment but
    not the rate. Rounded to six decimals (0.034900 = 3.49 %).
    """
    p, a = float(principal), float(payment)
    if principal <= 0 or payment <= 0 or count < 1:
        raise ValueError("The principal and the installment must be positive.")
    if a * count < p - 0.005 * count:
        raise ValueError("The installments add up to less than the principal.")
    if a * count <= p + 0.005 * count:
        return Decimal(0)

    def gap(g: float) -> float:
        return a * (1 - (1 + g) ** -count) / g - p

    gross = solve(gap, 1e-9, 10.0)
    return (Decimal(repr(gross)) / tax_factor).quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)


def irr(flows: Sequence[Decimal | float], low: float = -0.99, high: float = 100.0) -> float:
    """Internal rate of return per period of equally spaced flows (the first at time 0)."""
    values = [float(v) for v in flows]

    def npv(x: float) -> float:
        return sum(v * (1 + x) ** -k for k, v in enumerate(values))

    return solve(npv, low, high)


def effective_annual(monthly: float) -> float:
    """``(1 + monthly)^12 − 1``."""
    return (1 + monthly) ** 12 - 1


def monthly_from_annual(annual: float) -> float:
    """The monthly rate that compounds to ``annual`` in a year."""
    return (1 + annual) ** (1 / 12) - 1


def present_value(amounts: Sequence[Decimal | float], monthly_rate: float, first: int = 1) -> float:
    """Value today of payments due in months ``first``, ``first + 1``, …, discounted monthly."""
    return sum(float(a) * (1 + monthly_rate) ** -(first + k) for k, a in enumerate(amounts))


# --------------------------------------------------------------------------- cash or installments


@dataclass(frozen=True)
class CashOrInstallments:
    cash_price: Decimal
    installment: Decimal
    count: int
    first_in: int
    """Months until the first installment (1 = a month from now)."""
    discount_rate: float
    """Monthly rate the money could earn meanwhile."""
    present_value: float
    """Value today of the installments at ``discount_rate``."""
    implied_rate: float
    """Monthly rate at which the installments are worth exactly the cash price."""

    @property
    def total(self) -> Decimal:
        return self.installment * self.count

    @property
    def advantage(self) -> float:
        """How much cheaper (positive) or dearer the installments are, in today's money."""
        return float(self.cash_price) - self.present_value

    @property
    def installments_win(self) -> bool:
        return self.advantage > 0


def cash_or_installments(
    cash_price: Decimal, installment: Decimal, count: int, discount_rate: float, first_in: int = 1
) -> CashOrInstallments:
    """Compare paying ``cash_price`` today with ``count`` installments of ``installment``.

    The installments are discounted at ``discount_rate`` per month: the return
    the money would earn if it stayed (a deposit), or inflation for a
    comparison in real terms. The implied rate is the monthly interest the
    installment plan charges against the cash price.
    """
    if cash_price <= 0 or installment <= 0 or count < 1 or first_in < 0:
        raise ValueError("The cash price and the installment must be positive, with at least one installment.")
    pv = present_value([installment] * count, discount_rate, first_in)
    flows = [float(cash_price)] + [0.0] * max(first_in - 1, 0) + [-float(installment)] * count
    if first_in == 0:
        flows = [float(cash_price) - float(installment)] + [-float(installment)] * (count - 1)
    implied = irr(flows, -0.99, 10.0) if any(v < 0 for v in flows) else 0.0
    return CashOrInstallments(cash_price, installment, count, first_in, discount_rate, pv, implied)


# --------------------------------------------------------------------------- repayment strategies

AVALANCHE, SNOWBALL, LEAST = "avalanche", "snowball", "least"
STRATEGIES = (AVALANCHE, SNOWBALL, LEAST)


@dataclass
class Debt:
    name: str
    balance: Decimal
    monthly_rate: Decimal
    """Gross monthly rate charged on the balance (interest and taxes)."""
    minimum: Decimal
    """Least payment each month (a loan's installment, a card's minimum payment)."""


@dataclass
class PayoffResult:
    strategy: str
    months: int
    interest: Decimal
    paid_off: dict[str, int] = field(default_factory=dict)
    """Debt → the month (1, 2, …) it was cleared."""
    feasible: bool = True
    """False when the budget does not cover the minimum payments."""

    @property
    def order(self) -> list[str]:
        return sorted(self.paid_off, key=lambda name: (self.paid_off[name], name))


def payoff(debts: Sequence[Debt], budget: Decimal, strategy: str, max_months: int = 600) -> PayoffResult:
    """Pay ``budget`` a month into ``debts`` until all are cleared.

    Every month each balance grows by its rate, every debt gets its minimum
    payment, and what is left of the budget goes to one debt at a time:

    - avalanche: the highest rate first (it minimizes the interest paid);
    - snowball: the smallest balance first (quick wins);
    - least: nothing beyond the minimum payments (``budget`` is not used), the
      way the debts run if nothing changes.

    With avalanche and snowball, freed minimum payments stay in the budget, so
    the payments "roll over".
    """
    if strategy not in STRATEGIES:
        raise ValueError(f"Unknown strategy {strategy!r}; use one of: {', '.join(STRATEGIES)}")
    balances = {d.name: d.balance for d in debts if d.balance > 0}
    info = {d.name: d for d in debts}
    result = PayoffResult(strategy, 0, Decimal(0))
    month = 0
    while balances and month < max_months:
        month += 1
        for name in balances:
            charge = cents(balances[name] * info[name].monthly_rate)
            balances[name] += charge
            result.interest += charge
        payments = {name: min(info[name].minimum, balance) for name, balance in balances.items()}
        left = Decimal(0) if strategy == LEAST else budget - sum(payments.values(), Decimal(0))
        if left < 0:
            result.feasible = False
            result.months = month
            return result
        if strategy == AVALANCHE:
            order = sorted(balances, key=lambda n: (-info[n].monthly_rate, balances[n], n))
        else:
            order = sorted(balances, key=lambda n: (balances[n], -info[n].monthly_rate, n))
        for name in order:
            extra = min(left, balances[name] - payments[name])
            payments[name] += extra
            left -= extra
        for name, amount in payments.items():
            balances[name] -= amount
        for name in [n for n, b in balances.items() if b <= 0]:
            result.paid_off[name] = month
            del balances[name]
    result.months = month
    if balances:
        result.feasible = False
    return result


def months_to_clear(balance: Decimal, monthly_rate: Decimal, payment: Decimal) -> int | None:
    """Months to repay ``balance`` with level ``payment`` (``None`` if it never ends)."""
    if payment <= 0:
        return None
    if monthly_rate == 0:
        return math.ceil(balance / payment)
    r = float(monthly_rate)
    if float(payment) <= float(balance) * r:
        return None
    return math.ceil(-math.log(1 - float(balance) * r / float(payment)) / math.log(1 + r))
