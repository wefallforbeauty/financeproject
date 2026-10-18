"""Recording transactions under the double-entry rules.

Rules checked by ``add_transaction``:

1. At least two legs; no zero amounts; no more decimals than the unit allows.
2. Asset and liability accounts hold one commodity; their legs must use it.
3. In every commodity the legs add up to exactly zero. One leg may leave its
   amount empty; it then takes the balancing amount.
4. Accounts cannot be used before they are opened or after they are closed.

Exchanges between commodities (buying dollars or gold with lira) go through
per-commodity trading accounts under ``Equity:Trading`` (Selinger's method), so
rule 3 holds in each commodity separately. See docs/METHOD.md.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import ROUND_FLOOR, Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from financeproject.core.kinds import ASSET, EXPENSE, INCOME, LIABILITY
from financeproject.core.money import check_decimals
from financeproject.errors import (
    DuplicateTransaction,
    LedgerError,
    UnbalancedTransaction,
)
from financeproject.ledger.accounts import (
    find_account,
    get_or_create_account,
    trading_account,
)
from financeproject.ledger.db import OPENING_ACCOUNT
from financeproject.ledger.history import (
    DELETE,
    EDIT,
    RECATEGORIZE,
    log_change,
    snapshot,
)
from financeproject.ledger.models import Account, Commodity, Posting, Transaction

MANUAL_SOURCE = "manual"
OPENING_SOURCE = "opening"
SPLIT_SOURCE = "split"


@dataclass(frozen=True, slots=True)
class Leg:
    account: str | Account
    amount: Decimal | None
    """Signed amount; ``None`` lets this leg balance the transaction."""
    commodity: str | None = None
    memo: str | None = None


def _commodity_for(session: Session, account: Account, requested: str | None) -> Commodity:
    if account.commodity_code:
        if requested and requested.upper() != account.commodity_code:
            raise LedgerError(f"{account.path} holds only {account.commodity_code}; {requested.upper()} is not allowed.")
        code = account.commodity_code
    else:
        code = (requested or "TRY").upper()
    commodity = session.get(Commodity, code)
    if commodity is None:
        raise LedgerError(f"Unknown commodity: {code}")
    return commodity


def add_transaction(
    session: Session,
    on: date,
    description: str,
    legs: list[Leg],
    *,
    payee: str | None = None,
    note: str | None = None,
    source: str = MANUAL_SOURCE,
    import_key: str | None = None,
) -> Transaction:
    if not description or not description.strip():
        raise LedgerError("The description cannot be empty.")
    if len(legs) < 2:
        raise LedgerError("A transaction needs at least two legs.")

    resolved: list[list] = []
    for leg in legs:
        account = find_account(session, leg.account)
        if account.opened_on and on < account.opened_on:
            raise LedgerError(f"{account.path} was opened on {account.opened_on}; a transaction on {on} is not allowed.")
        if account.closed_on and on > account.closed_on:
            raise LedgerError(f"{account.path} was closed on {account.closed_on}; a transaction on {on} is not allowed.")
        commodity = _commodity_for(session, account, leg.commodity)
        if leg.amount is not None:
            if leg.amount == 0:
                raise LedgerError(f"The amount for {account.path} cannot be zero.")
            check_decimals(leg.amount, commodity.decimals, commodity.code)
        resolved.append([account, commodity.code, leg.amount, leg.memo])

    sums: dict[str, Decimal] = defaultdict(Decimal)
    for _, code, amount, _ in resolved:
        if amount is not None:
            sums[code] += amount
    open_legs = [r for r in resolved if r[2] is None]
    if len(open_legs) > 1:
        raise LedgerError("At most one leg can leave its amount empty.")
    if open_legs:
        leg = open_legs[0]
        unbalanced = {c for c, s in sums.items() if s != 0}
        if unbalanced != {leg[1]}:
            raise UnbalancedTransaction(
                f"The leg without an amount ({leg[0].path}, {leg[1]}) cannot balance the transaction; "
                f"unbalanced commodities: {', '.join(sorted(unbalanced)) or 'none'}."
            )
        leg[2] = -sums[leg[1]]
        sums[leg[1]] = Decimal(0)

    unbalanced = {c: s for c, s in sums.items() if s != 0}
    if unbalanced:
        detail = ", ".join(f"{c} {s:+f}" for c, s in sorted(unbalanced.items()))
        raise UnbalancedTransaction(f"The transaction does not balance (the legs must add up to zero): {detail}")

    if import_key and session.scalar(select(Transaction.id).where(Transaction.import_key == import_key)):
        raise DuplicateTransaction("This row was imported before.")

    tx = Transaction(
        date=on,
        description=description.strip(),
        payee=payee,
        note=note,
        source=source,
        import_key=import_key,
    )
    tx.postings = [
        Posting(account=account, commodity_code=code, amount=amount, memo=memo)
        for account, code, amount, memo in resolved
    ]
    session.add(tx)
    session.flush()
    return tx


def _balance_sheet_account(session: Session, query: str | Account, role: str) -> Account:
    account = find_account(session, query)
    if account.type not in (ASSET, LIABILITY):
        raise LedgerError(f"{role} must be an asset or liability account: {account.path}")
    return account


def _positive(amount: Decimal, what: str = "The amount") -> None:
    if amount <= 0:
        raise LedgerError(f"{what} must be positive.")


def signed_balance(account: Account, balance: Decimal) -> Decimal:
    """Natural balance → ledger sign: a debt of 5,000 is −5,000 on a liability."""
    return -balance if account.type == LIABILITY else balance


def opening_balance(
    session: Session, account: str | Account, balance: Decimal, on: date, description: str | None = None
) -> Transaction:
    """Starting balance of an account against ``Equity:Opening Balances``.

    ``balance`` has the natural sign: what you hold for an asset, what you owe
    for a liability.
    """
    acc = _balance_sheet_account(session, account, "The account of an opening balance")
    opening = get_or_create_account(session, OPENING_ACCOUNT)
    return add_transaction(
        session,
        on,
        description or f"Opening balance: {acc.path}",
        [Leg(acc, signed_balance(acc, balance)), Leg(opening, None, commodity=acc.commodity_code)],
        source=OPENING_SOURCE,
    )


def record_expense(
    session: Session,
    amount: Decimal,
    expense_account: str | Account,
    paid_from: str | Account,
    on: date,
    description: str,
    payee: str | None = None,
    *,
    source: str = MANUAL_SOURCE,
    import_key: str | None = None,
) -> Transaction:
    _positive(amount)
    expense = find_account(session, expense_account)
    if expense.type != EXPENSE:
        raise LedgerError(f"The expense account must start with 'Expenses:': {expense.path}")
    paying = _balance_sheet_account(session, paid_from, "The account paid from")
    return add_transaction(
        session,
        on,
        description,
        [Leg(expense, amount, commodity=paying.commodity_code), Leg(paying, -amount)],
        payee=payee,
        source=source,
        import_key=import_key,
    )


def record_income(
    session: Session,
    amount: Decimal,
    income_account: str | Account,
    paid_to: str | Account,
    on: date,
    description: str,
    payee: str | None = None,
) -> Transaction:
    _positive(amount)
    income = find_account(session, income_account)
    if income.type != INCOME:
        raise LedgerError(f"The income account must start with 'Income:': {income.path}")
    target = _balance_sheet_account(session, paid_to, "The account paid into")
    return add_transaction(
        session,
        on,
        description,
        [Leg(target, amount), Leg(income, -amount, commodity=target.commodity_code)],
        payee=payee,
    )


def record_transfer(
    session: Session,
    amount: Decimal,
    from_account: str | Account,
    to_account: str | Account,
    on: date,
    description: str | None = None,
) -> Transaction:
    """Move money between two accounts of the same unit, e.g. pay a card from the bank."""
    _positive(amount)
    src = _balance_sheet_account(session, from_account, "The source account")
    dst = _balance_sheet_account(session, to_account, "The target account")
    if src.id == dst.id:
        raise LedgerError("The source and target are the same account.")
    if src.commodity_code != dst.commodity_code:
        raise LedgerError(
            f"The units differ ({src.commodity_code} → {dst.commodity_code}); use 'exchange' to buy or sell currency or gold."
        )
    return add_transaction(
        session,
        on,
        description or f"Transfer: {src.path} → {dst.path}",
        [Leg(dst, amount), Leg(src, -amount)],
    )


def record_exchange(
    session: Session,
    from_account: str | Account,
    from_amount: Decimal,
    to_account: str | Account,
    to_amount: Decimal,
    on: date,
    description: str | None = None,
) -> Transaction:
    """Pay ``from_amount`` from one account and receive ``to_amount`` of another unit.

    Four legs, two per commodity, through the trading accounts.
    """
    _positive(from_amount, "The amount paid")
    _positive(to_amount, "The amount received")
    src = _balance_sheet_account(session, from_account, "The source account")
    dst = _balance_sheet_account(session, to_account, "The target account")
    if src.commodity_code == dst.commodity_code:
        raise LedgerError("The units are the same; use 'transfer' instead.")
    rate = from_amount / to_amount
    note = f"Rate: 1 {dst.commodity_code} = {rate:.6f} {src.commodity_code}"
    return add_transaction(
        session,
        on,
        description or f"Buy {dst.commodity_code} with {src.commodity_code}",
        [
            Leg(src, -from_amount),
            Leg(trading_account(session, src.commodity_code), from_amount),
            Leg(trading_account(session, dst.commodity_code), -to_amount),
            Leg(dst, to_amount),
        ],
        note=note,
    )


def record_split(session: Session, account: str | Account, ratio: Decimal, on: date) -> Transaction:
    """A split or bonus issue on ``on``: the shares held the day before are multiplied by ``ratio``.

    ``ratio`` is new shares per old share: 2 for a 100 % bonus issue, 1.25 for
    25 %, 0.1 for a 1-for-10 reverse split. Fractions of a share are dropped.
    The shares come from (or go to) the trading account of their commodity,
    so no money enters or leaves: the price falls in the same proportion.
    """
    from financeproject.ledger.queries import account_balance

    acc = _balance_sheet_account(session, account, "A split")
    commodity = session.get(Commodity, acc.commodity_code) if acc.commodity_code else None
    if acc.type != ASSET or commodity is None or commodity.kind == "currency":
        raise LedgerError(f"Splits are for accounts that hold shares or fund units: {acc.path}")
    if ratio <= 0 or ratio == 1:
        raise LedgerError("The ratio must be positive and not 1 (new shares per old share, e.g. 2 or 2:1).")
    held = account_balance(session, acc, on - timedelta(days=1)).get(commodity.code, Decimal(0))
    if held <= 0:
        raise LedgerError(f"{acc.path} holds no {commodity.code} before {on}.")
    after = (held * ratio).quantize(Decimal(1).scaleb(-commodity.decimals), rounding=ROUND_FLOOR)
    if after == held:
        raise LedgerError(f"The split changes nothing at {commodity.decimals} decimals.")
    return add_transaction(
        session,
        on,
        f"Split of {commodity.code}: {ratio.normalize():f} for 1",
        [Leg(acc, after - held), Leg(trading_account(session, commodity.code), held - after)],
        note=f"{held.normalize():f} → {after.normalize():f}",
        source=SPLIT_SOURCE,
    )


def _get(session: Session, transaction_id: int) -> Transaction:
    tx = session.get(Transaction, transaction_id)
    if tx is None:
        raise LedgerError(f"No transaction #{transaction_id}")
    return tx


def _label(tx: Transaction) -> str:
    return f"#{tx.id} {tx.date} {tx.description}"


def delete_transaction(session: Session, transaction_id: int) -> Transaction:
    """Delete a transaction; the change history keeps a copy of it."""
    tx = _get(session, transaction_id)
    log_change(session, DELETE, f"Deleted {_label(tx)}", transaction_id=tx.id, before=snapshot(tx))
    session.delete(tx)
    session.flush()
    return tx


def move_posting(session: Session, posting: Posting, account: Account, action: str = RECATEGORIZE) -> None:
    """Move one leg to another account, recording the change."""
    if posting.account_id == account.id:
        return
    tx = posting.transaction
    before = snapshot(tx)
    old = posting.account.path
    posting.account = account
    session.flush()
    log_change(
        session, action, f"{_label(tx)}: {old} → {account.path}", transaction_id=tx.id, before=before, after=snapshot(tx)
    )


def recategorize(session: Session, transaction_id: int, account: str | Account) -> Transaction:
    """Move the income or expense leg of a simple transaction to another category.

    A simple transaction has one balance-sheet leg and one income or expense
    leg; that is what imports and the expense and income commands create.
    """
    tx = _get(session, transaction_id)
    target = find_account(session, account)
    if target.type not in (INCOME, EXPENSE):
        raise LedgerError(f"The new category must be an income or expense account: {target.path}")
    categories = [p for p in tx.postings if p.account.type in (INCOME, EXPENSE)]
    if len(tx.postings) != 2 or len(categories) != 1:
        raise LedgerError(
            f"Transaction #{tx.id} is not a simple income or expense; change it with 'txn delete' and 'txn add'."
        )
    leg = categories[0]
    if target.commodity_code and target.commodity_code != leg.commodity_code:
        raise LedgerError(f"{target.path} holds only {target.commodity_code}; this transaction is in {leg.commodity_code}.")
    move_posting(session, leg, target)
    return tx


SIMPLE, TRANSFER, OTHER = "simple", "transfer", "other"
"""The shapes of a transaction for editing: one account and one category, two accounts, or more legs."""


def shape(tx: Transaction) -> str:
    if len(tx.postings) == 2:
        a, b = tx.postings
        sides = [p.account.type in (ASSET, LIABILITY) for p in tx.postings]
        if sides.count(True) == 1 and all(p.account.type in (ASSET, LIABILITY, INCOME, EXPENSE) for p in tx.postings):
            return SIMPLE
        if all(sides) and a.commodity_code == b.commodity_code:
            return TRANSFER
    return OTHER


def _sides(tx: Transaction) -> tuple[Posting, Posting]:
    """The balance-sheet leg and the category leg of a simple transaction."""
    a, b = tx.postings
    return (a, b) if a.account.type in (ASSET, LIABILITY) else (b, a)


def _check_postings(session: Session, tx: Transaction) -> None:
    sums: dict[str, Decimal] = defaultdict(Decimal)
    for p in tx.postings:
        account = p.account
        if account.opened_on and tx.date < account.opened_on:
            raise LedgerError(f"{account.path} was opened on {account.opened_on}; a transaction on {tx.date} is not allowed.")
        if account.closed_on and tx.date > account.closed_on:
            raise LedgerError(f"{account.path} was closed on {account.closed_on}; a transaction on {tx.date} is not allowed.")
        commodity = session.get(Commodity, p.commodity_code)
        check_decimals(p.amount, commodity.decimals, commodity.code)
        sums[p.commodity_code] += p.amount
    if any(v != 0 for v in sums.values()):
        raise UnbalancedTransaction("The transaction does not balance after the change.")


_KEEP = object()


def _describe_change(before: dict, after: dict) -> str:
    parts = []
    for field in ("date", "description", "payee", "note"):
        if before[field] != after[field]:
            parts.append(f"{field} {before[field] or '—'} → {after[field] or '—'}")
    legs_before = {(leg["account"], leg["amount"]) for leg in before["legs"]}
    legs_after = {(leg["account"], leg["amount"]) for leg in after["legs"]}
    if legs_before != legs_after:
        parts.append("legs " + ", ".join(f"{a} {x}" for a, x in sorted(legs_after)))
    return "; ".join(parts)


def update_transaction(
    session: Session,
    transaction_id: int,
    *,
    on=_KEEP,
    description=_KEEP,
    payee=_KEEP,
    note=_KEEP,
    amount=_KEEP,
    category=_KEEP,
    account=_KEEP,
) -> Transaction:
    """Change a transaction and record the change.

    The date, description, payee and note of any transaction can change. The
    amount (natural, positive), the category and the account of a simple
    transaction can change too, and the amount of a transfer between two
    accounts. Transactions with more legs (exchanges, loan payments) keep their
    legs: delete them and enter them again. An installment purchase stays on
    its card, and a new amount changes its plan's total.
    """
    tx = _get(session, transaction_id)
    before = snapshot(tx)
    saved = (
        tx.date, tx.description, tx.payee, tx.note,
        [(p, p.account, p.amount) for p in tx.postings],
        tx.installment_plan.total if tx.installment_plan is not None else None,
    )
    try:
        _apply_update(session, tx, on, description, payee, note, amount, category, account)
    except Exception:
        tx.date, tx.description, tx.payee, tx.note, legs, total = saved
        for posting, account_, amount_ in legs:
            posting.account, posting.amount = account_, amount_
        if total is not None:
            tx.installment_plan.total = total
        session.flush()
        raise
    after = snapshot(tx)
    if after != before:
        log_change(session, EDIT, f"{_label(tx)}: {_describe_change(before, after)}", transaction_id=tx.id, before=before, after=after)
    return tx


def _apply_update(session: Session, tx: Transaction, on, description, payee, note, amount, category, account) -> None:
    kind = shape(tx)
    if on is not _KEEP:
        tx.date = on
    if description is not _KEEP:
        if not description or not description.strip():
            raise LedgerError("The description cannot be empty.")
        tx.description = description.strip()
    if payee is not _KEEP:
        tx.payee = (payee or "").strip() or None
    if note is not _KEEP:
        tx.note = (note or "").strip() or None
    legs_changed = any(x is not _KEEP for x in (amount, category, account))
    if legs_changed and kind == OTHER:
        raise LedgerError(
            f"Only the date, description, payee and note of #{tx.id} can change: it has more than two legs. "
            "Delete it and enter it again."
        )
    if kind == SIMPLE:
        own, cat = _sides(tx)
        if account is not _KEEP:
            target = _balance_sheet_account(session, account, "The account")
            if tx.installment_plan is not None and target.id != own.account_id:
                raise LedgerError("An installment purchase stays on its card.")
            if (target.commodity_code or own.commodity_code) != own.commodity_code:
                raise LedgerError(f"{target.path} holds {target.commodity_code}; this transaction is in {own.commodity_code}.")
            own.account = target
        if category is not _KEEP:
            target = find_account(session, category)
            if target.type not in (INCOME, EXPENSE):
                raise LedgerError(f"The category must be an income or expense account: {target.path}")
            if target.commodity_code and target.commodity_code != cat.commodity_code:
                raise LedgerError(f"{target.path} holds only {target.commodity_code}; this transaction is in {cat.commodity_code}.")
            cat.account = target
        if amount is not _KEEP:
            _positive(amount)
            own.amount = amount if own.amount > 0 else -amount
            cat.amount = -own.amount
            if tx.installment_plan is not None:
                tx.installment_plan.total = amount
    elif kind == TRANSFER:
        if category is not _KEEP or account is not _KEEP:
            raise LedgerError("A transfer has no category, and its accounts stay; delete it and enter it again to change them.")
        if amount is not _KEEP:
            _positive(amount)
            a, b = tx.postings
            a.amount = amount if a.amount > 0 else -amount
            b.amount = -a.amount
    session.flush()
    _check_postings(session, tx)
