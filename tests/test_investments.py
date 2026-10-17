"""Returns, risk and concentration: the formulas, then the reports on a ledger."""

import random
from datetime import date, timedelta
from decimal import Decimal
from statistics import NormalDist, fmean, stdev

import pytest
from typer.testing import CliRunner

from financeproject.cli import app
from financeproject.core import risk
from financeproject.core.dates import business_days
from financeproject.core.returns import (
    Day,
    annualize,
    daily_returns,
    in_unit,
    money_weighted,
    twr,
    xirr,
)
from financeproject.errors import FinanceError
from financeproject.ledger.accounts import create_account, get_person
from financeproject.ledger.models import Commodity
from financeproject.ledger.transactions import (
    opening_balance,
    record_exchange,
    record_expense,
    record_income,
    record_split,
    record_transfer,
)
from financeproject.prices.store import upsert_price
from financeproject.reports.investments import (
    concentration_report,
    insured_limit,
    portfolio_accounts,
    returns_report,
    risk_report,
)

D = Decimal
D0, D1, D2, D3, D4 = (date(2026, 3, 1) + timedelta(days=k) for k in range(5))


# --------------------------------------------------------------------------- formulas


def test_time_weighted_return_counts_inflows_at_the_start_and_outflows_at_the_end():
    days = [
        Day(D0, 1000),
        Day(D1, 1100),  # +10 %
        Day(D2, 2200, inflow=1100),  # 1,100 more at the day's start, no change in price
        Day(D3, 1100),  # −50 %
        Day(D4, 500, outflow=600),  # 600 taken out at the day's end, no change in price
    ]
    assert [round(r, 10) for _, r in daily_returns(days)] == [0.1, 0.0, -0.5, 0.0]
    assert twr(days) == pytest.approx(1.1 * 0.5 - 1)
    assert in_unit(days, {d.on: 0.5 for d in days})[2] == Day(D2, 1100, 550, 0)
    with pytest.raises(ValueError, match="without a starting value"):
        daily_returns([Day(D0, 0), Day(D1, 50)])


def test_money_weighted_return():
    # the example of the spreadsheet function XIRR
    flows = [(date(2008, 1, 1), -10000), (date(2008, 3, 1), 2750), (date(2008, 10, 30), 4250), (date(2009, 2, 15), 3250), (date(2009, 4, 1), 2750)]
    assert xirr(flows) == pytest.approx(0.373362535, abs=1e-8)
    assert xirr([(D0, -100), (D1, -5)]) is None
    year = [Day(date(2025, 1, 1), 1000), Day(date(2026, 1, 1), 1100)]
    assert money_weighted(year) == pytest.approx(0.10)
    assert annualize(0.21, 730) == pytest.approx(0.10) and annualize(-1, 10) is None


def test_value_at_risk_and_expected_shortfall():
    returns = [-0.05, -0.04, -0.03, -0.02, -0.01, 0.0, 0.01, 0.02, 0.03, 0.04] * 2
    assert risk.historical_var(returns, 0.95) == risk.ValueAtRisk(0.95, 0.05, 0.05)  # k = ⌈20 · 5 %⌉ = 1
    v = risk.historical_var(returns, 0.90)  # the two worst days
    assert (v.var, v.cvar) == (pytest.approx(0.05), pytest.approx(0.05))
    v = risk.historical_var(returns, 0.80)
    assert (v.var, v.cvar) == (pytest.approx(0.04), pytest.approx(0.045))
    mu, sigma, z = fmean(returns), stdev(returns), NormalDist().inv_cdf(0.99)
    v = risk.normal_var(returns, 0.99)
    assert v.var == pytest.approx(-(mu - z * sigma)) and v.cvar == pytest.approx(-mu + sigma * NormalDist().pdf(z) / 0.01)
    assert risk.historical_var([], 0.95) is None and risk.normal_var([0.1], 0.95) is None


def test_kupiec_matches_the_published_acceptance_regions():
    # Kupiec (1995), as tabulated in Jorion's "Value at Risk": 255 days, exceptions not rejected at 5 %
    accepted = lambda x, level: risk.kupiec(x, 255, level)[1] >= 0.05  # noqa: E731
    assert accepted(6, 0.99) and not accepted(7, 0.99)  # N < 7
    assert not accepted(6, 0.95) and accepted(7, 0.95) and accepted(20, 0.95) and not accepted(21, 0.95)  # 6 < N < 21


