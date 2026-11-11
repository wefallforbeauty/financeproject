"""Portfolio theory (05): the cash index, the inputs, the optimizer and the portfolios, resampling, out of sample.

The optimizer is checked by the conditions an optimum must meet (Karush–Kuhn–
Tucker), against closed forms and against a search over a grid. The shrunk
covariance is checked against the formulas of the paper written out pair by
pair and against a sample worked with PyPortfolioOpt, and hierarchical risk
parity against a matrix worked through the code of López de Prado's paper with
scipy's ``linkage`` (the figures were computed once with PyPortfolioOpt 1.6
and scipy 1.18, which the program does not need).
"""

from __future__ import annotations

import math
from datetime import date

import numpy as np
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from financeproject.core import portfolio as pf
from financeproject.core.rates import RateSteps, constant, effective_annual

FIXED = np.array([
    [0.040, 0.018, 0.002, 0.001, 0.000],
    [0.018, 0.090, 0.003, 0.002, 0.001],
    [0.002, 0.003, 0.010, 0.006, 0.001],
    [0.001, 0.002, 0.006, 0.020, 0.002],
    [0.000, 0.001, 0.001, 0.002, 0.0025],
])


def random_returns(seed: int, t: int = 120, n: int = 5) -> np.ndarray:
    rng = np.random.default_rng(seed)
    common = rng.normal(size=(t, 2)) @ rng.normal(size=(2, n)) * 0.01
    return common + rng.normal(size=(t, n)) * rng.uniform(0.005, 0.04, n) + rng.normal(0.002, 0.003, n)


# --------------------------------------------------------------------------- the cash index


def test_cash_index_compounds_daily_and_follows_rate_changes():
    steps = RateSteps({date(2026, 1, 1): 36.5, date(2026, 1, 11): 73.0})
    assert steps.at(date(2025, 12, 31)) is None and steps.at(date(2026, 1, 10)) == 36.5 and steps.at(date(2026, 3, 1)) == 73.0
    assert steps.growth(date(2026, 1, 5), date(2026, 1, 5)) == 1.0
    assert steps.growth(date(2026, 1, 5), date(2026, 1, 8)) == pytest.approx(1.001**3)
    # the 11th already earns the new rate
    assert steps.growth(date(2026, 1, 8), date(2026, 1, 12)) == pytest.approx(1.001**2 * 1.002**2)
    weekly = steps.period_returns([date(2026, 1, 2), date(2026, 1, 9), date(2026, 1, 16)])
    assert weekly == pytest.approx([1.001**7 - 1, 1.001 * 1.002**6 - 1])
    with pytest.raises(ValueError):
        steps.growth(date(2025, 12, 1), date(2026, 1, 5))
    assert effective_annual(36.5) == pytest.approx(1.001**365 - 1)
    assert constant(40.0).growth(date(2026, 1, 1), date(2027, 1, 1)) == pytest.approx(1.40)
    assert not RateSteps({}) and RateSteps({}).last() is None and steps.last() == (date(2026, 1, 11), 73.0)


# --------------------------------------------------------------------------- inputs


def honey(x: np.ndarray) -> tuple[np.ndarray, float]:
    """The shrunk covariance as the paper's appendix writes it, one pair of assets at a time."""
    t, n = x.shape
    y = x - x.mean(axis=0)
    s = y.T @ y / t
    pairs = [(i, j) for i in range(n) for j in range(n) if i != j]
    r_bar = sum(s[i, j] / math.sqrt(s[i, i] * s[j, j]) for i, j in pairs) / (n * (n - 1))
    f = np.array([[s[i, i] if i == j else r_bar * math.sqrt(s[i, i] * s[j, j]) for j in range(n)] for i in range(n)])
    pi = sum(float(np.mean((y[:, i] * y[:, j] - s[i, j]) ** 2)) for i in range(n) for j in range(n))
    rho = sum(float(np.mean((y[:, i] ** 2 - s[i, i]) ** 2)) for i in range(n))
    for i, j in pairs:
        both = y[:, i] * y[:, j] - s[i, j]
        theta_i = float(np.mean((y[:, i] ** 2 - s[i, i]) * both))
        theta_j = float(np.mean((y[:, j] ** 2 - s[j, j]) * both))
        rho += r_bar / 2 * (math.sqrt(s[j, j] / s[i, i]) * theta_i + math.sqrt(s[i, i] / s[j, j]) * theta_j)
    gamma = float(((f - s) ** 2).sum())
    delta = max(0.0, min(1.0, (pi - rho) / gamma / t))
    return delta * f + (1 - delta) * s, delta


