"""Portfolio theory on a matrix of returns: what the efficient portfolios are, and how far to trust them.

Returns are a ``T × N`` array: ``T`` periods (weeks, by default) of ``N``
assets, in excess of the risk-free return of each period. All portfolios are
fully invested and long-only (weights ≥ 0, summing to 1): a household does not
sell short.

- **Inputs.** The mean vector and the covariance matrix of the sample, or the
  covariance shrunk towards equal correlations (Ledoit & Wolf 2004):
  ``Σ* = (1 − δ) S + δ F``, where ``F`` has the sample's variances and its
  average correlation for every pair, and the intensity ``δ`` minimizes the
  expected squared error, estimated from the data.
- **Least risk** (the global minimum variance portfolio): ``min w'Σw``.
- **Best ratio** (the tangency portfolio, Sharpe 1966): ``max μ'w / √(w'Σw)``.
  The ratio does not change when ``w`` is scaled, so it is found as
  ``min y'Σy`` with ``μ'y = 1, y ≥ 0`` and ``w = y / Σy``.
- **Efficient frontier** (Markowitz 1952): ``min ½ w'Σw − λ μ'w`` for λ from 0
  (least risk) up to where everything is in the asset with the highest mean.
- **Equal weights**, 1/N (DeMiguel, Garlappi & Uppal 2009).
- **Hierarchical risk parity** (López de Prado 2016): the assets are ordered
  by a single-linkage tree on their correlation distances, and the money is
  split down the ordered list in halves, each time in inverse proportion to
  the halves' variances. It needs no means and no matrix inversion.

The quadratic programs are solved by a primal active-set method (Nocedal &
Wright 2006, algorithm 16.3): exact for these small convex problems, with no
dependency beyond numpy.

- **Resampling** (Michaud 1989; Michaud & Michaud 2008): the periods are drawn
  again with replacement, the inputs estimated and the portfolios computed
  again, many times. The spread of the weights shows how much of a portfolio
  is the sample's noise.
- **Out of sample** (DeMiguel et al. 2009): each portfolio is chosen from the
  ``window`` periods before a day, held for ``step`` periods, and chosen
  again. The returns it earned afterwards are what its Sharpe ratio is
  computed from, and tested against equal weights (Jobson & Korkie 1981 with
  Memmel's 2003 correction).
"""

from __future__ import annotations

import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass

import numpy as np

SAMPLE, SHRUNK = "sample", "shrunk"
COVARIANCES = (SHRUNK, SAMPLE)
EQUAL, MIN_VARIANCE, MAX_SHARPE, HRP = "equal", "min_variance", "max_sharpe", "hrp"
STRATEGIES = (EQUAL, MIN_VARIANCE, MAX_SHARPE, HRP)


class NotConverged(ArithmeticError):
    """The optimizer did not reach an optimum (not seen in practice; a guard)."""


# --------------------------------------------------------------------------- inputs


def sample_covariance(returns: np.ndarray) -> np.ndarray:
    """The unbiased sample covariance of the columns."""
    x = returns - returns.mean(axis=0)
    return x.T @ x / (len(returns) - 1)


def _variances(cov: np.ndarray) -> np.ndarray:
    """The diagonal, kept above zero: an asset that did not move at all in a sample is the least risky
    one, not a division by zero."""
    diagonal = np.diag(cov)
    return np.maximum(diagonal, max(1e-12 * float(diagonal.mean()), 1e-300))


def ledoit_wolf(returns: np.ndarray) -> tuple[np.ndarray, float]:
    """The covariance shrunk towards equal correlations, and the shrinkage intensity δ in [0, 1].

    The target ``F`` keeps every variance of the sample ``S`` and gives every pair the average
    correlation ``r̄``: ``f_ii = s_ii``, ``f_ij = r̄ √(s_ii s_jj)``. The estimate is
    ``δF + (1 − δ)S`` with ``δ = max(0, min(1, κ/T))`` and ``κ = (π − ρ)/γ`` (Ledoit & Wolf 2004,
    "Honey, I shrunk the sample covariance matrix", appendix B), where, with ``S`` and every
    average over the periods taken with divisor ``T``:

    - ``π = Σ_ij π_ij``, ``π_ij = avg_t (x_ti x_tj − s_ij)²``: the sampling error of ``S``;
    - ``ρ = Σ_i π_ii + r̄ Σ_{i≠j} √(s_jj/s_ii) ϑ_ij``, ``ϑ_ij = avg_t (x_ti² − s_ii)(x_ti x_tj − s_ij)``:
      how the errors of the target and of the sample move together;
    - ``γ = Σ_ij (f_ij − s_ij)²``: how far the target is from the sample.

    A target of equal variances (the same authors' other estimator) would not do here: a household's
    holdings differ a hundredfold in variance, and it would make the calmest one look risky.
    """
    t, n = returns.shape
    x = returns - returns.mean(axis=0)
    s = x.T @ x / t
    if n < 2:
        return s, 0.0
    sd = np.sqrt(_variances(s))
    scale = np.outer(sd, sd)
    r_bar = (float((s / scale).sum()) - n) / (n * (n - 1))
    target = r_bar * scale
    np.fill_diagonal(target, np.diag(s))
    gamma = float(((target - s) ** 2).sum())
    if gamma <= 1e-30 * float(np.trace(s)) ** 2:
        return s, 0.0  # two assets, or all correlations equal already: nothing to shrink
    x2 = x**2
    pi = x2.T @ x2 / t - s**2
    theta = (x**3).T @ x / t - np.diag(s)[:, None] * s
    np.fill_diagonal(theta, 0.0)
    rho = float(np.trace(pi)) + r_bar * float(((sd[None, :] / sd[:, None]) * theta).sum())
    delta = max(0.0, min(1.0, (float(pi.sum()) - rho) / gamma / t))
    return delta * target + (1 - delta) * s, float(delta)


