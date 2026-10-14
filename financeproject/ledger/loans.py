"""Loans in the ledger: their terms, payment plan, state and payments.

A loan is a liability account plus its terms (``Loan``). A payment is a
transaction of up to four legs: the account it is paid from, the principal (it
lowers the liability), the interest and the taxes (expenses; booked with the
interest unless the loan names an account of their own).

How a payment divides:

- the planned installment, paid while the ledger owes what the plan says: the
  plan's own split;
- any other payment of an installment: interest and taxes first, one month's
  worth on what was owed when the installment's month began, and the rest
  repays principal. Several payments of the same installment (partial
  collections) share that month's interest;
- a payment outside the installments (``number=None``, an early repayment):
  all principal;
- whatever is paid beyond the principal still owed counts as interest (late
  interest, fees).

A loan that started before the ledger is entered with ``opening_on``: the
installments due by that day count as paid, and the account opens at the
balance the plan leaves (or at the balance given).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from financeproject.core.kinds import EXPENSE, LIABILITY
from financeproject.core.loans import (
    CENT,
    METHODS,
    Installment,
    LoanTerms,
    annual_cost_rate,
    balance_after,
    same_day_months_later,
    schedule,
    split_interest,
)
from financeproject.errors import AccountError, LedgerError
from financeproject.ledger.accounts import find_account, get_or_create_account
from financeproject.ledger.models import (
    Account,
    Loan,
    LoanPayment,
    Posting,
    Transaction,
)
from financeproject.ledger.queries import account_balance
from financeproject.ledger.transactions import (
    MANUAL_SOURCE,
    Leg,
    add_transaction,
    opening_balance,
)

LOAN_KINDS = ("consumer_loan", "vehicle_loan", "mortgage", "other_debt", "personal_debt")
DEFAULT_TAXES = {
    # kind: (KKDF, BSMV) as fractions of the interest, as of 2026; the loan's own papers decide
    "consumer_loan": (Decimal("0.15"), Decimal("0.15")),
    "vehicle_loan": (Decimal("0.15"), Decimal("0.15")),
    "mortgage": (Decimal(0), Decimal(0)),
    "other_debt": (Decimal(0), Decimal(0)),
    "personal_debt": (Decimal(0), Decimal(0)),
}
DEFAULT_INTEREST_ACCOUNT = "Expenses:Interest and Fees"
SETTLE = Decimal(1)
"""At the last installment a difference this small from what is owed is rounding: the loan is cleared."""


def terms_of(loan: Loan) -> LoanTerms:
    return LoanTerms(
        principal=loan.principal,
        monthly_rate=loan.monthly_rate,
        count=loan.count,
        first_due=loan.first_due,
        method=loan.method,
        kkdf=loan.kkdf,
        bsmv=loan.bsmv,
    )


def plan(loan: Loan) -> list[Installment]:
    return schedule(terms_of(loan))


def cost_rate(loan: Loan) -> float:
    """The annual cost rate of the loan's terms: fees and installments against the principal."""
    paid = [(loan.disbursed, loan.fees)] if loan.fees else []
    paid += [(i.due, i.payment) for i in plan(loan)]
    return annual_cost_rate([(loan.disbursed, loan.principal)], paid)


def get_loan(session: Session, name_or_account: str) -> Loan:
    """A loan by its name or by (part of) its account's name."""
    found = list(session.scalars(select(Loan).options(selectinload(Loan.account))))
    for loan in found:
        if loan.name.casefold() == name_or_account.strip().casefold():
            return loan
    try:
        account = find_account(session, name_or_account)
    except AccountError:
        account = None
    for loan in found:
        if account is not None and loan.account_id == account.id:
            return loan
    names = ", ".join(sorted(loan.name for loan in found)) or "none yet"
    raise LedgerError(f"No loan named {name_or_account!r}. Loans: {names}")


def _plain(reference: str) -> str:
    return re.sub(r"[\W_]", "", reference).casefold()


def loan_by_reference(session: Session, reference: str) -> Loan | None:
    """The loan whose bank number is ``reference`` (spaces, dashes and case do not matter)."""
    wanted = _plain(reference)
    for loan in session.scalars(select(Loan).where(Loan.reference.is_not(None))):
        if _plain(loan.reference) == wanted:
            return loan
    return None


def owed(session: Session, loan: Loan, on: date) -> Decimal:
    """Principal owed on ``on`` according to the ledger (positive)."""
    return -account_balance(session, loan.account, on).get(loan.account.commodity_code or "TRY", Decimal(0))


