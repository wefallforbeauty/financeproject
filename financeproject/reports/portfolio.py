"""Portfolio theory on the holdings: the efficient portfolios, how fragile they are, and how they did out of sample.

**The assets** are what the investment accounts hold today (``reports.investments``),
each with the price history the ledger has for it, whether or not it was held
then. Prices are sampled at the last market day of every week (the default),
day or month; a fund's price is published a day late and markets close at
different hours, which blurs daily returns of different assets more than
weekly ones. Share prices are followed through every split the price source
reported or the ledger recorded.

**Returns are in excess of cash**: each period's return minus what Turkish
lira earned at the TCMB policy rate over the same days (``core.rates``), or at
a rate given. A holding that barely moves against cash (a money market fund)
counts as cash and is left out: its ratio of return to risk would divide by
almost nothing.

Everything here describes the past of a short sample. It is not advice.
"""

from __future__ import annotations

import math
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta

import numpy as np
from sqlalchemy.orm import Session

from financeproject.core import portfolio as pf
from financeproject.core.kinds import ASSET
from financeproject.core.rates import RateSteps, constant, effective_annual
from financeproject.core.valuation import value_positions
from financeproject.errors import FinanceError
from financeproject.ledger.queries import positions_at
from financeproject.prices.store import load_rates, stored_splits
from financeproject.reports.context import MarketData, load_market_data
from financeproject.reports.investments import portfolio_accounts, recorded_splits

DAILY, WEEKLY, MONTHLY = "daily", "weekly", "monthly"
PERIODS = {DAILY: 252, WEEKLY: 52, MONTHLY: 12}
"""Periods a year."""
WINDOWS = {DAILY: 250, WEEKLY: 52, MONTHLY: 36}
"""Periods behind each choice in the out-of-sample test, by default."""
STEPS = {DAILY: 21, WEEKLY: 4, MONTHLY: 1}
"""Periods between rebalancings: about a month."""
MIN_PERIODS = 20
CASH_LIKE = 0.005
"""Yearly volatility against cash under which a holding counts as cash."""
SHORTER = 31
"""Days by which the prices must begin after the day asked for before the report says the window is shorter."""
SPARSE = 1 / 3
"""A holding without a new price in more than this share of the periods is left out."""
JUMP = (-0.45, 0.9)
"""A return of one period outside this range looks like a split nobody recorded."""
YEARS = 3
Z90 = 1.645
NOW = "now"
MAX_SHARPE = pf.MAX_SHARPE
ORDER = (NOW, pf.EQUAL, pf.MIN_VARIANCE, pf.MAX_SHARPE, pf.HRP)
LABELS = {
    NOW: "What you hold now",
    pf.EQUAL: "Equal weights (1/N)",
    pf.MIN_VARIANCE: "Least risk",
    pf.MAX_SHARPE: "Best return for its risk",
    pf.HRP: "Hierarchical risk parity",
}
"""How the reports name the portfolios."""


@dataclass(frozen=True)
class Sample:
    """The returns the reports work on."""

    codes: tuple[str, ...]
    days: list[date]
    """The sampling days; one more than the returns."""
    returns: np.ndarray
    """``T × N`` simple returns in TRY."""
    cash: np.ndarray
    """What cash earned in each period."""
    weights: np.ndarray
    """Today's shares of the assets in their total value."""
    values: dict[str, float]
    frequency: str
    accounts: tuple[str, ...]
    rate_source: str
    cash_now: float
    """What cash earns a year at the rate of the last day (compounded)."""
    rate_from: date | None
    """The day the rate in force took effect (``None`` for a rate given)."""
    cash_like: tuple[str, ...] = ()
    unpriced: tuple[str, ...] = ()
    sparse: tuple[str, ...] = ()
    """Holdings left out because too many periods passed without a new price."""
    limited_by: str | None = None
    """The asset whose prices start last."""
    jumps: tuple[tuple[str, date, float], ...] = ()

    @property
    def periods(self) -> int:
        return PERIODS[self.frequency]

    @property
    def excess(self) -> np.ndarray:
        return self.returns - self.cash[:, None]

    @property
    def start(self) -> date:
        return self.days[0]

    @property
    def end(self) -> date:
        return self.days[-1]