def covariance(returns: np.ndarray, method: str = SHRUNK) -> np.ndarray:
    if method == SAMPLE:
        return sample_covariance(returns)
    if method == SHRUNK:
        return ledoit_wolf(returns)[0]
    raise ValueError(f"Unknown covariance estimator: {method}")


# --------------------------------------------------------------------------- the quadratic program


def solve_qp(q: np.ndarray, a: np.ndarray, c: np.ndarray | None = None, start: np.ndarray | None = None) -> np.ndarray:
    """``min ½ x'Qx − c'x`` subject to ``a'x = 1`` and ``x ≥ 0``, for a positive semi-definite ``Q``.

    Primal active-set method: the variables at zero form the working set; on
    the others the equality-constrained problem is solved exactly; a step
    that would make a variable negative stops there and fixes it at zero; at
    a solution of the working set, a variable whose multiplier is negative is
    set free. ``start`` is a feasible point to begin from.
    """
    n = len(a)
    scale = float(np.trace(q)) / n
    if scale <= 0:
        raise ValueError("The covariance matrix has no variance.")
    q = q / scale + 1e-12 * np.eye(n)  # the same minimizer; the ridge keeps equal assets apart
    c = np.zeros(n) if c is None else np.asarray(c, dtype=float) / scale
    a = np.asarray(a, dtype=float)
    if start is None:
        j = int(np.argmax(a))
        if a[j] <= 0:
            raise ValueError("No feasible point: a'x = 1 needs a positive coefficient.")
        x = np.zeros(n)
        x[j] = 1 / a[j]
    else:
        x = np.clip(np.asarray(start, dtype=float), 0, None)
        x = x / float(a @ x)
    fixed = x <= 0
    for _ in range(50 * n + 100):
        free = np.flatnonzero(~fixed)
        k = len(free)
        kkt = np.zeros((k + 1, k + 1))
        kkt[:k, :k] = q[np.ix_(free, free)]
        kkt[:k, k] = -a[free]
        kkt[k, :k] = a[free]
        rhs = np.append(c[free], 1.0)
        try:
            solution = np.linalg.solve(kkt, rhs)
        except np.linalg.LinAlgError:
            solution = np.linalg.lstsq(kkt, rhs, rcond=None)[0]
        target, nu = solution[:k], solution[k]
        step = target - x[free]
        size = max(1.0, float(np.abs(x).max()))
        if np.abs(step).max() <= 1e-11 * size:
            x[free] = np.clip(target, 0, None)
            multipliers = (q @ x - c - nu * a)[fixed]
            if multipliers.size == 0 or multipliers.min() >= -1e-9 * size:
                x[fixed] = 0.0
                return x
            fixed[np.flatnonzero(fixed)[int(np.argmin(multipliers))]] = False
            continue
        falling = step < -1e-300
        alpha, block = 1.0, None
        if falling.any():
            ratios = x[free][falling] / -step[falling]
            i = int(np.argmin(ratios))
            if ratios[i] < 1.0:
                alpha, block = float(max(ratios[i], 0.0)), free[np.flatnonzero(falling)[i]]
        x[free] = np.clip(x[free] + alpha * step, 0, None)
        if block is not None:
            x[block] = 0.0
            fixed[block] = True
    raise NotConverged("The optimizer did not converge.")


def _weights(x: np.ndarray) -> np.ndarray:
    x = np.where(x < 1e-12, 0.0, x)
    return x / x.sum()


# --------------------------------------------------------------------------- the portfolios