def test_shrinkage_on_a_sample_worked_with_another_program():
    # 20 periods of 4 assets made by arithmetic; the figures are PyPortfolioOpt 1.6.0's
    # (constant correlation target, the sample covariance with divisor T)
    x = np.array([[((7 * t * t + 3 * t * (i + 1) + 11 * i) % 13 - 6) * (i + 1) / 100 for i in range(4)] for t in range(20)])
    cov, delta = pf.ledoit_wolf(x)
    assert delta == pytest.approx(0.3511916828928977, abs=1e-12)
    assert np.round(cov * 1e4, 4).tolist() == [
        [11.54, -5.4954, -9.004, -5.8836], [-5.4954, 82.75, 10.2695, -22.6301],
        [-9.004, 10.2695, 102.4875, -64.17], [-5.8836, -22.6301, -64.17, 170.36],
    ]


@settings(max_examples=60, deadline=None)
@given(st.integers(0, 10**6), st.integers(3, 8))
def test_shrinkage_follows_the_papers_formulas(seed, n):
    x = random_returns(seed, t=n + 4 + seed % 90, n=n)
    cov, delta = pf.ledoit_wolf(x)
    expected, expected_delta = honey(x)
    assert delta == pytest.approx(expected_delta, abs=1e-10) and cov == pytest.approx(expected, rel=1e-9, abs=1e-15)


def test_shrinkage_keeps_the_variances_and_evens_out_the_correlations():
    x = random_returns(3, t=40, n=8) * np.array([1, 1, 1, 1, 1, 1, 0.05, 30])  # a calm asset and a wild one
    cov, delta = pf.ledoit_wolf(x)
    s = np.cov(x, rowvar=False, ddof=0)
    assert 0 < delta < 1 and np.diag(cov) == pytest.approx(np.diag(s))  # every asset keeps its own risk
    sd = np.sqrt(np.diag(s))
    off = ~np.eye(8, dtype=bool)
    before, after = (s / np.outer(sd, sd))[off], (cov / np.outer(sd, sd))[off]
    assert after.mean() == pytest.approx(before.mean()) and after.std() == pytest.approx((1 - delta) * before.std())
    assert np.linalg.eigvalsh(cov).min() > 0
    assert pf.covariance(x, pf.SAMPLE) == pytest.approx(np.cov(x, rowvar=False))
    # nothing to shrink: two assets have one correlation; no variation at all; one asset
    two, d2 = pf.ledoit_wolf(x[:, :2])
    assert d2 == 0.0 and two == pytest.approx(s[:2, :2])
    assert pf.ledoit_wolf(np.tile(x[:1], (5, 1)))[1] == 0.0 and pf.ledoit_wolf(x[:, :1])[1] == 0.0
    with pytest.raises(ValueError):
        pf.covariance(x, "magic")


# --------------------------------------------------------------------------- the optimizer