def add_loan(
    session: Session,
    name: str,
    account: str | Account,
    *,
    principal: Decimal,
    monthly_rate: Decimal,
    count: int,
    first_due: date,
    disbursed: date | None = None,
    method: str = "annuity",
    kkdf: Decimal | None = None,
    bsmv: Decimal | None = None,
    fees: Decimal = Decimal(0),
    reference: str | None = None,
    interest_account: str = DEFAULT_INTEREST_ACCOUNT,
    tax_account: str | None = None,
    disburse_to: str | Account | None = None,
    opening_on: date | None = None,
    opening_owed: Decimal | None = None,
) -> Loan:
    """Record a loan's terms; optionally book its disbursement or its opening balance.

    ``kkdf`` and ``bsmv`` default by the account's kind (``DEFAULT_TAXES``).
    Without ``disburse_to`` or ``opening_on`` the account's own transactions
    tell the balance; installments due before the first of them count as paid.
    """
    acc = find_account(session, account)
    if acc.type != LIABILITY or acc.kind not in LOAN_KINDS:
        kinds = ", ".join(LOAN_KINDS)
        raise LedgerError(f"A loan needs a liability account of kind {kinds}: {acc.path} is {acc.kind or acc.type}.")
    if session.scalar(select(Loan.id).where(Loan.account_id == acc.id)):
        raise LedgerError(f"{acc.path} already has a loan.")
    if not name.strip():
        raise LedgerError("A loan needs a name.")
    if session.scalar(select(Loan.id).where(func.lower(Loan.name) == name.strip().lower())):
        raise LedgerError(f"There is already a loan called {name!r}.")
    if reference and loan_by_reference(session, reference):
        raise LedgerError(f"Another loan already has the reference {reference!r}.")
    if method not in METHODS:
        raise LedgerError(f"Unknown method {method!r}; use one of: {', '.join(METHODS)}")
    if disburse_to is not None and opening_on is not None:
        raise LedgerError("Give either the account the loan was paid into or an opening date, not both.")
    if opening_owed is not None and opening_on is None:
        raise LedgerError("The balance owed at the start needs the opening date.")
    if fees < 0:
        raise LedgerError("Fees cannot be negative.")
    default_kkdf, default_bsmv = DEFAULT_TAXES[acc.kind]
    disbursed = disbursed or same_day_months_later(first_due, -1)
    if disbursed > first_due:
        raise LedgerError("The first installment cannot fall due before the loan is paid out.")
    loan = Loan(
        name=name.strip(),
        account=acc,
        principal=principal,
        monthly_rate=monthly_rate,
        count=count,
        first_due=first_due,
        disbursed=disbursed,
        method=method,
        kkdf=default_kkdf if kkdf is None else kkdf,
        bsmv=default_bsmv if bsmv is None else bsmv,
        fees=fees,
        reference=reference.strip() if reference else None,
        interest_account=get_or_create_account(session, interest_account),
        tax_account=get_or_create_account(session, tax_account) if tax_account else None,
    )
    try:
        terms = terms_of(loan)
    except ValueError as exc:
        raise LedgerError(str(exc)) from exc
    if loan.interest_account.type != EXPENSE or (loan.tax_account is not None and loan.tax_account.type != EXPENSE):
        raise LedgerError("The interest and tax accounts must be expense accounts.")
    first_posting = session.scalar(
        select(func.min(Transaction.date)).join(Posting).where(Posting.account_id == acc.id)
    )
    if disburse_to is not None:
        if first_posting is not None:
            raise LedgerError(f"{acc.path} already has transactions; the disbursement would count twice.")
        target = find_account(session, disburse_to)
        legs = [Leg(target, principal - fees), Leg(acc, -principal)]
        if fees:
            legs.insert(1, Leg(loan.interest_account, fees))
        add_transaction(session, disbursed, f"Loan paid out: {loan.name}", legs, source="loan")
    elif opening_on is not None:
        if first_posting is not None:
            raise LedgerError(f"{acc.path} already has transactions; an opening balance would count them twice.")
        paid = sum(1 for k in range(1, count + 1) if terms.due_date(k) <= opening_on)
        loan.start_number = paid + 1
        remaining = balance_after(terms, paid) if opening_owed is None else opening_owed
        if remaining > 0:
            opening_balance(session, acc, remaining, opening_on, f"Opening balance: {loan.name}, {paid} installments paid")
    elif first_posting is not None:
        loan.start_number = 1 + sum(1 for k in range(1, count + 1) if terms.due_date(k) < first_posting)
    session.add(loan)
    session.flush()
    return loan


def delete_loan(session: Session, loan: Loan) -> None:
    """Forget the loan's terms; its account and transactions stay."""
    session.delete(loan)
    session.flush()


def paid_numbers(loan: Loan) -> set[int]:
    return {p.number for p in loan.payments if p.number is not None}


def next_number(loan: Loan) -> int | None:
    """The first installment kept in the ledger that has not been paid."""
    done = paid_numbers(loan)
    for k in range(loan.start_number, loan.count + 1):
        if k not in done:
            return k
    return None