def equal_weights(n: int) -> np.ndarray:
    return np.full(n, 1 / n)


def min_variance(cov: np.ndarray) -> np.ndarray:
    return _weights(solve_qp(cov, np.ones(len(cov))))


def max_sharpe(mean_excess: np.ndarray, cov: np.ndarray) -> np.ndarray | None:
    """The long-only portfolio with the highest mean excess return per unit of risk
    (``None`` when no asset earned more than the risk-free rate)."""
    top = float(np.max(mean_excess))
    if top <= 0:
        return None
    return _weights(solve_qp(cov, np.asarray(mean_excess) / top))


def frontier(mean: np.ndarray, cov: np.ndarray, points: int = 60) -> list[np.ndarray]:
    """Portfolios along the long-only efficient frontier, from least risk to the highest mean."""
    n = len(mean)
    ones = np.ones(n)
    spread = float(np.max(mean) - np.min(mean))
    first = min_variance(cov)
    if spread <= 0 or n < 2:
        return [first]
    scale = float(np.trace(cov)) / n
    top = float(np.max(mean))

    def at(lam: float, start: np.ndarray) -> np.ndarray:
        return _weights(solve_qp(cov, ones, lam * mean, start))

    high, w = scale / spread, first
    for _ in range(80):
        w = at(high, w)
        if float(mean @ w) >= top - 1e-9 * spread:
            break
        high *= 2
    out, w = [first], first
    for i in range(1, points + 1):
        w = at(high * (i / points) ** 2, w)
        if np.abs(w - out[-1]).max() > 1e-9:
            out.append(w)
    return out


def single_linkage_order(distance: np.ndarray) -> list[int]:
    """The leaves of the single-linkage tree of a distance matrix, left to right.

    Clusters merge in order of distance; a merged cluster gets the next number
    after the items, and of two merging clusters the one with the smaller
    number goes left (as scipy's ``linkage`` writes its rows)."""
    n = len(distance)
    if n == 1:
        return [0]
    pairs = sorted((float(distance[i, j]), i, j) for i in range(n) for j in range(i + 1, n))
    cluster = list(range(n))  # the cluster each item is in
    children: dict[int, tuple[int, int]] = {}
    next_id = n
    for _, i, j in pairs:
        ci, cj = cluster[i], cluster[j]
        if ci == cj:
            continue
        children[next_id] = (min(ci, cj), max(ci, cj))
        cluster = [next_id if c in (ci, cj) else c for c in cluster]
        next_id += 1
        if next_id == 2 * n - 1:
            break

    def leaves(node: int) -> list[int]:
        if node < n:
            return [node]
        left, right = children[node]
        return leaves(left) + leaves(right)

    return leaves(next_id - 1)


def _cluster_variance(cov: np.ndarray, items: list[int]) -> float:
    sub = cov[np.ix_(items, items)]
    w = 1 / _variances(sub)
    w /= w.sum()
    return float(max(w @ sub @ w, 1e-300))


def hrp_order(cov: np.ndarray) -> list[int]:
    """The assets in the order of the tree: similar ones next to each other."""
    sd = np.sqrt(_variances(cov))
    corr = np.clip(cov / np.outer(sd, sd), -1, 1)
    dist = np.sqrt((1 - corr) / 2)
    between = np.sqrt(((dist[:, None, :] - dist[None, :, :]) ** 2).sum(axis=2))
    return single_linkage_order(between)


