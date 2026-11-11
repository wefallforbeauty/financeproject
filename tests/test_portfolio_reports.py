"""Portfolio theory on a ledger (05): the return matrix, the reports, the policy rate and the commands."""

import math
import random
from datetime import date, timedelta
from decimal import Decimal

import httpx
import numpy as np
import pytest
from typer.testing import CliRunner

from financeproject.cli import app
from financeproject.core import portfolio as pf
from financeproject.core.dates import business_days
from financeproject.errors import FinanceError, SourceError
from financeproject.ledger.accounts import create_account
from financeproject.ledger.db import make_engine, session_scope
from financeproject.ledger.models import Commodity
from financeproject.ledger.transactions import (
    opening_balance,
    record_exchange,
    record_split,
)
from financeproject.prices import tcmb_rates, update
from financeproject.prices.http import make_client
from financeproject.prices.store import (
    POLICY_RATE,
    bundled_rates,
    load_index,
    load_rates,
    upsert_price,
    upsert_split,
)
from financeproject.reports.portfolio import (
    NOW,
    ORDER,
    all_splits,
    build_sample,
    cash_benchmark,
    frontier_report,
    walk_report,
)

D = Decimal
FIRST, END = date(2025, 6, 2), date(2026, 10, 30)
DAYS = list(business_days(FIRST, END))


def fill(session) -> None:
    """A fund, a share, dollars and a money market fund that earns the policy rate, with prices on every weekday."""
    for code, kind in (("FUND.X", "fund"), ("SHARE.X", "stock"), ("MMF.X", "fund")):
        session.add(Commodity(code=code, name=code, kind=kind, decimals=4, price_source="manual"))
    session.flush()
    create_account(session, "Assets:Bank:Checking", kind="checking", institution="Bank A")
    create_account(session, "Assets:Investments:Fund", kind="fund", commodity="FUND.X")
    create_account(session, "Assets:Investments:Share", kind="stock", commodity="SHARE.X")
    create_account(session, "Assets:Investments:Money Market", kind="fund", commodity="MMF.X")
    create_account(session, "Assets:FX:Dollars", kind="fx", commodity="USD")
    rng = random.Random(4)
    rates, _ = load_rates(session)
    fund, share, usd = 1.0, 80.0, 40.0
    for d in DAYS:
        common = rng.gauss(0.0012, 0.006)
        fund *= 1 + common + rng.gauss(0, 0.004)
        share *= 1 + 1.5 * common + rng.gauss(0.0004, 0.016)
        usd *= 1 + rng.gauss(0.0009, 0.003)
        upsert_price(session, "FUND.X", "TRY", d, D(f"{fund:.6f}"), "test")
        upsert_price(session, "SHARE.X", "TRY", d, D(f"{share:.4f}"), "test")
        upsert_price(session, "USD", "TRY", d, D(f"{usd:.4f}"), "test")
        upsert_price(session, "MMF.X", "TRY", d, D(f"{rates.growth(FIRST, d):.8f}"), "test")
    opening_balance(session, "Checking", D(1000000), FIRST)
    buy = DAYS[-30]
    record_exchange(session, "Checking", D(60000), "Fund", D(30000), buy)
    record_exchange(session, "Checking", D(30000), "Share", D(300), buy)
    record_exchange(session, "Checking", D(90000), "Dollars", D(2000), buy)
    record_exchange(session, "Checking", D(50000), "Money Market", D(30000), buy)


@pytest.fixture
def held(session):
    fill(session)
    return session