def paid_toward(loan: Loan, number: int) -> tuple[Decimal, Decimal]:
    """Principal, and interest with taxes, that earlier payments of installment ``number`` covered."""
    principal = cost = Decimal(0)
    charges = {loan.interest_account_id, loan.tax_account_id} - {None}
    for link in loan.payments:
        if link.number != number:
            continue
        for posting in link.transaction.postings:
            if posting.account_id == loan.account_id:
                principal += posting.amount
            elif posting.account_id in charges:
                cost += posting.amount
    return principal, cost


@dataclass(frozen=True)
class PaymentSplit:
    number: int | None
    principal: Decimal
    interest: Decimal
    """Interest, with whatever was paid beyond the month's interest and the principal owed."""
    kkdf: Decimal
    bsmv: Decimal
    as_planned: bool

    @property
    def amount(self) -> Decimal:
        return self.principal + self.interest + self.kkdf + self.bsmv

    @property
    def taxes(self) -> Decimal:
        return self.kkdf + self.bsmv


def _cost_parts(loan: Loan, cost: Decimal) -> tuple[Decimal, Decimal, Decimal]:
    """``cost`` divided into interest, KKDF and BSMV in the loan's proportions."""
    interest = (cost / (1 + loan.kkdf + loan.bsmv)).quantize(CENT)
    kkdf = (interest * loan.kkdf).quantize(CENT)
    return interest, kkdf, cost - interest - kkdf


def split_payment(
    loan: Loan,
    amount: Decimal,
    balance: Decimal,
    number: int | None = None,
    *,
    paid_principal: Decimal = Decimal(0),
    paid_cost: Decimal = Decimal(0),
    strict: bool = True,
) -> PaymentSplit:
    """How ``amount`` divides into principal, interest and taxes when ``balance`` is owed.

    ``paid_principal`` and ``paid_cost`` are what earlier payments of the same
    installment covered. With ``strict``, paying more than the loan owes (with
    the month's interest) is an error rather than extra interest.
    """
    if amount <= 0:
        raise LedgerError("A loan payment must be positive.")
    start = balance + paid_principal  # owed when the installment's month began
    if balance <= 0 or start <= 0:
        raise LedgerError(f"{loan.name} has nothing left to pay.")
    if number is None:
        principal = min(amount, balance)
        excess = amount - principal
        if strict and excess > 0:
            raise LedgerError(f"{amount} is more than {loan.name} still owes ({balance}).")
        return PaymentSplit(None, principal, excess, Decimal(0), Decimal(0), False)
    installments = plan(loan)
    if not paid_principal and not paid_cost and 1 <= number <= len(installments):
        planned = installments[number - 1]
        if planned.payment == amount and planned.balance + planned.principal == balance:
            return PaymentSplit(number, planned.principal, planned.interest, planned.kkdf, planned.bsmv, True)
    interest, kkdf, bsmv = split_interest(terms_of(loan), start)
    due = interest + kkdf + bsmv
    left = max(due - paid_cost, Decimal(0))
    if left == due and amount >= due:
        cost_parts = (interest, kkdf, bsmv)
    else:
        cost_parts = _cost_parts(loan, min(amount, left))
    principal = amount - sum(cost_parts, Decimal(0))
    interest, kkdf, bsmv = cost_parts
    last = number >= loan.count
    if last and 0 < balance - principal <= SETTLE and interest >= balance - principal:
        interest -= balance - principal  # rounding left by the plan: the last installment clears the loan
        principal = balance
    if principal > balance:
        excess = principal - balance
        if strict and excess > (SETTLE if last else 0):
            raise LedgerError(
                f"{amount} is more than {loan.name} still owes with this month's interest ({balance + due - paid_cost})."
            )
        principal, interest = balance, interest + excess
    return PaymentSplit(number, principal, interest, kkdf, bsmv, False)


def payment_legs(loan: Loan, split: PaymentSplit, paid_from: Account, amount: Decimal) -> list[Leg]:
    unit = loan.account.commodity_code or "TRY"
    legs = [Leg(paid_from, -amount)]
    if split.principal:
        legs.append(Leg(loan.account, split.principal))
    if loan.tax_account is None:
        if split.interest + split.taxes:
            legs.append(Leg(loan.interest_account, split.interest + split.taxes, unit))
    else:
        if split.interest:
            legs.append(Leg(loan.interest_account, split.interest, unit))
        if split.taxes:
            legs.append(Leg(loan.tax_account, split.taxes, unit))
    return legs