def kkt_gap(q, a, c, x) -> float:
    """How far ``x`` is from the optimum conditions of min ½x'Qx − c'x, a'x = 1, x ≥ 0 (0 = optimal)."""
    scale = np.trace(q) / len(a)
    g = (q @ x - c) / scale
    free = x > 1e-10
    nu = float(g[free] @ a[free] / (a[free] @ a[free]))
    stationarity = np.abs(g[free] - nu * a[free]).max()
    dual = max(0.0, float(-(g[~free] - nu * a[~free]).min())) if (~free).any() else 0.0
    return max(stationarity, dual, abs(a @ x - 1), float(-x.min()))


@settings(max_examples=150, deadline=None)
@given(st.integers(0, 10**6), st.integers(2, 9), st.booleans())
def test_the_optimizer_meets_the_optimum_conditions(seed, n, with_mean):
    rng = np.random.default_rng(seed)
    x = random_returns(seed, t=n + 3 + int(rng.integers(0, 60)), n=n)
    if rng.random() < 0.2:
        x[:, -1] = x[:, 0]  # two identical assets
    cov, mean = pf.sample_covariance(x), x.mean(axis=0)
    ones = np.ones(n)
    c = float(rng.uniform(0, 3)) * mean if with_mean else np.zeros(n)
    w = pf.solve_qp(cov, ones, c)
    assert kkt_gap(cov, ones, c, w) < 1e-7
    best = pf.max_sharpe(mean, cov)
    if best is None:
        assert mean.max() <= 0
    else:
        ratio = mean @ best / np.sqrt(best @ cov @ best)
        for other in (pf.equal_weights(n), pf.min_variance(cov), *np.eye(n)):
            assert ratio >= mean @ other / np.sqrt(other @ cov @ other) - 1e-9


def test_closed_forms_when_no_bound_binds():
    cov = np.array([[0.04, 0.006, 0.0], [0.006, 0.09, 0.012], [0.0, 0.012, 0.0625]])
    inverse = np.linalg.inv(cov)
    ones = np.ones(3)
    assert pf.min_variance(cov) == pytest.approx(inverse @ ones / (ones @ inverse @ ones))
    mean = np.array([0.06, 0.10, 0.08])
    assert pf.max_sharpe(mean, cov) == pytest.approx(inverse @ mean / (ones @ inverse @ mean))
    # two assets: the textbook formula
    two = cov[:2, :2]
    w0 = (two[1, 1] - two[0, 1]) / (two[0, 0] + two[1, 1] - 2 * two[0, 1])
    assert pf.min_variance(two) == pytest.approx([w0, 1 - w0])


def test_bounds_bind_and_a_grid_finds_nothing_better():
    cov = np.array([[0.04, 0.038, 0.0], [0.038, 0.05, 0.0], [0.0, 0.0, 0.09]])
    mean = np.array([0.02, 0.10, 0.05])
    least, best = pf.min_variance(cov), pf.max_sharpe(mean, cov)
    assert best[0] == 0.0  # the first asset is the second with a lower return: left out
    grid = [np.array([i, j, 100 - i - j]) / 100 for i in range(101) for j in range(101 - i)]
    assert min(w @ cov @ w for w in grid) >= least @ cov @ least - 1e-12
    ratio = mean @ best / np.sqrt(best @ cov @ best)
    assert max(mean @ w / np.sqrt(w @ cov @ w) for w in grid) <= ratio + 1e-12
    assert pf.max_sharpe(-mean, cov) is None
    with pytest.raises(ValueError):
        pf.solve_qp(cov, -np.ones(3))
    with pytest.raises(ValueError):
        pf.solve_qp(np.zeros((2, 2)), np.ones(2))