def test_christoffersen_sees_clustering():
    spread = ([False] * 19 + [True]) * 10
    clustered = [True] * 10 + [False] * 190
    assert risk.christoffersen(spread)[1] > 0.05
    assert risk.christoffersen(clustered)[1] < 0.001
    assert risk.christoffersen([False] * 50) is None


def test_backtest_counts_exceptions():
    rng = random.Random(3)
    returns = [rng.gauss(0, 0.01) for _ in range(600)]
    b = risk.backtest(returns, 0.99, window=250)
    expected_hits = sum(-returns[t] > risk.historical_var(returns[t - 250 : t], 0.99).var for t in range(250, 600))
    assert (b.days, b.exceptions, b.expected) == (350, expected_hits, pytest.approx(3.5))
    assert 0 <= b.kupiec_p <= 1
    assert risk.backtest(returns[:250], 0.99) is None


def test_drawdown_volatility_and_correlation():
    days = [date(2026, 1, k) for k in range(1, 6)]
    index = list(zip(days, [1.0, 1.2, 0.9, 1.1, 1.25]))
    dd = risk.max_drawdown(index)
    assert (dd.depth, dd.peak, dd.bottom, dd.recovered) == (pytest.approx(0.25), days[1], days[2], days[4])
    assert dd.days(days[4]) == 3
    still = risk.max_drawdown(index[:4])
    assert still.recovered is None and still.days(days[3]) == 2
    assert risk.max_drawdown(list(zip(days, [1, 2, 3, 4, 5]))) is None
    assert risk.volatility([0.01] * 10) == 0 and risk.volatility([0.01]) is None
    assert risk.correlation([1, 2, 3, 4], [2, 4, 6, 8]) == pytest.approx(1)
    assert risk.correlation([1, 2, 3, 4], [4, 3, 2, 1]) == pytest.approx(-1)
    assert risk.correlation([1, 1, 1], [1, 2, 3]) is None


# --------------------------------------------------------------------------- reports


@pytest.fixture
def portfolio(session):
    session.add(Commodity(code="FUND.X", name="Örnek fon", kind="fund", decimals=6, price_source="manual"))
    session.add(Commodity(code="SHARE.X", name="Örnek hisse", kind="stock", decimals=0, price_source="manual"))
    session.flush()
    create_account(session, "Assets:Bank:Checking", kind="checking", institution="Bank A")
    create_account(session, "Assets:Investments:Fund", kind="fund", commodity="FUND.X", institution="Broker")
    create_account(session, "Assets:Investments:Share", kind="stock", commodity="SHARE.X", institution="Broker")
    create_account(session, "Assets:Investments:Cash", kind="checking", institution="Broker")
    create_account(session, "Assets:FX:Dollars", kind="fx", commodity="USD", institution="Bank B")
    for path in ("Income:Dividends", "Expenses:Investments:Fees", "Expenses:Travel"):
        create_account(session, path)
    opening_balance(session, "Checking", D(100000), D0 - timedelta(days=30))
    for d in (D0 - timedelta(days=30), D0, D1, D2, D3, D4):
        upsert_price(session, "USD", "TRY", d, D(40), "test")
    for d, p in ((D1, "1.0"), (D2, "1.1"), (D3, "0.55")):
        upsert_price(session, "FUND.X", "TRY", d, D(p), "test")
    return session


def test_returns_of_a_fund_bought_twice(portfolio):
    s = portfolio
    record_exchange(s, "Checking", D(1000), "Fund", D(1000), D1)
    record_exchange(s, "Checking", D(1100), "Fund", D(1000), D2)
    report = returns_report(s, D1, D3, ["Assets:Investments:Fund"])
    try_, = (u for u in report.units if u.name == "TRY")
    assert (try_.start_value, try_.inflows, try_.outflows, try_.end_value) == (0, 2100, 0, pytest.approx(1100))
    # the second purchase counts from the start of its day (Portfolio Performance's rule):
    # (2,000 units · 1.1) / (1,000 + 1,100) on D2, then −50 %
    assert try_.twr == pytest.approx(2200 / 2100 * 0.5 - 1) and try_.gain == pytest.approx(-1000)
    usd, = (u for u in report.units if u.name == "USD")
    assert usd.twr == pytest.approx(try_.twr) and usd.end_value == pytest.approx(1100 / 40)  # the dollar did not move
    assert report.accounts == ("Assets:Investments:Fund",) and report.days == 3