def hrp(cov: np.ndarray) -> np.ndarray:
    """Hierarchical risk parity weights (López de Prado 2016)."""
    n = len(cov)
    w = np.ones(n)
    clusters = [hrp_order(cov)]
    while clusters:
        clusters = [c[a:b] for c in clusters for a, b in ((0, len(c) // 2), (len(c) // 2, len(c))) if len(c) > 1]
        for i in range(0, len(clusters), 2):
            left, right = clusters[i], clusters[i + 1]
            v_left, v_right = _cluster_variance(cov, left), _cluster_variance(cov, right)
            alpha = 1 - v_left / (v_left + v_right)
            w[left] *= alpha
            w[right] *= 1 - alpha
    return w / w.sum()


def choose(strategy: str, excess: np.ndarray, method: str = SHRUNK) -> np.ndarray | None:
    """The weights of ``strategy`` from a sample of excess returns (``None``: no best ratio exists)."""
    n = excess.shape[1]
    if strategy == EQUAL:
        return equal_weights(n)
    cov = covariance(excess, method)
    if strategy == MIN_VARIANCE:
        return min_variance(cov)
    if strategy == MAX_SHARPE:
        return max_sharpe(excess.mean(axis=0), cov)
    if strategy == HRP:
        return hrp(cov)
    raise ValueError(f"Unknown strategy: {strategy}")


# --------------------------------------------------------------------------- resampling


@dataclass(frozen=True)
class Resampled:
    mean: np.ndarray
    """The average weights over the draws (Michaud's resampled portfolio)."""
    low: np.ndarray
    high: np.ndarray
    """The 5th and 95th percentile of each weight."""
    draws: int
    """Draws in which the portfolio existed."""


def resample(
    excess: np.ndarray, strategies: Sequence[str], method: str = SHRUNK, draws: int = 200, seed: int = 20260520
) -> dict[str, Resampled]:
    """Each strategy's weights over ``draws`` samples of the periods drawn with replacement."""
    rng = np.random.default_rng(seed)
    t = len(excess)
    found: dict[str, list[np.ndarray]] = {s: [] for s in strategies}
    for _ in range(draws):
        sample = excess[rng.integers(0, t, t)]
        for s in strategies:
            w = choose(s, sample, method)
            if w is not None:
                found[s].append(w)
    out = {}
    for s, rows in found.items():
        if rows:
            stack = np.vstack(rows)
            out[s] = Resampled(stack.mean(axis=0), np.percentile(stack, 5, axis=0), np.percentile(stack, 95, axis=0), len(rows))
    return out


# --------------------------------------------------------------------------- out of sample


@dataclass(frozen=True)
class Walk:
    returns: np.ndarray
    """The portfolio's return in each period after the first window."""
    turnover: float
    """The mean share of the portfolio sold and bought again at a rebalancing."""
    weights: np.ndarray
    """The weights chosen last."""
    fallbacks: int = 0
    """Rebalancings at which no best ratio existed and least risk was held instead."""


def walk_forward(
    returns: np.ndarray,
    risk_free: np.ndarray,
    window: int,
    step: int,
    chooser: Callable[[np.ndarray], np.ndarray | None],
    fallback: Callable[[np.ndarray], np.ndarray] | None = None,
) -> Walk:
    """Hold what ``chooser`` picks from the ``window`` periods before each rebalancing, for ``step`` periods.

    Between rebalancings the weights drift with the returns; the turnover is
    ``½ Σ|w_new − w_drifted|``.
    """
    t, n = returns.shape
    if t <= window:
        raise ValueError("The history is not longer than the window.")
    excess = returns - risk_free[:, None]
    out = np.empty(t - window)
    held: np.ndarray | None = None
    traded, fallbacks, target = [], 0, equal_weights(n)
    for start in range(window, t, step):
        sample = excess[start - window : start]
        chosen = chooser(sample)
        if chosen is None:
            fallbacks += 1
            chosen = fallback(sample) if fallback else equal_weights(n)
        target = chosen
        if held is not None:
            traded.append(0.5 * float(np.abs(target - held).sum()))
        held = target.copy()
        for k in range(start, min(start + step, t)):
            growth = held * (1 + returns[k])
            total = float(growth.sum())
            out[k - window] = total - 1
            held = growth / total
    return Walk(out, float(np.mean(traded)) if traded else 0.0, target, fallbacks)


def sharpe(excess: np.ndarray, periods: int) -> float | None:
    """The yearly Sharpe ratio of a series of excess returns."""
    if len(excess) < 2:
        return None
    sd = float(np.std(excess, ddof=1))
    return None if sd == 0 else float(np.mean(excess)) / sd * math.sqrt(periods)


def sharpe_difference_p(a: np.ndarray, b: np.ndarray) -> float | None:
    """Two-sided p-value that two series of excess returns have the same Sharpe ratio
    (Jobson & Korkie 1981, as corrected by Memmel 2003)."""
    t = len(a)
    if t < 3 or t != len(b):
        return None
    ma, mb = float(np.mean(a)), float(np.mean(b))
    sa, sb = float(np.std(a, ddof=1)), float(np.std(b, ddof=1))
    if sa == 0 or sb == 0:
        return None
    sab = float(np.cov(a, b, ddof=1)[0, 1])
    theta = (
        2 * sa**2 * sb**2 - 2 * sa * sb * sab + 0.5 * ma**2 * sb**2 + 0.5 * mb**2 * sa**2 - ma * mb / (sa * sb) * sab**2
    ) / t
    if theta <= 0:
        return None
    z = (sb * ma - sa * mb) / math.sqrt(theta)
    return math.erfc(abs(z) / math.sqrt(2))


def max_drawdown(returns: np.ndarray) -> float:
    """The largest fall of the growth of 1 from an earlier peak, as a positive fraction."""
    index = np.concatenate([[1.0], np.cumprod(1 + returns)])
    return float(np.max(1 - index / np.maximum.accumulate(index)))
