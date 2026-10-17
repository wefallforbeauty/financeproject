"""Writing imported rows into the ledger: categories, duplicates, all or nothing.

Every row becomes a two-legged transaction between its account and a counter
account. The counter account comes, in this order, from:

1. the file itself (a ``counter_account`` column);
2. the first matching categorization rule;
3. the learned model (``ingest.learn``), when its guess is confident enough;
4. ``Expenses:Uncategorized`` or ``Income:Uncategorized``.

A statement line that pays an installment of a loan defined in the ledger
(``finance loan add --reference``) is booked as a loan payment instead:
principal, interest and taxes (``ledger.loans``).

Credit card lists go into the card's account:

- the first installment of a purchase books the whole purchase with its
  installment plan; later installments of a purchase whose plan is in the
  ledger are already counted and are skipped; installments of purchases made
  before the ledger are booked one by one as they come;
- payments of the card become transfers from the account given with
  ``payments_from``.

A row whose counter account is another balance-sheet account is a transfer.
If the ledger already has that transfer (the same two accounts, the opposite
amounts, within three days), for example from the other account's statement,
the row is skipped.

Every row gets an import key, so importing the same or an overlapping file
again adds nothing twice:

- with the bank's own line number (``reference``): a hash of the account,
  date, amount and reference;
- without it: a hash of the account, date, amount and description;
- in both cases with the row's occurrence number among identical rows.

If any row is invalid, nothing is written and all errors are listed.
"""

from __future__ import annotations

import hashlib
from collections import Counter
from dataclasses import dataclass, field, replace
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from financeproject.core.dates import add_months, month_start
from financeproject.core.kinds import ASSET, BALANCE_SHEET_TYPES, EXPENSE, LIABILITY
from financeproject.core.text import fold
from financeproject.errors import FinanceError
from financeproject.ingest import learn
from financeproject.ingest.rules import load_rules, match
from financeproject.ingest.statement import Statement, StatementLine
from financeproject.ledger.accounts import find_account
from financeproject.ledger.db import UNCATEGORIZED_EXPENSE, UNCATEGORIZED_INCOME
from financeproject.ledger.installments import (
    matching_plan,
    record_installment_purchase,
)
from financeproject.ledger.loans import loan_by_reference, owed, record_payment
from financeproject.ledger.models import Account, Loan, Posting, Transaction
from financeproject.ledger.queries import account_balance
from financeproject.ledger.transactions import Leg, add_transaction, opening_balance

HOW_FILE, HOW_UNCATEGORIZED, HOW_LOAN = "file", "uncategorized", "loan"
HOW_INSTALLMENT, HOW_TRANSFER, HOW_PAYMENT = "installment", "transfer", "card payment"
TRANSFER_DAYS = 3
"""Two sides of a transfer are taken for the same transfer up to this many days apart."""


@dataclass(frozen=True, slots=True)
class Row:
    line: int
    date: date
    description: str
    amount: Decimal
    """Signed change of ``account``: negative = money out."""
    account: str | None = None
    counter_account: str | None = None
    commodity: str | None = None
    payee: str | None = None
    note: str | None = None
    reference: str | None = None
    loan_id: int | None = None
    """The loan this row pays (statement lines that name a defined loan)."""
    installment: int | None = None
    installments: int | None = None
    """A card purchase in this many installments: booked in full with its plan."""
    first_due: date | None = None
    """The month of the first installment of such a purchase."""
    covered_by: int | None = None
    """The transaction that already holds this row (an installment of a purchase in the ledger)."""
    how: str = HOW_FILE
    """How the row's own ``counter_account`` was chosen, as the preview shows it."""


def import_key(account_path: str, row: Row, occurrence: int) -> str:
    amount = format(row.amount.normalize(), "f")
    what = f"ref:{fold(row.reference)}" if row.reference else fold(row.description)
    raw = f"{fold(account_path)}|{row.date.isoformat()}|{amount}|{what}|{occurrence}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


@dataclass(frozen=True, slots=True)
class PreviewRow:
    line: int
    date: date
    description: str
    amount: Decimal
    commodity: str
    account: str
    counter_account: str
    how: str
    """``file``, ``rule #n``, ``model 0.93`` or ``uncategorized``."""
    duplicate: bool
    payee: str | None = None


@dataclass(frozen=True)
class BalanceCheck:
    """The bank's closing balance against the ledger's balance on the same day."""

    on: date
    bank: Decimal
    ledger: Decimal
    predicted: bool
    """True on a dry run: the ledger balance is what it would be after the import."""

    @property
    def difference(self) -> Decimal:
        return self.ledger - self.bank

    @property
    def ok(self) -> bool:
        return self.difference == 0