def test_the_sample_weekly_returns_above_cash(held):
    s = held
    sample = build_sample(s, end=END)
    assert sample.codes == ("FUND.X", "SHARE.X", "USD") and sample.cash_like == ("MMF.X",)
    assert sample.frequency == "weekly" and sample.periods == 52 and sample.rate_source == "bundled"
    # the last weekday of every week
    assert all(d.weekday() == 4 for d in sample.days[:-1]) and sample.days[-1] == END and sample.start == date(2025, 6, 6)
    assert sample.returns.shape == (len(sample.days) - 1, 3) and not sample.jumps
    assert sample.limited_by is not None  # three years were asked for by default; the prices begin later
    from financeproject.prices.store import load_pricebook

    book = load_pricebook(s)
    a, b = sample.days[10], sample.days[11]
    assert sample.returns[10, 1] == pytest.approx(float(book.resolve("SHARE.X", b).value / book.resolve("SHARE.X", a).value) - 1)
    rates, _ = load_rates(s)
    assert sample.cash[10] == pytest.approx(rates.growth(a, b) - 1) and sample.excess[10, 1] == pytest.approx(sample.returns[10, 1] - sample.cash[10])
    # today's shares of the three, by value
    worth = [float(q * book.resolve(c, END).value) for c, q in (("FUND.X", D(30000)), ("SHARE.X", D(300)), ("USD", D(2000)))]
    assert sample.weights == pytest.approx(np.array(worth) / sum(worth)) and sample.weights.sum() == pytest.approx(1)
    assert sample.cash_now == pytest.approx((1 + 0.36 / 365) ** 365 - 1) and sample.rate_from == date(2026, 10, 23)
    # other frequencies and a rate given
    assert build_sample(s, end=END, frequency="daily").returns.shape[0] == len(DAYS) - 1
    with pytest.raises(FinanceError, match="Only 16 monthly returns since 2025-06-02"):
        build_sample(s, end=END, frequency="monthly")
    given = build_sample(s, end=END, risk_free=40.0)
    assert given.rate_source == "given" and given.cash_now == pytest.approx(0.40) and given.rate_from is None
    assert np.prod(1 + given.cash) == pytest.approx(1.40 ** ((END - given.start).days / 365))
    assert given.cash_like == ("MMF.X",)  # it earns another rate, but still does not move
    short = build_sample(s, end=END, start=date(2026, 3, 2))
    assert short.start == date(2026, 3, 6) and short.limited_by is None


def test_the_sample_needs_holdings_history_and_a_rate(held):
    s = held
    with pytest.raises(FinanceError, match="at least two holdings"):
        build_sample(s, end=END, paths=["Assets:Investments:Fund"])
    with pytest.raises(FinanceError, match="Fewer than two holdings move against cash .MMF.X count"):
        build_sample(s, end=END, paths=["Assets:Investments:Fund", "Assets:Investments:Money Market"])
    with pytest.raises(FinanceError, match="at least 20 are needed"):
        build_sample(s, end=END, start=date(2026, 8, 3))
    with pytest.raises(FinanceError, match="frequency is one of"):
        build_sample(s, end=END, frequency="hourly")
    with pytest.raises(FinanceError, match="above"):
        build_sample(s, end=END, risk_free=-100.0)
    # a holding whose prices begin late shortens the window, and is named
    s.add(Commodity(code="NEW.X", name="New", kind="stock", decimals=0, price_source="manual"))
    s.flush()
    create_account(s, "Assets:Investments:New", kind="stock", commodity="NEW.X")
    rng = random.Random(1)
    p = 10.0
    for d in DAYS[-150:]:
        p *= 1 + rng.gauss(0.001, 0.02)
        upsert_price(s, "NEW.X", "TRY", d, D(f"{p:.4f}"), "test")
    record_exchange(s, "Checking", D(1000), "New", D(50), DAYS[-5])
    sample = build_sample(s, end=END)
    assert sample.limited_by == "NEW.X" and sample.start >= DAYS[-150] and "NEW.X" in sample.codes


def test_a_holding_priced_now_and_then_is_left_out_not_taken_for_cash(held):
    s = held
    s.add(Commodity(code="FLAT.X", name="A flat priced once a quarter", kind="stock", decimals=0, price_source="manual"))
    s.flush()
    create_account(s, "Assets:Investments:Flat", kind="stock", commodity="FLAT.X")
    for k, d in enumerate(DAYS[::65]):
        upsert_price(s, "FLAT.X", "TRY", d, D(1000000 + 90000 * k), "manual")
    record_exchange(s, "Checking", D(100000), "Flat", D(1), DAYS[-5])
    sample = build_sample(s, end=END)
    assert sample.sparse == ("FLAT.X",) and "FLAT.X" not in sample.codes and "FLAT.X" not in sample.cash_like
    assert sample.weights.sum() == pytest.approx(1) and len(sample.weights) == 3
    with pytest.raises(FinanceError, match="FLAT.X: too few prices for weekly returns"):
        build_sample(s, end=END, paths=["Assets:Investments:Flat", "Assets:Investments:Fund"])


def test_splits_do_not_count_as_returns(held):
    s = held
    before = build_sample(s, end=END)
    split_day = DAYS[-12]  # while the share is held
    from financeproject.ledger.models import Price

    for row in s.query(Price).filter(Price.commodity_code == "SHARE.X", Price.date >= split_day):
        row.value = row.value / 4
    s.flush()
    jumped = build_sample(s, end=END)
    assert [(c, r < -0.7) for c, _, r in jumped.jumps] == [("SHARE.X", True)]  # an unrecorded split shows as a warning
    upsert_split(s, "SHARE.X", split_day, D(4), "yahoo")
    after = build_sample(s, end=END)
    assert not after.jumps and after.returns == pytest.approx(before.returns, abs=1e-4)
    # the same split recorded in the ledger a day later is not counted twice; another one is added
    record_split(s, "Share", D(4), split_day + timedelta(days=1))
    assert all_splits(s, "SHARE.X") == [(split_day, 4.0)]
    assert upsert_split(s, "SHARE.X", split_day, D(4), "yahoo") == "unchanged"


