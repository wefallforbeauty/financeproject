"""Balance sheet, net worth series, chart and the demo household."""

from datetime import date, timedelta
from decimal import Decimal

import pytest

from financeproject.core.prices import PriceBook
from financeproject.demo import build_demo, synthetic_prices
from financeproject.ledger.accounts import create_account
from financeproject.ledger.queries import commodity_totals
from financeproject.ledger.transactions import (
    opening_balance,
    record_exchange,
    record_expense,
)
from financeproject.prices.store import upsert_price
from financeproject.prices.update import load_bundled_cpi
from financeproject.reports.balance_sheet import build_balance_sheet
from financeproject.reports.chart import save_networth_chart
from financeproject.reports.context import load_market_data
from financeproject.reports.networth import networth_series


@pytest.fixture
def small(session):
    s = session
    load_bundled_cpi(s)
    for code, quote, day, value in (
        ("USD", "TRY", date(2026, 8, 31), "48"),
        ("USD", "TRY", date(2026, 9, 30), "49"),
        ("XAU_G", "USD", date(2026, 8, 31), "130"),
        ("XAU_G", "USD", date(2026, 9, 30), "135"),
    ):
        upsert_price(s, code, quote, day, Decimal(value), "test")
    create_account(s, "Assets:Bank:Checking", kind="checking", owner="Me")
    create_account(s, "Assets:FX:USD", kind="fx", commodity="USD", owner="Me")
    create_account(s, "Assets:Gold:Grams", kind="gold", owner="Dad")
    create_account(s, "Liabilities:Credit Card:Bonus", kind="credit_card", owner="Me")
    create_account(s, "Expenses:Groceries")
    opening_balance(s, "Checking", Decimal("100000"), date(2026, 8, 31))
    opening_balance(s, "Grams", Decimal("10"), date(2026, 8, 31))
    record_exchange(s, "Checking", Decimal("48000"), "Assets:FX:USD", Decimal("1000"), date(2026, 8, 31))
    record_expense(s, Decimal("2000"), "Groceries", "Bonus", date(2026, 9, 15), "Groceries")
    return s


def test_balance_sheet(small):
    bs = build_balance_sheet(small, date(2026, 9, 30))
    nw = bs.net_worth
    # 52,000 TRY + 1,000 USD × 49 + 10 g × 135 × 49 − 2,000 TRY card debt
    expected = Decimal(52000) + 49000 + Decimal(10 * 135 * 49) - 2000
    assert nw.net.try_ == expected
    assert nw.net.usd == expected / 49
    assert nw.net.gold_g == expected / (135 * 49)
    assert nw.converter.cpi_base == date(2026, 9, 1)
    assert nw.net.real_try == expected  # September 30 is after the latest CPI month's middle
    assert bs.by_owner == {"Me": Decimal(52000 + 49000 - 2000), "Dad": Decimal(10 * 135 * 49)}
    assert bs.liquidity.short_term_debt == 2000
    assert [v.position.account for v in bs.liabilities] == ["Liabilities:Credit Card:Bonus"]
    only_dad = build_balance_sheet(small, date(2026, 9, 30), owner="Dad")
    assert only_dad.net_worth.net.try_ == Decimal(10 * 135 * 49)


def test_real_try_deflates_older_dates(small):
    bs = build_balance_sheet(small, date(2026, 8, 31))
    market = bs.market
    factor = market.deflator.factor(date(2026, 8, 31))
    assert 1.005 < factor < 1.0184  # between August and September prices (+1.84 % in September)
    assert float(bs.net_worth.net.real_try) == pytest.approx(float(bs.net_worth.net.try_) * factor)


def test_networth_series_matches_balance_sheet(small):
    frame = networth_series(small, date(2026, 8, 31), date(2026, 9, 30), "month")
    assert list(frame["date"]) == [date(2026, 8, 31), date(2026, 9, 30)]
    last = build_balance_sheet(small, date(2026, 9, 30)).net_worth.net
    row = frame.iloc[-1]
    assert row["nominal_try"] == pytest.approx(float(last.try_))
    assert row["usd"] == pytest.approx(float(last.usd))
    assert row["gold_g"] == pytest.approx(float(last.gold_g))
    assert (frame["missing_prices"] == 0).all()


def test_networth_series_flags_missing_prices(session):
    create_account(session, "Assets:FX:USD", kind="fx", commodity="USD")
    opening_balance(session, "USD", Decimal("10"), date(2026, 1, 1))
    frame = networth_series(session, date(2026, 1, 1), date(2026, 2, 1))
    assert (frame["missing_prices"] == 1).all()
    assert frame["usd"].isna().all()


def test_empty_ledger_gives_empty_series(session):
    assert networth_series(session).empty


def test_chart_is_written(small, tmp_path):
    frame = networth_series(small, date(2026, 8, 1), date(2026, 9, 30), "week")
    path = save_networth_chart(frame, tmp_path / "charts" / "net.png", date(2026, 9, 1), "test")
    assert path.exists() and path.stat().st_size > 10_000


def test_demo_household_is_consistent(session):
    start, end = date(2025, 1, 1), date(2025, 12, 31)
    load_bundled_cpi(session)
    for q in synthetic_prices(start - timedelta(days=7), end):
        upsert_price(session, q.commodity, q.quote, q.on, q.value, q.source)
    market = load_market_data(session)
    summary = build_demo(session, start, end, market.book, market.deflator)
    assert summary.transactions > 200 and summary.skipped_exchanges == 0
    assert all(v == 0 for v in commodity_totals(session).values())
    bs = build_balance_sheet(session, end)
    assert bs.net_worth.complete and not bs.net_worth.stale
    assert bs.net_worth.net.try_ > 0
    assert {"Me", "Dad"} <= set(bs.by_owner)


def test_demo_without_prices_skips_exchanges(session):
    summary = build_demo(session, date(2025, 1, 1), date(2025, 3, 31), PriceBook())
    assert summary.skipped_exchanges == 3
