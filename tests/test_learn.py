"""The learned categorizer: features, naive Bayes, evaluation, use in imports."""

from datetime import date, timedelta
from decimal import Decimal

import pytest

from financeproject.errors import LedgerError
from financeproject.ingest import learn
from financeproject.ingest.csvfile import import_csv
from financeproject.ledger.accounts import create_account
from financeproject.ledger.transactions import (
    recategorize,
    record_expense,
    record_income,
)


def test_words_keep_names_and_drop_numbers():
    assert learn.words("POS ALIŞVERİŞ KART NO: 4000 **** **** 0000 İŞYERİ: SOK-00001 MUTABAKAT: 1234567") == [
        "pos", "alisveris", "kart", "no", "isyeri", "sok", "mutabakat",
    ]
    assert learn.words("A101 MARKET") == ["a101", "market"]
    assert learn.words(None) == []


def test_features():
    f = learn.features("POS MIGROS", "Migros Konya", 7, Decimal("-450"))
    assert {"w:pos", "w:migros", "w:konya", "payee:migros konya", "account:7"} <= f
    assert learn.size_bucket(Decimal("-450")) == "size:2.5"  # 316–1,000
    assert learn.size_bucket(Decimal("25000")) == "size:4.5"
    assert learn.size_bucket(Decimal("0.5")) == "size:<1"


def test_naive_bayes_learns_one_example_at_a_time():
    nb = learn.NaiveBayes()
    groceries, fuel, salary = 1, 2, 3
    for _ in range(3):
        nb.add(frozenset({"w:migros", "w:pos"}), groceries, -1)
        nb.add(frozenset({"w:opet", "w:pos"}), fuel, -1)
    nb.add(frozenset({"w:salary"}), salary, 1)
    guess = nb.predict(frozenset({"w:migros", "w:pos"}), -1)
    # vocabulary of 4 features, 6 feature counts per class, Laplace smoothing:
    # groceries 0.4 · 0.4 against fuel 0.1 · 0.4, so 0.16 / (0.16 + 0.04) = 0.8
    assert guess.label == groceries and guess.confidence == pytest.approx(0.8)
    assert nb.predict(frozenset({"w:opet"}), -1).label == fuel
    # money in is only matched with categories money came in through
    assert nb.predict(frozenset({"w:migros"}), 1).label == salary
    # nothing known about the text: no guess
    assert nb.predict(frozenset({"w:unknown", "account:1"}), -1) is None
    assert len(nb) == 7


def test_prequential_evaluation_counts():
    items = [
        learn.Example(i, date(2026, 1, 1) + timedelta(days=i), frozenset({f"w:shop{i % 2}"}), i % 2, -1) for i in range(30)
    ]
    ev = learn.evaluate(items, threshold=0.8, warmup=10)
    assert ev.examples == 20 and ev.predicted == 20 and ev.correct == 20
    assert ev.accuracy == 1.0 and ev.precision == 1.0 and ev.coverage == 1.0
    empty = learn.evaluate([], warmup=10)
    assert empty.accuracy is None and empty.coverage is None and empty.precision is None


@pytest.fixture
def history(session):
    create_account(session, "Liabilities:Credit Card:Bonus", kind="credit_card")
    create_account(session, "Assets:Bank:Checking", kind="checking")
    for path in ("Expenses:Food:Groceries", "Expenses:Transport", "Income:Salary"):
        create_account(session, path)
    start = date(2026, 1, 1)
    for k in range(8):
        record_expense(session, Decimal(400 + 10 * k), "Groceries", "Bonus", start + timedelta(days=3 * k), "POS MIGROS", "MIGROS")
        record_expense(session, Decimal(900 + 10 * k), "Transport", "Bonus", start + timedelta(days=3 * k + 1), "POS OPET", "OPET")
    record_income(session, Decimal(50000), "Salary", "Checking", start, "Salary", "EMPLOYER")
    return session


def test_training_needs_enough_examples(session):
    assert learn.train(session) is None


def test_suggestions_for_uncategorized_transactions(history):
    s = history
    model = learn.train(s)
    assert model is not None and model.examples == 17
    tx = record_expense(s, Decimal("432.10"), "Expenses:Uncategorized", "Bonus", date(2026, 2, 1), "POS MIGROS 4512", "MIGROS")
    record_expense(s, Decimal("77"), "Expenses:Uncategorized", "Bonus", date(2026, 2, 1), "SOMETHING NEW", None)
    suggestions = learn.suggest(s, learn.train(s))
    assert [(x.transaction.id, x.account.path) for x in suggestions] == [(tx.id, "Expenses:Food:Groceries")]
    assert suggestions[0].confidence >= learn.DEFAULT_MIN_CONFIDENCE
    learn.apply(s, suggestions)
    assert {p.account.path for p in tx.postings} == {"Expenses:Food:Groceries", "Liabilities:Credit Card:Bonus"}


def test_import_uses_the_model_after_the_rules(history, tmp_path):
    path = tmp_path / "card.csv"
    path.write_text(
        "date,description,amount,payee\n"
        "2026-02-03,POS MIGROS,-415.00,MIGROS\n"
        "2026-02-04,POS OPET,-990.00,OPET\n"
        "2026-02-05,FLOWER SHOP,-120.00,\n",
        encoding="utf-8",
    )
    result = import_csv(history, path, account="Bonus")
    assert result.written
    assert [r.counter_account for r in result.rows] == [
        "Expenses:Food:Groceries", "Expenses:Transport", "Expenses:Uncategorized",
    ]
    assert result.rows[0].how.startswith("model ") and result.learned == 2
    off = import_csv(history, path, account="Bonus", dry_run=True, use_model=False)
    assert all(r.duplicate for r in off.rows)


def test_recategorize_teaches_the_model(history):
    s = history
    create_account(s, "Expenses:Gifts")
    tx = record_expense(s, Decimal("300"), "Expenses:Uncategorized", "Bonus", date(2026, 3, 1), "FLOWER SHOP", "FLOWERS")
    recategorize(s, tx.id, "Gifts")
    assert {p.account.path for p in tx.postings} == {"Expenses:Gifts", "Liabilities:Credit Card:Bonus"}
    assert any(ex.transaction_id == tx.id for ex in learn.examples(s))
    with pytest.raises(LedgerError, match="income or expense"):
        recategorize(s, tx.id, "Bonus")
    with pytest.raises(LedgerError, match="No transaction"):
        recategorize(s, 9999, "Gifts")