@dataclass
class ImportResult:
    path: Path
    source: str
    """What was read, for example ``CSV, utf-8`` or ``Ziraat Bankası xlsx``."""
    rows: list[PreviewRow] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    written: bool = False
    opening: tuple[date, Decimal] | None = None
    """The opening balance taken from a statement (added, or to be added on a dry run)."""
    balance_check: BalanceCheck | None = None
    unknown_loans: dict[str, int] = field(default_factory=dict)
    """Loan numbers on installment lines that no defined loan has, with their line counts."""
    older_installments: int = 0
    """Card installments of purchases made before the ledger, booked one by one."""

    @property
    def new(self) -> int:
        return sum(1 for r in self.rows if not r.duplicate)

    @property
    def added(self) -> int:
        return self.new if self.written else 0

    @property
    def duplicates(self) -> int:
        return sum(1 for r in self.rows if r.duplicate)

    @property
    def uncategorized(self) -> int:
        return sum(1 for r in self.rows if not r.duplicate and r.how == HOW_UNCATEGORIZED)

    @property
    def learned(self) -> int:
        return sum(1 for r in self.rows if not r.duplicate and r.how.startswith("model"))

    @property
    def loan_payments(self) -> int:
        return sum(1 for r in self.rows if not r.duplicate and r.how.startswith(HOW_LOAN))

    @property
    def installment_purchases(self) -> int:
        """Card purchases booked in full with their installment plans."""
        return sum(1 for r in self.rows if not r.duplicate and HOW_INSTALLMENT in r.how)

    @property
    def covered_installments(self) -> int:
        """Card installments of purchases already in the ledger."""
        return sum(1 for r in self.rows if r.duplicate and r.how.startswith(HOW_INSTALLMENT))

    @property
    def matched_transfers(self) -> int:
        """Transfers the ledger already had from the other account."""
        return sum(1 for r in self.rows if r.duplicate and r.how.startswith(HOW_TRANSFER))


def _check_loan_row(session: Session, loan: Loan, row: Row) -> None:
    """Refuse an installment line the loan's start in the ledger cannot take."""
    if row.installment is not None and row.installment < loan.start_number:
        raise FinanceError(
            f"installment {row.installment} of {loan.name} counts as paid before the ledger began; "
            "add the loan again with an earlier --opening-on date"
        )
    if owed(session, loan, row.date) <= 0:
        raise FinanceError(f"{loan.name} owed nothing on {row.date}; its balance (opening or payout) must start earlier")


def _mirror(
    session: Session, account: Account, counter: Account, amount: Decimal, on: date, used: set[int]
) -> int | None:
    """A transaction already in the ledger that moves ``amount`` between the two accounts within
    ``TRANSFER_DAYS`` of ``on`` (the closest one not in ``used``)."""
    found = []
    for tx in session.scalars(
        select(Transaction)
        .join(Posting, Posting.transaction_id == Transaction.id)
        .where(
            Posting.account_id == account.id,
            Transaction.date >= on - timedelta(days=TRANSFER_DAYS),
            Transaction.date <= on + timedelta(days=TRANSFER_DAYS),
        )
    ).unique():
        legs = {(p.account_id, p.amount) for p in tx.postings}
        if tx.id not in used and len(tx.postings) == 2 and legs == {(account.id, amount), (counter.id, -amount)}:
            found.append(tx)
    if not found:
        return None
    best = min(found, key=lambda tx: (abs((tx.date - on).days), tx.id))
    used.add(best.id)
    return best.id


