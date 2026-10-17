"""Risk of a series of returns: volatility, drawdowns, value at risk and its backtest, correlation.

- **Volatility**: the sample standard deviation of daily log returns, times
  √252 for a year (about 252 trading days a year in Turkey).
- **Maximum drawdown**: the largest fall of the growth index from a previous
  peak, 1 − I_t / max_{s≤t} I_s, with the day of the peak, the bottom, and
  the day the index got back to the peak (if it did).
- **Value at risk** at level α (95 %, 99 %): the loss of one day that is
  exceeded with probability 1 − α. Historical: sorted returns r_(1) ≤ … ≤
  r_(n), k = ⌈n(1 − α)⌉, VaR = −r_(k), and the expected shortfall (CVaR) is
  the mean loss of those k worst days. Normal: VaR = −(μ − z_α σ), CVaR =
  −(μ − σ φ(z_α) / (1 − α)).
- **Backtest**: each day's historical VaR from the previous ``window`` days
  is compared with that day's return. Kupiec's test (1995) asks whether the
  share of exceptions matches 1 − α (LR_POF, χ² with 1 degree of freedom);
  Christoffersen's (1998) whether exceptions cluster (LR_ind, χ² 1) and both
  together (LR_cc = LR_POF + LR_ind, χ² 2).
- **Correlation**: Pearson's, between daily log returns on common days.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from statistics import NormalDist, fmean, stdev

TRADING_DAYS = 252
LEVELS = (0.95, 0.99)


def log_returns(returns: Sequence[float]) -> list[float]:
    return [math.log1p(r) for r in returns]


def volatility(returns: Sequence[float], periods: int = TRADING_DAYS) -> float | None:
    """Yearly volatility of simple daily returns (from their logs)."""
    if len(returns) < 2:
        return None
    return stdev(log_returns(returns)) * math.sqrt(periods)


def growth_index(returns: Sequence[tuple[date, float]], start: date, base: float = 1.0) -> list[tuple[date, float]]:
    """The value of ``base`` invested at ``start`` and grown by the returns."""
    out = [(start, base)]
    for on, r in returns:
        out.append((on, out[-1][1] * (1 + r)))
    return out


@dataclass(frozen=True)
class Drawdown:
    depth: float
    """The fall from the peak, as a positive fraction."""
    peak: date
    bottom: date
    recovered: date | None
    """The first day back at the peak; ``None`` if not yet."""

    def days(self, until: date) -> int:
        """Days from the peak to the recovery, or to ``until`` while it lasts."""
        return ((self.recovered or until) - self.peak).days


def max_drawdown(index: Sequence[tuple[date, float]]) -> Drawdown | None:
    """The largest fall of the index from a previous peak (``None`` if it never fell)."""
    if not index:
        return None
    peak_day, peak = index[0]
    worst: tuple[float, date, date, float] | None = None  # depth, peak day, bottom day, peak level
    for on, level in index:
        if level > peak:
            peak_day, peak = on, level
        depth = 1 - level / peak
        if depth > 0 and (worst is None or depth > worst[0]):
            worst = (depth, peak_day, on, peak)
    if worst is None:
        return None
    depth, peak_day, bottom, peak = worst
    recovered = next((on for on, level in index if on > bottom and level >= peak), None)
    return Drawdown(depth, peak_day, bottom, recovered)


@dataclass(frozen=True)
class ValueAtRisk:
    level: float
    var: float
    """Loss of one day, as a positive fraction of the value."""
    cvar: float
    """Mean loss on the days beyond the VaR (expected shortfall)."""


def historical_var(returns: Sequence[float], level: float) -> ValueAtRisk | None:
    """Value at risk and expected shortfall from the returns as they were."""
    n = len(returns)
    if n == 0:
        return None
    k = max(1, math.ceil(n * (1 - level) - 1e-9))
    worst = sorted(returns)[:k]
    return ValueAtRisk(level, -worst[-1], -fmean(worst))


def normal_var(returns: Sequence[float], level: float) -> ValueAtRisk | None:
    """Value at risk and expected shortfall of a normal distribution with the returns' mean and spread."""
    if len(returns) < 2:
        return None
    mu, sigma = fmean(returns), stdev(returns)
    z = NormalDist().inv_cdf(level)
    return ValueAtRisk(level, -(mu - z * sigma), -(mu - sigma * NormalDist().pdf(z) / (1 - level)))