def test_spending_from_a_currency_account_is_money_out_not_a_loss(portfolio):
    s = portfolio
    record_exchange(s, "Checking", D(4000), "Dollars", D(100), D1)
    record_expense(s, D(50), "Travel", "Dollars", D2, "Hotel")
    report = returns_report(s, D1, D3, ["Dollars"])
    try_ = report.units[0]
    assert (try_.inflows, try_.outflows, try_.twr) == (4000, 2000, 0)


def test_dividends_and_fees_in_the_portfolio_are_return(portfolio):
    s = portfolio
    record_transfer(s, D(1000), "Checking", "Assets:Investments:Cash", D1)
    record_income(s, D(50), "Dividends", "Assets:Investments:Cash", D2, "Dividend")
    record_expense(s, D(10), "Fees", "Assets:Investments:Cash", D3, "Custody fee")
    report = returns_report(s, D1, D3, ["Assets:Investments:Cash"])
    assert report.units[0].twr == pytest.approx(0.04) and report.units[0].inflows == 1000


def test_a_split_is_no_return(portfolio):
    s = portfolio
    upsert_price(s, "SHARE.X", "TRY", D1, D(100), "test")
    upsert_price(s, "SHARE.X", "TRY", D2, D(50), "test")
    opening_balance(s, "Share", D(10), D1)
    record_split(s, "Share", D(2), D2)
    report = returns_report(s, D1, D3, ["Share"])
    assert report.units[0].twr == pytest.approx(0) and report.units[0].inflows == 1000


def test_the_default_portfolio_and_accounts(portfolio):
    s = portfolio
    assert [a.path for a in portfolio_accounts(s)] == ["Assets:FX:Dollars", "Assets:Investments:Fund", "Assets:Investments:Share"]
    assert [a.path for a in portfolio_accounts(s, ["Assets:Investments"])] == [
        "Assets:Investments:Cash", "Assets:Investments:Fund", "Assets:Investments:Share",
    ]
    with pytest.raises(FinanceError, match="asset accounts"):
        portfolio_accounts(s, ["Travel"])
    record_exchange(s, "Checking", D(1000), "Fund", D(1000), D1)
    record_exchange(s, "Checking", D(4000), "Dollars", D(100), D1)
    report = returns_report(s, D1, D3)
    assert [r.name for r in report.by_account] == ["Assets:FX:Dollars", "Assets:Investments:Fund"]
    record_exchange(s, "Checking", D(500), "Fund", D(500), D0)  # the fund has no price before D1
    with pytest.raises(FinanceError, match="No price of FUND.X on or before 2026-03-01"):
        returns_report(s, D0, D3, ["Fund"])


def test_risk_report(portfolio):
    s = portfolio
    rng = random.Random(11)
    days = list(business_days(date(2025, 1, 1), date(2026, 3, 31)))
    price = 1.0
    for d in days:
        price *= 1 + rng.gauss(0.0005, 0.01)
        upsert_price(s, "FUND.X", "TRY", d, D(str(round(price, 6))), "test")
        upsert_price(s, "SHARE.X", "TRY", d, D(str(round(100 * price**0.5 * (1 + rng.gauss(0, 0.005)), 2))), "test")
    upsert_price(s, "USD", "TRY", date(2025, 1, 1), D(40), "test")
    record_exchange(s, "Checking", D(10000), "Fund", D(10000), date(2025, 1, 1))
    record_exchange(s, "Checking", D(10000), "Share", D(100), date(2025, 1, 1))
    report = risk_report(s, date(2025, 1, 2), date(2026, 3, 31), ["Assets:Investments"])
    assert len(report.returns) == len(days) - 1
    assert 0.05 < report.volatility < 0.25
    assert [b.level for b in report.backtests] == [0.95, 0.99] and report.backtests[0].days == len(days) - 1 - 250
    assert [h.commodity for h in report.holdings] == ["FUND.X", "SHARE.X"]
    assert report.holdings[0].correlation > 0.5
    assert report.correlations[("FUND.X", "SHARE.X")] > 0.3
    assert report.historical[0].level == 0.95 and report.historical[0].var > 0