def all_splits(session: Session, code: str) -> list[tuple[date, float]]:
    """The splits of ``code``: those its price source reported, and those recorded in the ledger
    that are not within a week of one of them."""
    known = stored_splits(session, code)
    for on, ratio in recorded_splits(session, code):
        if not any(abs((on - other).days) <= 7 for other, _ in known):
            known.append((on, ratio))
    return sorted(known)


def sampling_days(market: MarketData, codes: list[str], start: date, end: date, frequency: str) -> list[date]:
    """The last market day of every week or month from ``start`` to ``end`` (every market day for daily).

    A market day is one on which most of the holdings have a quote: gold is quoted on weekends and
    on local holidays too, when nothing else moves. Holdings quoted far less often than the others
    (a price typed in now and then) have no say in it."""
    own = {code: [d for d in market.book.dates(code) if start <= d <= end] for code in codes}
    most = max(len(days) for days in own.values())
    often = [code for code in codes if len(own[code]) * 3 >= most]
    quoted = Counter(d for code in often for d in own[code])
    chosen = sorted(d for d, count in quoted.items() if count > len(often) / 2)
    if frequency == DAILY:
        return chosen
    last: dict[tuple[int, int], date] = {}
    for d in chosen:
        key = tuple(d.isocalendar()[:2]) if frequency == WEEKLY else (d.year, d.month)
        last[key] = d
    return sorted(last.values())


def current_values(session: Session, market: MarketData, accounts, on: date) -> tuple[dict[str, float], list[str]]:
    """The TRY value of each commodity the accounts hold on ``on`` (not the lira itself), and those without a price."""
    ids = {a.id for a in accounts}
    values: dict[str, float] = defaultdict(float)
    unpriced: set[str] = set()
    for v in value_positions(positions_at(session, on, types=(ASSET,)), market.book, on):
        p = v.position
        if p.account_id not in ids or p.commodity == market.book.base or p.quantity <= 0:
            continue
        if v.value is None:
            unpriced.add(p.commodity)
        else:
            values[p.commodity] += float(v.value)
    return {c: v for c, v in values.items() if v > 0}, sorted(unpriced)


def _first_priced(market: MarketData, code: str) -> date:
    """The first day ``code`` has a price in TRY (gold needs the dollar's as well)."""
    for d in market.book.dates(code):
        if market.book.resolve(code, d) is not None:
            return d
    raise FinanceError(f"No price of {code}: get prices with 'finance price update'.")