def import_rows(
    session: Session,
    rows: list[Row],
    *,
    path: Path,
    source: str,
    transaction_source: str,
    account: str | Account | None = None,
    errors: list[str] | None = None,
    dry_run: bool = False,
    use_model: bool = True,
    min_confidence: float = learn.DEFAULT_MIN_CONFIDENCE,
    opening: tuple[date, Decimal] | None = None,
) -> ImportResult:
    """Categorize, check and (unless ``dry_run`` or any row is invalid) write ``rows``.

    ``account`` is used for rows without one. ``opening`` adds an opening
    balance to ``account`` in the same all-or-nothing step.
    """
    result = ImportResult(Path(path), source, errors=list(errors or []), opening=opening)
    rules = load_rules(session)
    model = learn.train(session) if use_model else None
    default_account = find_account(session, account) if account else None
    uncategorized_expense = find_account(session, UNCATEGORIZED_EXPENSE)
    uncategorized_income = find_account(session, UNCATEGORIZED_INCOME)
    seen: Counter[tuple] = Counter()
    pending: list[tuple[Row, Account, Account, str]] = []
    mirrored: set[int] = set()

    for row in rows:
        if row.covered_by is not None:
            result.rows.append(
                PreviewRow(
                    row.line, row.date, row.description, row.amount, "TRY", default_account.path if default_account else "",
                    "", f"{HOW_INSTALLMENT} {row.installment}/{row.installments} of #{row.covered_by}", True, row.payee,
                )
            )
            continue
        try:
            acc = find_account(session, row.account) if row.account else default_account
            if acc is None:
                raise FinanceError("no account (the file needs an 'account' column, or give --account)")
            commodity = acc.commodity_code or row.commodity or "TRY"
            guess = None
            identity = fold(row.reference) if row.reference else fold(row.description)
            base = (acc.id, row.date, row.amount.normalize(), identity)
            seen[base] += 1
            key = import_key(acc.path, row, seen[base])
            duplicate = session.scalar(select(Transaction.id).where(Transaction.import_key == key)) is not None
            if row.loan_id is not None:
                loan = session.get(Loan, row.loan_id)
                if not duplicate:
                    _check_loan_row(session, loan, row)
                counter = loan.account
                how = f"{HOW_LOAN} {loan.name}" + (f" #{row.installment}" if row.installment else "")
            elif row.counter_account:
                counter, how = find_account(session, row.counter_account), row.how
            elif rule := match(rules, " ".join(filter(None, [row.description, row.payee]))):
                counter, how = rule.account, f"rule #{rule.rule_id}"
            elif (
                model is not None
                and (guess := model.guess(row.description, row.payee, acc.id, row.amount)) is not None
                and guess.confidence >= min_confidence
                and guess.account.commodity_code in (None, commodity)
            ):
                counter, how = guess.account, f"model {guess.confidence:.2f}"
            else:
                counter = uncategorized_expense if row.amount < 0 else uncategorized_income
                how = HOW_UNCATEGORIZED
            if row.installments is not None:
                if counter.type != EXPENSE:
                    raise FinanceError(f"an installment purchase needs an expense category, not {counter.path}")
                how += f"; {row.installments} {HOW_INSTALLMENT}s"
            if not duplicate and row.loan_id is None and counter.type in BALANCE_SHEET_TYPES:
                if (twin := _mirror(session, acc, counter, row.amount, row.date, mirrored)) is not None:
                    duplicate, how = True, f"{HOW_TRANSFER} #{twin}"
            result.rows.append(
                PreviewRow(
                    row.line, row.date, row.description, row.amount, commodity, acc.path, counter.path, how, duplicate, row.payee
                )
            )
            if not duplicate:
                pending.append((row, acc, counter, key))
        except FinanceError as exc:
            result.errors.append(f"line {row.line}: {exc}")

    if result.errors or dry_run:
        return result

    try:
        if opening is not None:
            if default_account is None:
                raise FinanceError("An opening balance needs --account.")
            opening_balance(session, default_account, opening[1], opening[0], "Opening balance (from the bank statement)")
        for row, acc, counter, key in pending:
            try:
                if row.loan_id is not None:
                    record_payment(
                        session,
                        session.get(Loan, row.loan_id),
                        row.date,
                        acc,
                        -row.amount,
                        row.installment,
                        description=row.description,
                        payee=row.payee,
                        source=transaction_source,
                        import_key=key,
                        partial=True,
                    )
                    continue
                if row.installments is not None:
                    record_installment_purchase(
                        session,
                        -row.amount,
                        counter,
                        acc,
                        row.date,
                        row.description,
                        row.installments,
                        row.first_due,
                        row.payee,
                        source=transaction_source,
                        import_key=key,
                    )
                    continue
                add_transaction(
                    session,
                    row.date,
                    row.description,
                    [
                        Leg(acc, row.amount, commodity=row.commodity),
                        Leg(counter, -row.amount, commodity=acc.commodity_code or row.commodity),
                    ],
                    payee=row.payee,
                    note=row.note,
                    source=transaction_source,
                    import_key=key,
                )
            except FinanceError as exc:
                result.errors.append(f"line {row.line}: {exc}")
    except FinanceError as exc:
        result.errors.append(str(exc))
    if result.errors:
        session.rollback()
        return result
    result.written = True
    return result


def has_postings(session: Session, account: Account) -> bool:
    return bool(session.scalar(select(func.count()).select_from(Posting).where(Posting.account_id == account.id)))