def test_the_frontier_runs_from_least_risk_to_the_highest_mean():
    x = random_returns(11)
    cov, mean = pf.sample_covariance(x), x.mean(axis=0)
    points = pf.frontier(mean, cov, 40)
    means = [float(mean @ w) for w in points]
    risks = [float(w @ cov @ w) for w in points]
    assert points[0] == pytest.approx(pf.min_variance(cov))
    assert points[-1] == pytest.approx(np.eye(5)[int(np.argmax(mean))], abs=1e-7)
    assert all(b >= a - 1e-12 for a, b in zip(means, means[1:])) and all(b >= a - 1e-15 for a, b in zip(risks, risks[1:]))
    assert len(points) > 10 and all(abs(w.sum() - 1) < 1e-9 and w.min() >= 0 for w in points)
    # the best ratio lies on it: no frontier point has a higher one, and it is close to one of them
    best = pf.max_sharpe(mean, cov)
    ratios = [m / np.sqrt(r) for m, r in zip(means, risks)]
    assert max(ratios) <= mean @ best / np.sqrt(best @ cov @ best) + 1e-12
    assert len(pf.frontier(np.full(5, 0.01), cov)) == 1  # equal means: only least risk


# --------------------------------------------------------------------------- hierarchical risk parity


def test_hrp_on_a_matrix_worked_with_the_papers_code():
    assert pf.hrp_order(FIXED) == [0, 1, 4, 2, 3]
    assert np.round(pf.hrp(FIXED), 6).tolist() == [0.046489, 0.020662, 0.131387, 0.065694, 0.735768]


def test_hrp_by_hand_and_its_tree():
    # two pairs of twins: the tree puts each pair together, and the halves get inverse-variance shares
    cov = np.array([[0.04, 0.036, 0.0, 0.0], [0.036, 0.04, 0.0, 0.0], [0.0, 0.0, 0.01, 0.009], [0.0, 0.0, 0.009, 0.01]])
    order = pf.hrp_order(cov)
    assert sorted(order[:2]) in ([0, 1], [2, 3]) and sorted(order[2:]) in ([0, 1], [2, 3])
    v_a, v_b = 0.038, 0.0095  # the variance of each pair held half and half
    w = pf.hrp(cov)
    assert w == pytest.approx([0.5 * v_b / (v_a + v_b)] * 2 + [0.5 * v_a / (v_a + v_b)] * 2)
    assert pf.hrp(np.diag([0.04, 0.01])) == pytest.approx([0.2, 0.8])  # two assets: inverse variance
    # smaller cluster number on the left, as scipy writes it: items 1 and 2 merge first, then 0 joins
    d = np.array([[0.0, 5.0, 4.0], [5.0, 0.0, 1.0], [4.0, 1.0, 0.0]])
    assert pf.single_linkage_order(d) == [0, 1, 2] and pf.single_linkage_order(np.zeros((1, 1))) == [0]


@settings(max_examples=50, deadline=None)
@given(st.integers(0, 10**6), st.integers(2, 12))
def test_hrp_weights_are_a_portfolio(seed, n):
    w = pf.hrp(pf.sample_covariance(random_returns(seed, t=n + 20, n=n)))
    assert w.sum() == pytest.approx(1) and w.min() > 0


# --------------------------------------------------------------------------- resampling and out of sample


def test_resampling_shows_how_unsure_the_best_ratio_is():
    x = random_returns(5, t=104)
    found = pf.resample(x, pf.STRATEGIES, draws=60)
    again = pf.resample(x, pf.STRATEGIES, draws=60)
    assert all(np.array_equal(found[s].mean, again[s].mean) for s in found)  # the same draws every time
    equal, least, best = found[pf.EQUAL], found[pf.MIN_VARIANCE], found[pf.MAX_SHARPE]
    assert equal.low == pytest.approx(equal.high) and equal.draws == 60
    assert (best.high - best.low).mean() > (least.high - least.low).mean() > 0  # means are noisier than variances
    for r in found.values():
        assert r.mean.sum() == pytest.approx(1) and (r.low <= r.mean + 1e-12).all() and (r.mean <= r.high + 1e-12).all()
    assert pf.MAX_SHARPE not in pf.resample(-np.abs(x), [pf.MAX_SHARPE], draws=5)