def build_sample(
    session: Session,
    *,
    start: date | None = None,
    end: date | None = None,
    paths: list[str] | None = None,
    frequency: str = WEEKLY,
    risk_free: float | None = None,
    market: MarketData | None = None,
    years: int = YEARS,
) -> Sample:
    """The holdings' returns from ``start`` (default: as far back as every holding has prices, at most
    ``years`` years) to ``end``. ``risk_free`` is a yearly rate in per cent that replaces the policy rate."""
    if frequency not in PERIODS:
        raise FinanceError(f"The frequency is one of: {', '.join(PERIODS)}.")
    market = market or load_market_data(session)
    end = end or date.today()
    accounts = portfolio_accounts(session, paths)
    values, unpriced = current_values(session, market, accounts, end)
    codes = sorted(values)
    if len(codes) < 2:
        raise FinanceError("Portfolio theory needs at least two holdings with prices; these accounts hold fewer.")
    firsts = {c: _first_priced(market, c) for c in codes}
    limited_by = max(codes, key=lambda c: firsts[c])
    earliest = max(firsts.values())
    floor = start or date(end.year - years, end.month, min(end.day, 28))
    days = sampling_days(market, codes, max(earliest, floor), end, frequency)
    if len(days) < MIN_PERIODS + 1:
        raise FinanceError(
            f"Only {max(len(days) - 1, 0)} {frequency} returns since {max(earliest, floor)}, when the prices of "
            f"{limited_by} begin; at least {MIN_PERIODS} are needed. Get older prices with "
            "'finance price update --start YYYY-MM-DD', or choose another --frequency."
        )
    prices = np.empty((len(days), len(codes)))
    quiet = dict.fromkeys(codes, 0)  # periods that passed without a new price
    for j, code in enumerate(codes):
        splits = all_splits(session, code)
        for i, d in enumerate(days):
            found = market.book.resolve(code, d)
            p = float(found.value)
            for on, ratio in splits:
                if on > d:
                    p /= ratio
            prices[i, j] = p
            if i and found.as_of <= days[i - 1]:
                quiet[code] += 1
    if (prices <= 0).any():
        raise FinanceError(f"{codes[int(np.argmax((prices <= 0).any(axis=0)))]} has a price of zero; correct it with 'finance price add'.")
    returns = prices[1:] / prices[:-1] - 1
    # a holding priced now and then would look calm, even like cash: it is left out, and said so
    sparse = tuple(c for c in codes if quiet[c] > (len(days) - 1) * SPARSE)
    if risk_free is not None:
        if risk_free <= -100:
            raise FinanceError("The risk-free rate must be above −100 %.")
        rates, source, rate_from = constant(risk_free), "given", None
        cash_now = risk_free / 100
    else:
        rates, source = load_rates(session)
        if not rates or rates.first > days[0]:
            raise FinanceError("No policy rate that far back; give a rate with --risk-free.")
        rate_from, rate = rates.last()
        cash_now = effective_annual(rate)
    cash = np.array(rates.period_returns(days))
    volatility = (returns - cash[:, None]).std(axis=0, ddof=1) * math.sqrt(PERIODS[frequency])
    usable = [j for j in range(len(codes)) if codes[j] not in sparse]
    keep = [j for j in usable if volatility[j] >= CASH_LIKE]
    cash_like = tuple(codes[j] for j in usable if volatility[j] < CASH_LIKE)
    if len(keep) < 2:
        raise FinanceError(
            "Fewer than two holdings move against cash"
            + (f" ({', '.join(cash_like)} count as cash)" if cash_like else "")
            + (f" ({', '.join(sparse)}: too few prices for {frequency} returns)" if sparse else "")
            + "; there is nothing to weigh against each other."
        )
    jumps = tuple(
        (codes[j], days[i + 1], float(returns[i, j]))
        for i, j in zip(*np.nonzero((returns < JUMP[0]) | (returns > JUMP[1])))
        if j in keep
    )
    kept = tuple(codes[j] for j in keep)
    worth = np.array([values[c] for c in kept])
    return Sample(
        kept, days, returns[:, keep], cash, worth / worth.sum(), {c: values[c] for c in kept}, frequency,
        tuple(a.path for a in accounts), source, cash_now, rate_from, cash_like, tuple(unpriced),
        sparse, limited_by if (firsts[limited_by] - floor).days > SHORTER else None, jumps,
    )


# --------------------------------------------------------------------------- the efficient portfolios


@dataclass(frozen=True)
class AssetLine:
    code: str
    weight: float
    value: float
    grew: float
    """What it returned a year over the window (compounded)."""
    excess: float
    """Mean return above cash, a year."""
    low: float
    high: float
    """The 90 % range of that mean."""
    volatility: float
    sharpe: float | None


@dataclass(frozen=True)
class PortfolioLine:
    key: str
    weights: list[float]
    excess: float
    volatility: float
    sharpe: float | None
    low: list[float] | None = None
    high: list[float] | None = None
    """The 5th and 95th percentile of each weight over the resampled histories."""
    average: list[float] | None = None


@dataclass(frozen=True)
class FrontierReport:
    sample: Sample
    method: str
    shrinkage: float | None
    assets: list[AssetLine]
    portfolios: list[PortfolioLine]
    frontier: list[tuple[float, float]]
    """(volatility, return above cash) a year along the efficient frontier."""
    cash_grew: float
    """What cash returned a year over the window."""
    draws: int
    order: list[str] = field(default_factory=list)
    """The assets as the tree of hierarchical risk parity orders them."""

    def portfolio(self, key: str) -> PortfolioLine | None:
        return next((p for p in self.portfolios if p.key == key), None)


