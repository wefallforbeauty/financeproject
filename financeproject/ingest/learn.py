"""Categorization that learns from the ledger: multinomial naive Bayes.

Every transaction that moved money between one balance-sheet account and one
income or expense account is an example. Its description, payee, account and
size are the input; the income or expense account is the label. Correcting a
category (``finance txn recategorize``) changes the examples, so the next
prediction learns from the correction.

The model is the multinomial naive Bayes text classifier with Laplace
smoothing, counting each word once per transaction ("boolean" multinomial NB,
Manning et al. 2008, ch. 13; McCallum & Nigam 1998). Features:

- the words of the description and the payee, folded (case, accents and
  Turkish letters removed), at least two characters and at least one letter
  (``A101`` stays; receipt and card numbers go);
- the whole payee as one feature, so a known merchant is recognized at once;
- the balance-sheet account the money moved through;
- the size of the amount in half-decade bins (… 100–316, 316–1,000 …).

Money out is only matched with categories that money out went to before, and
money in likewise. A guess is used only when its posterior probability reaches
a threshold. Naive Bayes probabilities are known to be poorly calibrated
(Zadrozny & Elkan 2001), so the threshold is checked empirically:
``evaluate`` goes through the ledger in date order, predicts each transaction
from the earlier ones and then learns it (prequential evaluation, Dawid 1984),
and reports how often the confident guesses were right.

For comparison, smart_importer (Beancount) uses a linear support vector
machine; naive Bayes needs no extra dependency, learns one transaction at a
time and works with few examples.
"""

from __future__ import annotations

import math
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from financeproject.core.kinds import BALANCE_SHEET_TYPES, EXPENSE, INCOME
from financeproject.core.text import fold
from financeproject.ledger.db import UNCATEGORIZED_EXPENSE, UNCATEGORIZED_INCOME
from financeproject.ledger.models import Account, Posting, Transaction

DEFAULT_MIN_CONFIDENCE = 0.8
MIN_EXAMPLES = 10
_WORD = re.compile(r"\b(?=[a-z0-9]*[a-z])[a-z0-9]{2,}\b")
UNCATEGORIZED = (UNCATEGORIZED_EXPENSE, UNCATEGORIZED_INCOME)


def words(text: str | None) -> list[str]:
    return _WORD.findall(fold(text or ""))


def size_bucket(amount: Decimal) -> str:
    magnitude = abs(float(amount))
    if magnitude < 1:
        return "size:<1"
    return f"size:{round(math.log10(magnitude) * 2) / 2:g}"


def features(description: str, payee: str | None, account_id: int, amount: Decimal) -> frozenset[str]:
    out = {f"w:{w}" for w in words(description)}
    payee_words = words(payee)
    out |= {f"w:{w}" for w in payee_words}
    if payee_words:
        out.add("payee:" + " ".join(payee_words))
    out.add(f"account:{account_id}")
    out.add(size_bucket(amount))
    return frozenset(out)


def _is_text_feature(feature: str) -> bool:
    return feature.startswith(("w:", "payee:"))


@dataclass(frozen=True, slots=True)
class Prediction:
    label: int
    confidence: float


class NaiveBayes:
    """Boolean multinomial naive Bayes that can learn one example at a time."""

    def __init__(self, alpha: float = 1.0):
        self.alpha = alpha
        self.docs: Counter[int] = Counter()
        self.counts: dict[int, Counter[str]] = defaultdict(Counter)
        self.totals: Counter[int] = Counter()
        self.vocabulary: set[str] = set()
        self.labels_by_direction: dict[int, set[int]] = defaultdict(set)

    def add(self, feats: frozenset[str], label: int, direction: int) -> None:
        self.docs[label] += 1
        self.counts[label].update(feats)
        self.totals[label] += len(feats)
        self.vocabulary |= feats
        self.labels_by_direction[direction].add(label)

    def __len__(self) -> int:
        return sum(self.docs.values())

    def predict(self, feats: frozenset[str], direction: int) -> Prediction | None:
        known = feats & self.vocabulary
        if not any(_is_text_feature(f) for f in known):
            return None
        candidates = self.labels_by_direction.get(direction)
        if not candidates:
            return None
        n = sum(self.docs[c] for c in candidates)
        v = len(self.vocabulary)
        scores: dict[int, float] = {}
        for c in candidates:
            denominator = self.totals[c] + self.alpha * v
            counts = self.counts[c]
            score = math.log(self.docs[c] / n)
            for f in known:
                score += math.log((counts[f] + self.alpha) / denominator)
            scores[c] = score
        best = max(scores, key=lambda c: (scores[c], -c))
        top = scores[best]
        z = sum(math.exp(s - top) for s in scores.values())
        return Prediction(best, 1.0 / z)


@dataclass(frozen=True, slots=True)
class Example:
    transaction_id: int
    date: date
    features: frozenset[str]
    label: int
    direction: int