def test_walking_forward():
    x = random_returns(8, t=60, n=3)
    rf = np.full(60, 0.001)
    fixed = np.array([0.5, 0.3, 0.2])
    walk = pf.walk_forward(x, rf, 20, 1, lambda sample: fixed)
    assert walk.returns == pytest.approx(x[20:] @ fixed) and len(walk.returns) == 40
    # rebalanced every period, the trade undoes one period's drift
    drift = fixed * (1 + x[20]) / (fixed @ (1 + x[20]))
    monthly = pf.walk_forward(x, rf, 20, 4, lambda sample: fixed)
    assert monthly.returns[0] == pytest.approx(x[20] @ fixed) and monthly.returns[1] == pytest.approx(x[21] @ drift)
    assert 0 < monthly.turnover < 0.2 and walk.turnover < monthly.turnover
    # the chooser sees only the window before the day
    seen = []
    pf.walk_forward(x, rf, 20, 10, lambda sample: seen.append(sample.copy()) or fixed)
    assert len(seen) == 4 and seen[1] == pytest.approx(x[10:30] - 0.001)
    none = pf.walk_forward(x, rf, 20, 10, lambda sample: None, lambda sample: fixed)
    assert none.fallbacks == 4 and none.weights == pytest.approx(fixed)
    with pytest.raises(ValueError):
        pf.walk_forward(x, rf, 60, 1, lambda sample: fixed)
    for s in pf.STRATEGIES:
        assert pf.choose(s, x - 0.001) is None or pf.choose(s, x - 0.001).sum() == pytest.approx(1)
    with pytest.raises(ValueError):
        pf.choose("lucky", x)


def test_sharpe_ratio_its_test_and_the_largest_fall():
    rng = np.random.default_rng(2)
    a = rng.normal(0.002, 0.02, 400)
    assert pf.sharpe(a, 52) == pytest.approx(a.mean() / a.std(ddof=1) * 52**0.5)
    assert pf.sharpe(np.zeros(5), 52) is None and pf.sharpe(a[:1], 52) is None
    assert pf.sharpe_difference_p(a, a * 3) == pytest.approx(1.0)  # leverage does not change the ratio
    noisy = a + rng.normal(0, 0.02, 400)
    assert 0.0 < pf.sharpe_difference_p(a, noisy) < 1.0
    better = np.abs(a) + 0.01
    assert pf.sharpe_difference_p(better, a) < 0.001 and pf.sharpe_difference_p(a, better) == pytest.approx(pf.sharpe_difference_p(better, a))
    assert pf.sharpe_difference_p(a, a[:10]) is None and pf.sharpe_difference_p(np.zeros(9), a[:9]) is None
    # under equal ratios the p-value is about uniform: roughly 5 % of samples fall under 0.05
    hits = 0
    for _ in range(400):
        u, v = rng.normal(0.001, 0.02, 150), rng.normal(0.0015, 0.03, 150)
        hits += pf.sharpe_difference_p(u, v) < 0.05
    assert 5 <= hits <= 40
    assert pf.max_drawdown(np.array([0.1, -0.5, 0.2, 1.0, -0.1])) == pytest.approx(0.5)
    assert pf.max_drawdown(np.array([0.1, 0.2])) == 0.0


def test_an_asset_that_does_not_move_is_the_least_risky_not_an_error():
    x = random_returns(6, t=30, n=4)
    x[:, 2] = 0.001  # a price that stood still while cash earned the same every week
    cov = pf.sample_covariance(x)
    for weights in (pf.hrp(cov), pf.min_variance(cov)):
        assert np.isfinite(weights).all() and weights.sum() == pytest.approx(1) and weights[2] > 0.99
    assert sorted(pf.hrp_order(cov)) == [0, 1, 2, 3]
    walk = pf.walk_forward(x, np.zeros(30), 12, 3, lambda sample: pf.choose(pf.HRP, sample, pf.SAMPLE))
    assert np.isfinite(walk.returns).all()