def _card_installment(session: Session, card: Account, line: StatementLine, row: Row) -> tuple[Row, bool]:
    """A card installment line as a row: covered by a plan in the ledger, the whole purchase with its
    plan (first installment), or the installment alone (purchase made before the ledger)."""
    number, count = line.installment, line.installment_count
    purchased = line.purchase_date or add_months(line.date, -(number - 1))
    plan = matching_plan(session, card, line.purchase_total, count, purchased)
    if plan is not None:
        return replace(row, installment=number, installments=count, covered_by=plan.transaction_id), False
    if number == 1:
        first = month_start(line.date)
        return replace(row, date=purchased, amount=-line.purchase_total, installments=count, first_due=first), False
    note = f"Installment {number}/{count} of a purchase of {line.purchase_total} on {purchased}"
    return replace(row, note=note), True


def import_statement(
    session: Session,
    statement: Statement,
    path: Path,
    account: str | Account,
    *,
    dry_run: bool = False,
    add_opening: bool = False,
    use_model: bool = True,
    min_confidence: float = learn.DEFAULT_MIN_CONFIDENCE,
    payments_from: str | Account | None = None,
) -> ImportResult:
    """Import a bank statement into its account and compare the balances afterwards.

    ``add_opening`` starts an account that has no transactions yet at the
    balance the statement had before its first line. ``payments_from`` is the
    account that pays a credit card, for the payments on a card list.
    """
    acc = find_account(session, account)
    if statement.card:
        if acc.type != LIABILITY or acc.kind != "credit_card":
            raise FinanceError(f"A card list goes into a credit card account: {acc.path}")
    elif acc.type != ASSET:
        raise FinanceError(f"Bank statements go into asset accounts (checking, time deposit, foreign currency): {acc.path}")
    if statement.currency and acc.commodity_code and statement.currency != acc.commodity_code:
        raise FinanceError(f"The statement is in {statement.currency} but {acc.path} holds {acc.commodity_code}.")
    payer = None
    if payments_from is not None:
        if not statement.card:
            raise FinanceError("--payments-from is for credit card lists.")
        payer = find_account(session, payments_from)
        if payer.type not in BALANCE_SHEET_TYPES or payer.id == acc.id:
            raise FinanceError(f"The card is paid from another asset or liability account, not {payer.path}.")
    opening = None
    if add_opening:
        if statement.opening_balance is None:
            raise FinanceError("This statement has no balances, so no opening balance can be taken from it.")
        if has_postings(session, acc):
            raise FinanceError(f"{acc.path} already has transactions; an opening balance from the statement would count them twice.")
        opening = (statement.start, statement.opening_balance)
    rows = []
    unknown_loans: Counter[str] = Counter()
    older: set[int] = set()
    for i, line in enumerate(statement.lines, start=1):
        loan = None
        if line.loan_reference and line.amount < 0:
            loan = loan_by_reference(session, line.loan_reference)
            if loan is None:
                unknown_loans[line.loan_reference] += 1
        row = Row(
            line=i,
            date=line.date,
            description=line.description,
            amount=line.amount,
            payee=line.payee,
            reference=line.reference,
            loan_id=loan.id if loan else None,
            installment=line.installment if loan else None,
        )
        if line.kind == "installment" and line.purchase_total is not None:
            row, is_older = _card_installment(session, acc, line, row)
            if is_older:
                older.add(i)
        elif line.kind == "payment" and payer is not None and line.amount > 0:
            row = replace(row, counter_account=payer.path, how=HOW_PAYMENT)
        rows.append(row)
    result = import_rows(
        session,
        rows,
        path=path,
        source=f"{statement.bank} {statement.file_format}",
        transaction_source=f"{statement.bank.split()[0].lower()}:{Path(path).name}"[:80],
        account=acc,
        dry_run=dry_run,
        use_model=use_model,
        min_confidence=min_confidence,
        opening=opening,
    )
    result.unknown_loans = dict(unknown_loans)
    result.older_installments = sum(1 for r in result.rows if not r.duplicate and r.line in older)
    if statement.closing_balance is not None and not result.errors:
        commodity = acc.commodity_code or "TRY"
        ledger = account_balance(session, acc, statement.end).get(commodity, Decimal(0))
        if not result.written:
            ledger += sum((r.amount for r in result.rows if not r.duplicate and r.date <= statement.end), Decimal(0))
            if opening is not None and opening[0] <= statement.end:
                ledger += opening[1]
        result.balance_check = BalanceCheck(statement.end, statement.closing_balance, ledger, not result.written)
    return result
