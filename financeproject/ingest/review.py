"""The uncategorized transactions with a suggested category for each, for review.

A suggestion comes from the first matching rule, otherwise from the learned
model (``ingest.learn``) with its probability. Nothing changes until a
suggestion is accepted or another category is chosen; every change is
recorded in the change history.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from financeproject.core.kinds import BALANCE_SHEET_TYPES
from financeproject.errors import LedgerError
from financeproject.ingest import learn
from financeproject.ingest.rules import rule_matches
from financeproject.ledger.accounts import find_account
from financeproject.ledger.models import Account, Posting, Transaction
from financeproject.ledger.queries import UNCATEGORIZED
from financeproject.ledger.transactions import move_posting


@dataclass(frozen=True)
class ReviewItem:
    transaction: Transaction
    posting: Posting
    """The uncategorized leg."""
    account: Account | None
    """The asset or liability account the money moved through."""
    amount: Decimal
    """The change of that account (money out negative)."""
    suggestion: Account | None
    confidence: float | None
    """The model's probability; ``None`` for a rule."""
    how: str
    """``rule #n``, ``model`` or empty."""

    @property
    def confident(self) -> bool:
        """True for a rule, or a guess at least as sure as the import's threshold."""
        return self.suggestion is not None and (self.confidence is None or self.confidence >= learn.DEFAULT_MIN_CONFIDENCE)


def review(session: Session) -> list[ReviewItem]:
    """Every uncategorized leg with its suggestion, newest first."""
    rules = {posting.id: rule for _, posting, rule in rule_matches(session)}
    model = learn.train(session)
    postings = session.scalars(
        select(Posting)
        .join(Account, Posting.account_id == Account.id)
        .where(Account.path.in_(UNCATEGORIZED))
        .options(selectinload(Posting.transaction).selectinload(Transaction.postings).selectinload(Posting.account))
    )
    out = []
    for posting in postings:
        tx = posting.transaction
        own = next((p for p in tx.postings if p.account.type in BALANCE_SHEET_TYPES), None)
        suggestion, confidence, how = None, None, ""
        if posting.id in rules:
            rule = rules[posting.id]
            suggestion, how = rule.account, f"rule #{rule.rule_id}"
        elif model is not None and own is not None and len(tx.postings) == 2:
            guess = model.guess(tx.description, tx.payee, own.account_id, own.amount)
            if guess is not None and guess.account.commodity_code in (None, posting.commodity_code):
                suggestion, confidence, how = guess.account, guess.confidence, "model"
        out.append(
            ReviewItem(
                tx, posting, own.account if own else None, own.amount if own else -posting.amount,
                suggestion, confidence, how,
            )
        )
    return sorted(out, key=lambda item: (item.transaction.date, item.transaction.id), reverse=True)


def categorize(session: Session, transaction_id: int, account: str | Account) -> Transaction:
    """Put the uncategorized leg of a transaction into ``account``."""
    target = find_account(session, account)
    tx = session.get(Transaction, transaction_id)
    leg = next((p for p in tx.postings if p.account.path in UNCATEGORIZED), None) if tx else None
    if leg is None:
        raise LedgerError(f"Transaction #{transaction_id} has no uncategorized leg.")
    if target.type != leg.account.type:
        raise LedgerError(f"{target.path} is not an {leg.account.type} account like {leg.account.path}.")
    move_posting(session, leg, target)
    return tx


def accept_confident(session: Session) -> int:
    """Accept every rule's suggestion and every guess that reaches the threshold; returns how many."""
    items = [item for item in review(session) if item.confident]
    for item in items:
        move_posting(session, item.posting, item.suggestion)
    return len(items)