def record_payment(
    session: Session,
    loan: Loan,
    on: date,
    paid_from: str | Account,
    amount: Decimal | None = None,
    number: int | None = None,
    *,
    extra: bool = False,
    description: str | None = None,
    payee: str | None = None,
    source: str = MANUAL_SOURCE,
    import_key: str | None = None,
    partial: bool = False,
) -> Transaction:
    """Book a payment of ``loan``: by default the next installment, as planned.

    ``extra`` books an early repayment (all principal) instead of an
    installment. ``partial`` accepts another payment of an installment that
    already has one (a bank collecting an installment in parts) and books
    overpayments as interest instead of refusing them; imports use it.
    """
    if extra:
        if amount is None:
            raise LedgerError("An early repayment needs an amount.")
        number = None
    elif number is None:
        number = next_number(loan)
        if number is None:
            raise LedgerError(f"All installments of {loan.name} are paid; an early repayment needs --extra.")
    elif number in paid_numbers(loan) and not partial:
        raise LedgerError(f"Installment {number} of {loan.name} is already paid.")
    if number is not None and number < loan.start_number:
        raise LedgerError(
            f"Installment {number} of {loan.name} counts as paid before the ledger began "
            f"(the ledger keeps installments from {loan.start_number} on)."
        )
    paid_principal, paid_cost = paid_toward(loan, number) if number is not None else (Decimal(0), Decimal(0))
    if amount is None:
        if not 1 <= number <= loan.count:
            raise LedgerError(f"{loan.name} has installments 1 to {loan.count}; give an amount for any other payment.")
        amount = plan(loan)[number - 1].payment
    balance = owed(session, loan, on)
    if balance <= 0:
        raise LedgerError(f"{loan.name} owed nothing on {on}; is its balance (opening or payout) in the ledger from an earlier date?")
    split = split_payment(
        loan, amount, balance, number, paid_principal=paid_principal, paid_cost=paid_cost, strict=not partial
    )
    account = find_account(session, paid_from)
    if account.id == loan.account_id:
        raise LedgerError("A loan cannot be paid from its own account.")
    if description is None:
        description = f"{loan.name}: installment {number}" if number is not None else f"{loan.name}: early repayment"
    tx = add_transaction(
        session,
        on,
        description,
        payment_legs(loan, split, account, amount),
        payee=payee,
        source=source,
        import_key=import_key,
    )
    tx.loan_payment = LoanPayment(loan=loan, number=number)
    session.flush()
    return tx


def all_loans(session: Session) -> list[Loan]:
    return list(
        session.scalars(
            select(Loan)
            .options(selectinload(Loan.account), selectinload(Loan.payments).selectinload(LoanPayment.transaction))
            .order_by(Loan.name)
        )
    )


@dataclass(frozen=True)
class LoanState:
    loan: Loan
    on: date
    owed: Decimal
    """Principal owed according to the ledger."""
    paid: int
    """Installments paid: in the ledger and, for a loan started earlier, before it."""
    remaining: tuple[Installment, ...]
    """Installments still to pay (overdue ones too)."""
    replanned: bool
    """True when the ledger's balance differs from the plan's and ``remaining`` was worked out from the ledger."""

    @property
    def next(self) -> Installment | None:
        return self.remaining[0] if self.remaining else None

    @property
    def still_to_pay(self) -> Decimal:
        return sum((i.payment for i in self.remaining), Decimal(0))

    @property
    def last_due(self) -> date | None:
        return self.remaining[-1].due if self.remaining else None


def loan_state(session: Session, loan: Loan, on: date) -> LoanState:
    """What the loan owes on ``on`` and the installments left, from the plan or, if they differ, the ledger."""
    balance = owed(session, loan, on)
    done = {n for n in paid_numbers(loan) if n is not None}
    numbers = [k for k in range(loan.start_number, loan.count + 1) if k not in done]
    paid = loan.start_number - 1 + len(done & set(range(loan.start_number, loan.count + 1)))
    if balance <= 0 or not numbers:
        return LoanState(loan, on, max(balance, Decimal(0)), paid, (), False)
    installments = plan(loan)
    expected = installments[numbers[0] - 1].balance + installments[numbers[0] - 1].principal
    if abs(expected - balance) <= SETTLE and numbers == list(range(numbers[0], loan.count + 1)):
        return LoanState(loan, on, balance, paid, tuple(installments[k - 1] for k in numbers), False)
    terms = terms_of(loan)
    rest = LoanTerms(
        principal=balance,
        monthly_rate=terms.monthly_rate,
        count=len(numbers),
        first_due=terms.due_date(numbers[0]),
        method=terms.method,
        kkdf=terms.kkdf,
        bsmv=terms.bsmv,
    )
    replanned = tuple(
        Installment(k, terms.due_date(k), i.payment, i.principal, i.interest, i.kkdf, i.bsmv, i.balance)
        for k, i in zip(numbers, schedule(rest))
    )
    return LoanState(loan, on, balance, paid, replanned, True)