def _settled(compute):
    """Run the optimizers; say it plainly in the rare case that they cannot work on these returns."""
    try:
        return compute()
    except (ArithmeticError, ValueError, np.linalg.LinAlgError) as exc:
        raise FinanceError(f"The portfolios cannot be computed from these returns ({exc}); try another frequency or window.") from exc


def _annual_growth(total: float, days: int) -> float:
    return (1 + total) ** (365 / days) - 1 if days > 0 and total > -1 else 0.0


def frontier_report(
    session: Session,
    *,
    start: date | None = None,
    end: date | None = None,
    paths: list[str] | None = None,
    frequency: str = WEEKLY,
    risk_free: float | None = None,
    method: str = pf.SHRUNK,
    draws: int = 200,
    market: MarketData | None = None,
) -> FrontierReport:
    if method not in pf.COVARIANCES:
        raise FinanceError(f"The covariance is one of: {', '.join(pf.COVARIANCES)}.")
    sample = build_sample(session, start=start, end=end, paths=paths, frequency=frequency, risk_free=risk_free, market=market)
    excess, periods = sample.excess, sample.periods
    t, n = excess.shape
    mean = excess.mean(axis=0)
    cov = pf.covariance(excess, method)
    span = (sample.end - sample.start).days
    sd = np.sqrt(np.diag(pf.sample_covariance(excess)))  # for the error of the mean
    risk = np.sqrt(np.diag(cov))  # the volatility the portfolios are built on, so that all of one holding is that holding
    assets = []
    for j, code in enumerate(sample.codes):
        se = sd[j] / math.sqrt(t)
        total = float(np.prod(1 + sample.returns[:, j]) - 1)
        assets.append(AssetLine(
            code, float(sample.weights[j]), sample.values[code], _annual_growth(total, span), float(mean[j] * periods),
            float((mean[j] - Z90 * se) * periods), float((mean[j] + Z90 * se) * periods), float(risk[j] * math.sqrt(periods)),
            float(mean[j] / risk[j] * math.sqrt(periods)) if risk[j] > 0 else None,
        ))

    def line(key: str, w: np.ndarray, spread: pf.Resampled | None = None) -> PortfolioLine:
        vol = math.sqrt(float(w @ cov @ w) * periods)
        ex = float(mean @ w) * periods
        return PortfolioLine(
            key, [float(x) for x in w], ex, vol, ex / vol if vol > 0 else None,
            None if spread is None else [float(x) for x in spread.low],
            None if spread is None else [float(x) for x in spread.high],
            None if spread is None else [float(x) for x in spread.mean],
        )

    chosen = _settled(lambda: {s: pf.choose(s, excess, method) for s in pf.STRATEGIES})
    spreads = _settled(lambda: pf.resample(excess, (pf.MIN_VARIANCE, pf.MAX_SHARPE, pf.HRP), method, draws)) if draws > 0 else {}
    portfolios = [line(NOW, sample.weights)]
    portfolios += [line(s, w, spreads.get(s)) for s, w in chosen.items() if w is not None]
    curve = [
        (math.sqrt(float(w @ cov @ w) * periods), float(mean @ w) * periods)
        for w in _settled(lambda: pf.frontier(mean, cov))
    ]
    cash_total = float(np.prod(1 + sample.cash) - 1)
    shrinkage = pf.ledoit_wolf(excess)[1] if method == pf.SHRUNK else None
    return FrontierReport(
        sample, method, shrinkage, assets, portfolios, curve, _annual_growth(cash_total, span), draws,
        [sample.codes[i] for i in pf.hrp_order(cov)],
    )


# --------------------------------------------------------------------------- out of sample