def _two_sided(tx: Transaction) -> tuple[Posting, Posting] | None:
    """The balance-sheet and the income/expense posting of a simple transaction."""
    if len(tx.postings) != 2:
        return None
    a, b = tx.postings
    if a.account.type in BALANCE_SHEET_TYPES and b.account.type in (INCOME, EXPENSE):
        return a, b
    if b.account.type in BALANCE_SHEET_TYPES and a.account.type in (INCOME, EXPENSE):
        return b, a
    return None


def simple_transactions(session: Session) -> list[tuple[Transaction, Posting, Posting]]:
    txs = session.scalars(
        select(Transaction)
        .options(selectinload(Transaction.postings).selectinload(Posting.account))
        .order_by(Transaction.date, Transaction.id)
    )
    out = []
    for tx in txs:
        pair = _two_sided(tx)
        if pair is not None:
            out.append((tx, *pair))
    return out


def examples(session: Session) -> list[Example]:
    """Categorized simple transactions, oldest first."""
    out: list[Example] = []
    for tx, bs, category in simple_transactions(session):
        if category.account.path in UNCATEGORIZED:
            continue
        out.append(
            Example(
                tx.id,
                tx.date,
                features(tx.description, tx.payee, bs.account_id, bs.amount),
                category.account_id,
                1 if bs.amount > 0 else -1,
            )
        )
    return out


@dataclass(frozen=True, slots=True)
class Guess:
    account: Account
    confidence: float


class CategoryModel:
    def __init__(self, session: Session, training: list[Example]):
        self.nb = NaiveBayes()
        for ex in training:
            self.nb.add(ex.features, ex.label, ex.direction)
        ids = set(self.nb.docs)
        self.accounts = {a.id: a for a in session.scalars(select(Account).where(Account.id.in_(ids)))}
        self.examples = len(training)

    def guess(self, description: str, payee: str | None, account_id: int, amount: Decimal) -> Guess | None:
        p = self.nb.predict(features(description, payee, account_id, amount), 1 if amount > 0 else -1)
        if p is None:
            return None
        return Guess(self.accounts[p.label], p.confidence)


def train(session: Session, min_examples: int = MIN_EXAMPLES) -> CategoryModel | None:
    """A model of the ledger's categorized transactions; ``None`` with too few of them."""
    training = examples(session)
    if len(training) < min_examples:
        return None
    return CategoryModel(session, training)


@dataclass(frozen=True)
class Evaluation:
    examples: int
    predicted: int
    correct: int
    confident: int
    confident_correct: int
    threshold: float

    @property
    def accuracy(self) -> float | None:
        """Share of right guesses among all guesses."""
        return self.correct / self.predicted if self.predicted else None

    @property
    def coverage(self) -> float | None:
        """Share of the evaluated transactions whose guess reached the threshold."""
        return self.confident / self.examples if self.examples else None

    @property
    def precision(self) -> float | None:
        """Share of right guesses among the confident ones (the ones that would be applied)."""
        return self.confident_correct / self.confident if self.confident else None


def evaluate(items: list[Example], threshold: float = DEFAULT_MIN_CONFIDENCE, warmup: int = MIN_EXAMPLES) -> Evaluation:
    """Prequential evaluation: predict each example from the earlier ones, then learn it."""
    nb = NaiveBayes()
    evaluated = predicted = correct = confident = confident_correct = 0
    for i, ex in enumerate(items):
        if i >= warmup:
            evaluated += 1
            p = nb.predict(ex.features, ex.direction)
            if p is not None:
                predicted += 1
                hit = p.label == ex.label
                correct += hit
                if p.confidence >= threshold:
                    confident += 1
                    confident_correct += hit
        nb.add(ex.features, ex.label, ex.direction)
    return Evaluation(evaluated, predicted, correct, confident, confident_correct, threshold)


@dataclass(frozen=True)
class Suggestion:
    transaction: Transaction
    posting: Posting
    """The uncategorized posting that would move."""
    account: Account
    confidence: float


def suggest(session: Session, model: CategoryModel, min_confidence: float = DEFAULT_MIN_CONFIDENCE) -> list[Suggestion]:
    """Guesses for the uncategorized simple transactions that reach ``min_confidence``."""
    out: list[Suggestion] = []
    for tx, bs, category in simple_transactions(session):
        if category.account.path not in UNCATEGORIZED:
            continue
        guess = model.guess(tx.description, tx.payee, bs.account_id, bs.amount)
        if guess is None or guess.confidence < min_confidence:
            continue
        if guess.account.commodity_code and guess.account.commodity_code != category.commodity_code:
            continue
        out.append(Suggestion(tx, category, guess.account, guess.confidence))
    return out


def apply(session: Session, suggestions: list[Suggestion]) -> None:
    """Move every suggested posting to its category, recording each change."""
    from financeproject.ledger.transactions import move_posting

    for s in suggestions:
        move_posting(session, s.posting, s.account)