def test_the_frontier_report(held):
    report = frontier_report(held, end=END, draws=40)
    sample = report.sample
    assert [p.key for p in report.portfolios][:2] == [NOW, pf.EQUAL] and {p.key for p in report.portfolios} <= set(ORDER)
    least = report.portfolio(pf.MIN_VARIANCE)
    assert all(least.volatility <= p.volatility + 1e-12 for p in report.portfolios)
    assert report.frontier[0] == pytest.approx((least.volatility, least.excess))
    best = report.portfolio(pf.MAX_SHARPE)
    if best is not None:
        assert all(best.sharpe >= p.sharpe - 1e-9 for p in report.portfolios)
        assert all(best.sharpe >= x / v - 1e-9 for v, x in report.frontier)
    assert report.portfolio(NOW).weights == pytest.approx(sample.weights) and report.portfolio(NOW).low is None
    assert all(lo <= w + 0.2 and lo <= hi for lo, hi, w in zip(least.low, least.high, least.weights))
    shrunk = pf.covariance(sample.excess)
    for a, j in zip(report.assets, range(3)):
        excess = sample.excess[:, j]
        assert a.excess == pytest.approx(excess.mean() * 52) and a.volatility == pytest.approx(math.sqrt(shrunk[j, j] * 52))
        assert a.low < a.excess < a.high and a.sharpe == pytest.approx(a.excess / a.volatility)
        assert (a.high - a.low) / 2 == pytest.approx(1.645 * excess.std(ddof=1) / math.sqrt(len(excess)) * 52)
        assert 1 + a.grew == pytest.approx(float(np.prod(1 + sample.returns[:, j])) ** (365 / (sample.end - sample.start).days))
    # the frontier ends in the holding with the highest mean: all of one holding is that holding
    top = max(report.assets, key=lambda a: a.excess)
    assert report.frontier[-1] == pytest.approx((top.volatility, top.excess), abs=1e-6)
    assert 0 <= report.shrinkage <= 1 and sorted(report.order) == list(sample.codes) and report.draws == 40
    plain = frontier_report(held, end=END, method="sample", draws=0)
    assert plain.shrinkage is None and plain.portfolio(pf.HRP).low is None
    assert [a.volatility for a in plain.assets] == pytest.approx(list(plain.sample.excess.std(axis=0, ddof=1) * math.sqrt(52)))
    with pytest.raises(FinanceError, match="covariance is one of"):
        frontier_report(held, end=END, method="robust")


def test_the_out_of_sample_report(held):
    report = walk_report(held, end=END, window=26, step=4)
    sample = report.sample
    assert [line.key for line in report.lines] == list(ORDER) and report.tested == sample.returns.shape[0] - 26
    assert report.days[0] == sample.days[26] and report.days[-1] == END
    equal = next(line for line in report.lines if line.key == pf.EQUAL)
    assert equal.p_value is None and equal.ahead == pytest.approx([0.0] * len(report.days))
    # equal weights by hand: rebalanced every 4 periods, drifting in between
    r = sample.returns[26:]
    w, value, growth = np.full(3, 1 / 3), 1.0, []
    for i, row in enumerate(r):
        if i % 4 == 0:
            w = np.full(3, 1 / 3)
        step = float(w @ row)
        w = w * (1 + row) / (1 + step)
        value *= 1 + step
        growth.append(value)
    span = (report.days[-1] - report.days[0]).days
    assert 1 + equal.grew == pytest.approx(value ** (365 / span))
    for line in report.lines:
        assert len(line.ahead) == len(report.days) and line.ahead[0] == 0 and 0 <= line.drawdown < 1 and line.turnover >= 0
        assert sum(line.weights) == pytest.approx(1)
        assert line.key == pf.EQUAL or line.p_value is None or 0 <= line.p_value <= 1
    assert walk_report(held, end=END).window == 36  # half the history, when a year is too long
    with pytest.raises(FinanceError, match="leave"):
        walk_report(held, end=END, window=70)
    with pytest.raises(FinanceError, match="at least 10 periods"):
        walk_report(held, end=END, window=4)
    with pytest.raises(FinanceError, match="1 or more"):
        walk_report(held, end=END, step=-1)