@dataclass(frozen=True)
class WalkLine:
    key: str
    grew: float
    """Return a year over the test (compounded)."""
    excess: float
    volatility: float
    sharpe: float | None
    drawdown: float
    turnover: float
    p_value: float | None
    """That its Sharpe ratio equals that of equal weights."""
    ahead: list[float]
    """Its growth of 1 over that of equal weights, minus 1, at each day of the test."""
    weights: list[float]
    fallbacks: int = 0


@dataclass(frozen=True)
class WalkReport:
    sample: Sample
    method: str
    window: int
    step: int
    days: list[date]
    """The days of the test, the first being its start."""
    lines: list[WalkLine]
    cash_grew: float

    @property
    def tested(self) -> int:
        return len(self.days) - 1


def walk_report(
    session: Session,
    *,
    start: date | None = None,
    end: date | None = None,
    paths: list[str] | None = None,
    frequency: str = WEEKLY,
    risk_free: float | None = None,
    method: str = pf.SHRUNK,
    window: int | None = None,
    step: int | None = None,
    market: MarketData | None = None,
) -> WalkReport:
    if method not in pf.COVARIANCES:
        raise FinanceError(f"The covariance is one of: {', '.join(pf.COVARIANCES)}.")
    sample = build_sample(session, start=start, end=end, paths=paths, frequency=frequency, risk_free=risk_free, market=market)
    t, n = sample.returns.shape
    window = window or min(WINDOWS[frequency], t // 2)
    step = step or STEPS[frequency]
    if window < max(n + 2, 10):
        raise FinanceError(f"The window must be at least {max(n + 2, 10)} periods for {n} assets.")
    if step < 1:
        raise FinanceError("The portfolios are chosen again every 1 or more periods.")
    if t - window < 10:
        raise FinanceError(
            f"{t} {frequency} returns with a window of {window} leave {max(t - window, 0)} to test on; at least 10 are "
            "needed. Get older prices with 'finance price update --start YYYY-MM-DD', or shorten the window."
        )
    periods = sample.periods
    cash = sample.cash[window:]
    choosers = {
        NOW: lambda s: sample.weights,
        pf.EQUAL: lambda s: pf.equal_weights(n),
        pf.MIN_VARIANCE: lambda s: pf.choose(pf.MIN_VARIANCE, s, method),
        pf.MAX_SHARPE: lambda s: pf.choose(pf.MAX_SHARPE, s, method),
        pf.HRP: lambda s: pf.choose(pf.HRP, s, method),
    }
    walks = _settled(lambda: {
        key: pf.walk_forward(sample.returns, sample.cash, window, step, chooser, lambda s: pf.choose(pf.MIN_VARIANCE, s, method))
        for key, chooser in choosers.items()
    })
    days = sample.days[window:]
    span = (days[-1] - days[0]).days
    base = walks[pf.EQUAL].returns
    base_growth = np.cumprod(1 + base)
    lines = []
    for key in ORDER:
        walk = walks[key]
        excess = walk.returns - cash
        growth = np.cumprod(1 + walk.returns)
        lines.append(WalkLine(
            key, _annual_growth(float(growth[-1] - 1), span), float(excess.mean() * periods),
            float(excess.std(ddof=1) * math.sqrt(periods)), pf.sharpe(excess, periods), pf.max_drawdown(walk.returns),
            walk.turnover, None if key == pf.EQUAL else pf.sharpe_difference_p(excess, base - cash),
            [0.0, *(float(x) for x in growth / base_growth - 1)], [float(x) for x in walk.weights], walk.fallbacks,
        ))
    return WalkReport(sample, method, window, step, days, lines, _annual_growth(float(np.prod(1 + cash) - 1), span))


# --------------------------------------------------------------------------- cash as a benchmark of the returns


@dataclass(frozen=True)
class CashBenchmark:
    total: float
    """What the lira earned at the policy rate from the day before the start to the end."""
    year: float | None
    source: str


def cash_benchmark(rates: RateSteps, source: str, start: date, end: date) -> CashBenchmark | None:
    first = start - timedelta(days=1)
    if not rates or rates.first > first or end <= first:
        return None
    total = rates.growth(first, end) - 1
    return CashBenchmark(total, _annual_growth(total, (end - first).days), source)