def test_concentration_and_deposit_insurance(portfolio):
    s = portfolio
    ayse = get_person(s, "Ayşe", create=True)
    create_account(s, "Assets:Bank:Time Deposit", kind="time_deposit", institution="Bank A", owner="Ayşe")
    create_account(s, "Assets:Bank:Ayse Checking", kind="checking", institution="Bank A", owner="Ayşe")
    create_account(s, "Assets:Home:Gold", kind="gold")
    upsert_price(s, "XAU_G", "USD", D0, D(100), "test")
    opening_balance(s, "Ayse Checking", D(1_000_000), D0)
    opening_balance(s, "Time Deposit", D(300_000), D0)
    opening_balance(s, "Gold", D(10), D0)
    upsert_price(s, "FUND.X", "TRY", D0, D(1), "test")
    record_exchange(s, "Checking", D(50000), "Fund", D(50000), D0)
    report = concentration_report(s, D0)
    assert ayse.name == "Ayşe" and report.total == D(100000) + D(1_300_000) + D(40000)
    assert [x.name for x in report.by_holding] == ["TRY", "FUND.X", "XAU_G"]
    assert report.by_institution[0].name == "Bank A" and report.by_currency[0].name == "TRY"
    assert report.hhi == pytest.approx(sum(x.share**2 for x in report.by_holding))
    over = [d for d in report.deposits if d.over > 0]
    assert [(d.owner, d.institution, d.over) for d in over] == [("Ayşe", "Bank A", D(100000))]
    flags = report.flags()
    assert "94 % in one institution: Bank A" in flags
    assert any("Ayşe at Bank A" in f for f in flags)
    assert not any("in one holding" in f for f in flags)  # the fund is 3 %
    assert insured_limit(2026) == (D(1_200_000), 2026) and insured_limit(2031) == (D(1_200_000), 2026)
    assert insured_limit(2025) == (D(950_000), 2025)


# --------------------------------------------------------------------------- commands


def test_investment_commands(tmp_path):
    runner = CliRunner(env={"COLUMNS": "200"})
    db = str(tmp_path / "ledger.sqlite")

    def run(*args):
        return runner.invoke(app, ["-d", db, *args])

    def ok(*args):
        result = run(*args)
        assert result.exit_code == 0, result.output
        return result.output

    ok("init")
    assert "TEFAS.<fund code>" in run("commodity", "add", "AFT", "Fon", "--kind", "fund", "--price-source", "tefas").output
    ok("commodity", "add", "TEFAS.TST", "Örnek fon", "--kind", "fund", "--price-source", "tefas")
    ok("commodity", "add", "BIST.TESTA", "Örnek hisse", "--kind", "stock", "--price-source", "yahoo", "--decimals", "0")
    ok("account", "add", "Assets:Bank:Checking", "--kind", "checking", "--institution", "Bank A")
    ok("account", "add", "Assets:Investments:Fund", "--kind", "fund", "--commodity", "TEFAS.TST", "--institution", "Broker")
    ok("account", "add", "Assets:Investments:Share", "--kind", "stock", "--commodity", "BIST.TESTA", "--institution", "Broker")
    ok("opening", "Checking", "100000", "--date", "2026-01-01")
    for d, fund, share in (("2026-01-02", "1.0", "50"), ("2026-02-02", "1.2", "40"), ("2026-03-02", "1.5", "22")):
        ok("price", "add", "TEFAS.TST", fund, "--date", d)
        ok("price", "add", "BIST.TESTA", share, "--date", d)
    ok("exchange", "--from", "Checking", "--paid", "10000", "--to", "Fund", "--received", "10000", "--date", "2026-01-02")
    ok("exchange", "--from", "Checking", "--paid", "5000", "--to", "Share", "--received", "100", "--date", "2026-01-02")
    assert "Split of BIST.TESTA: 2 for 1: 100 → 200" in ok("split", "Share", "2:1", "--date", "2026-03-02")
    out = ok("returns", "--end", "2026-03-02")
    assert "Investments · 2026-01-02 – 2026-03-02 (60 days)" in out and "Assets:Investments:Share" in out
    assert "50.00 %" in out  # the fund went from 1.0 to 1.5
    assert "'a year' extends a period shorter than a year" in out
    out = ok("risk", "--end", "2026-03-02")
    assert "too few for risk figures" in out
    out = ok("concentration", "--date", "2026-03-02")
    assert "Concentration · 2026-03-02" in out and "Deposit insurance: 1,200,000 TRY per person and bank (TMSF, 2026)" in out
    assert "in one institution: Bank A" in out
    out = ok("concentration", "--date", "2026-03-02", "--insured-limit", "50000", "--threshold", "95")
    assert "over the insured 50,000" in out and "(given)" in out
    assert run("split", "Checking", "2").exit_code == 1
    assert run("risk", "--window", "5").exit_code == 1