def _xlogy(x: float, y: float) -> float:
    """x · ln y with 0 · ln 0 = 0."""
    return 0.0 if x == 0 else x * math.log(y)


def kupiec(exceptions: int, n: int, level: float) -> tuple[float, float]:
    """Kupiec's proportion-of-failures test: LR_POF and its p-value (χ², 1 degree of freedom)."""
    p = 1 - level
    observed = exceptions / n
    lr = -2 * (
        _xlogy(n - exceptions, 1 - p) + _xlogy(exceptions, p)
        - _xlogy(n - exceptions, 1 - observed) - _xlogy(exceptions, observed)
    )
    lr = max(lr, 0.0)
    return lr, math.erfc(math.sqrt(lr / 2))


def christoffersen(hits: Sequence[bool]) -> tuple[float, float] | None:
    """Christoffersen's independence test: LR_ind and its p-value (χ², 1 degree of freedom).

    ``None`` when there is no exception or nothing but exceptions (the test needs both).
    """
    counts = {(a, b): 0 for a in (False, True) for b in (False, True)}
    for a, b in zip(hits, hits[1:]):
        counts[(a, b)] += 1
    n00, n01, n10, n11 = counts[(False, False)], counts[(False, True)], counts[(True, False)], counts[(True, True)]
    if n01 + n11 == 0 or n00 + n10 == 0:
        return None
    pi = (n01 + n11) / (n00 + n01 + n10 + n11)
    pi0 = n01 / (n00 + n01) if n00 + n01 else 0.0
    pi1 = n11 / (n10 + n11) if n10 + n11 else 0.0
    restricted = _xlogy(n00 + n10, 1 - pi) + _xlogy(n01 + n11, pi)
    unrestricted = _xlogy(n00, 1 - pi0) + _xlogy(n01, pi0) + _xlogy(n10, 1 - pi1) + _xlogy(n11, pi1)
    lr = max(-2 * (restricted - unrestricted), 0.0)
    return lr, math.erfc(math.sqrt(lr / 2))


@dataclass(frozen=True)
class Backtest:
    level: float
    window: int
    days: int
    """Days tested (each with a VaR from the ``window`` days before it)."""
    exceptions: int
    """Days whose loss was larger than the VaR."""
    kupiec_p: float
    independence_p: float | None
    combined_p: float | None
    """Christoffersen's conditional coverage: right share and no clustering."""

    @property
    def expected(self) -> float:
        return self.days * (1 - self.level)


def backtest(returns: Sequence[float], level: float, window: int = 250) -> Backtest | None:
    """Historical VaR of every day from the ``window`` days before it, against that day's return."""
    if len(returns) <= window:
        return None
    hits = []
    for t in range(window, len(returns)):
        var = historical_var(returns[t - window : t], level).var
        hits.append(-returns[t] > var)
    exceptions = sum(hits)
    lr_pof, p_pof = kupiec(exceptions, len(hits), level)
    independence = christoffersen(hits)
    if independence is None:
        p_ind = p_cc = None
    else:
        lr_ind, p_ind = independence
        p_cc = math.exp(-(lr_pof + lr_ind) / 2)
    return Backtest(level, window, len(hits), exceptions, p_pof, p_ind, p_cc)


def correlation(a: Sequence[float], b: Sequence[float]) -> float | None:
    """Pearson's correlation of two equally long series (``None`` if one does not vary)."""
    n = len(a)
    if n < 3 or n != len(b):
        return None
    ma, mb = fmean(a), fmean(b)
    sa = math.sqrt(sum((x - ma) ** 2 for x in a))
    sb = math.sqrt(sum((y - mb) ** 2 for y in b))
    if sa == 0 or sb == 0:
        return None
    return sum((x - ma) * (y - mb) for x, y in zip(a, b)) / (sa * sb)

