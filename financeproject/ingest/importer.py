"""Writing imported rows into the ledger: categories, duplicates, all or nothing.

Every row becomes a two-legged transaction between its account and a counter
account. The counter account comes, in this order, from:

1. the file itself (a ``counter_account`` column);
2. the first matching categorization rule;
3. the learned model (``ingest.learn``), when its guess is confident enough;
4. ``Expenses:Uncategorized`` or ``Income:Uncategorized``.

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
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from financeproject.core.kinds import ASSET
from financeproject.core.text import fold
from financeproject.errors import FinanceError
from financeproject.ingest import learn
from financeproject.ingest.rules import load_rules, match
from financeproject.ingest.statement import Statement
from financeproject.ledger.accounts import find_account
from financeproject.ledger.db import UNCATEGORIZED_EXPENSE, UNCATEGORIZED_INCOME
from financeproject.ledger.models import Account, Posting, Transaction
from financeproject.ledger.queries import account_balance
from financeproject.ledger.transactions import Leg, add_transaction, opening_balance

HOW_FILE, HOW_UNCATEGORIZED = "file", "uncategorized"


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

    for row in rows:
        try:
            acc = find_account(session, row.account) if row.account else default_account
            if acc is None:
                raise FinanceError("no account (the file needs an 'account' column, or give --account)")
            commodity = acc.commodity_code or row.commodity or "TRY"
            guess = None
            if row.counter_account:
                counter, how = find_account(session, row.counter_account), HOW_FILE
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
            identity = fold(row.reference) if row.reference else fold(row.description)
            base = (acc.id, row.date, row.amount.normalize(), identity)
            seen[base] += 1
            key = import_key(acc.path, row, seen[base])
            duplicate = session.scalar(select(Transaction.id).where(Transaction.import_key == key)) is not None
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
) -> ImportResult:
    """Import a bank statement into an asset account and compare the balances afterwards.

    ``add_opening`` starts an account that has no transactions yet at the
    balance the statement had before its first line.
    """
    acc = find_account(session, account)
    if acc.type != ASSET:
        raise FinanceError(f"Bank statements go into asset accounts (checking, time deposit, foreign currency): {acc.path}")
    if statement.currency and acc.commodity_code and statement.currency != acc.commodity_code:
        raise FinanceError(f"The statement is in {statement.currency} but {acc.path} holds {acc.commodity_code}.")
    opening = None
    if add_opening:
        if statement.opening_balance is None:
            raise FinanceError("This statement has no balances, so no opening balance can be taken from it.")
        if has_postings(session, acc):
            raise FinanceError(f"{acc.path} already has transactions; an opening balance from the statement would count them twice.")
        opening = (statement.start, statement.opening_balance)
    rows = [
        Row(
            line=i,
            date=line.date,
            description=line.description,
            amount=line.amount,
            payee=line.payee,
            reference=line.reference,
        )
        for i, line in enumerate(statement.lines, start=1)
    ]
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
    if statement.closing_balance is not None and not result.errors:
        commodity = acc.commodity_code or "TRY"
        ledger = account_balance(session, acc, statement.end).get(commodity, Decimal(0))
        if not result.written:
            ledger += sum((r.amount for r in result.rows if not r.duplicate and r.date <= statement.end), Decimal(0))
            if opening is not None and opening[0] <= statement.end:
                ledger += opening[1]
        result.balance_check = BalanceCheck(statement.end, statement.closing_balance, ledger, not result.written)
    return result