def test_cash_as_a_benchmark(held):
    rates, source = load_rates(held)
    cash = cash_benchmark(rates, source, date(2026, 1, 1), date(2026, 1, 22))  # 38 % all along
    assert cash.total == pytest.approx((1 + 0.38 / 365) ** 22 - 1) and cash.source == "bundled"
    assert cash.year == pytest.approx((1 + 0.38 / 365) ** 365 - 1)
    assert cash_benchmark(rates, source, date(2000, 1, 1), date(2026, 1, 1)) is None


# --------------------------------------------------------------------------- the policy rate


def test_the_policy_rate_page(fixtures):
    rows = tcmb_rates.parse_rate_table((fixtures / "tcmb_repo_sample.html").read_text(encoding="utf-8"))
    assert rows[0] == (date(2010, 5, 20), D("7.00")) and rows[-1] == (date(2026, 10, 23), D("36.00")) and len(rows) == 8
    with pytest.raises(SourceError, match="No rate table"):
        tcmb_rates.parse_rate_table("<table><tr><td>Tarih</td><td>-</td><td>x</td></tr></table>")
    snapshot = bundled_rates()
    assert snapshot[date(2010, 5, 20)] == 7.0 and snapshot[date(2024, 3, 22)] == 50.0 and list(snapshot) == sorted(snapshot)


def test_updating_the_policy_rate(session, fixtures):
    page = (fixtures / "tcmb_repo_sample.html").read_bytes()
    assert load_rates(session)[1] == "bundled"
    with make_client(transport=httpx.MockTransport(lambda request: httpx.Response(500))) as client:
        report = update.update_rates(session, client)
        assert report.added == 0 and "copy shipped with the program" in report.warnings[0]
    seen = []
    with make_client(transport=httpx.MockTransport(lambda request: seen.append(request.url) or httpx.Response(200, content=page))) as client:
        report = update.update_rates(session, client)
        assert report.added == 8 and "now 36.00 %" in report.messages[0] and seen[0].host == "www.tcmb.gov.tr"
        assert update.update_rates(session, client).added == 0
    rates, source = load_rates(session)
    assert source == "tcmb-rates" and rates.at(date(2026, 11, 1)) == 36.0 and len(load_index(session, POLICY_RATE)) == 8
    assert rates.at(date(2024, 4, 1)) == 50.0  # a day the shortened page does not cover: from the copy shipped with the program
    assert "rates" in update.SOURCES


# --------------------------------------------------------------------------- commands


def test_the_commands(tmp_path):
    db = tmp_path / "ledger.sqlite"
    runner = CliRunner(env={"COLUMNS": "220"})

    def run(*args):
        return runner.invoke(app, ["-d", str(db), *args])

    assert run("init").exit_code == 0
    engine = make_engine(db)
    with session_scope(engine) as s:
        fill(s)
    engine.dispose()
    out = run("frontier", "--end", END.isoformat(), "--draws", "20")
    assert out.exit_code == 0, out.output
    for text in ("What you hold now", "Equal weights (1/N)", "Least risk", "Hierarchical risk parity", "Efficient frontier",
                 "Counted as cash", "MMF.X", "20 resampled histories", "not advice", "Ledoit-Wolf"):
        assert text in out.output, text
    out = run("backtest", "--end", END.isoformat(), "--window", "26", "--covariance", "sample", "--risk-free", "40")
    assert out.exit_code == 0, out.output
    assert "Against 1/N" in out.output and "given: 40.00 % a year" in out.output and "Chosen from the 26 periods before" in out.output
    assert "Cash over the same days" in run("returns", "--start", "2026-10-01", "--end", END.isoformat()).output
    for bad in (("frontier", "--frequency", "hourly"), ("frontier", "--draws", "9999"), ("backtest", "--end", END.isoformat(), "--window", "3")):
        out = run(*bad)
        assert out.exit_code != 0 and "Traceback" not in out.output


def test_market_days_are_the_days_most_holdings_are_quoted(held):
    s = held
    create_account(s, "Assets:Gold:Grams", kind="gold", commodity="XAU_G")
    day = FIRST
    while day <= END:  # gold is quoted every day of the week
        upsert_price(s, "XAU_G", "USD", day, D("100") + D(day.toordinal() % 7), "test")
        day += timedelta(days=1)
    record_exchange(s, "Checking", D(50000), "Grams", D(10), DAYS[-5])
    weekly = build_sample(s, end=END)
    assert "XAU_G" in weekly.codes and all(d.weekday() == 4 for d in weekly.days) and not weekly.sparse
    daily = build_sample(s, end=END, frequency="daily")
    assert all(d.weekday() < 5 for d in daily.days) and len(daily.days) == len(DAYS) and not daily.sparse
