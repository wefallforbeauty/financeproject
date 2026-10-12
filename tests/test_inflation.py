"""CPI chaining, rebasing, splicing and the deflator, checked on the real Turkish series."""

import csv
import math
from datetime import date
from importlib import resources

import pytest

from financeproject.core.dates import add_months, parse_month
from financeproject.core.inflation import (
    Deflator,
    annual_pct,
    chain_index,
    fisher_real_rate,
    monthly_pct,
    rebase,
    splice,
)


def bundled_table():
    text = resources.files("financeproject.data").joinpath("cpi_tcmb_monthly.csv").read_text(encoding="utf-8")
    rows = list(csv.DictReader(text.splitlines()))
    monthly = {parse_month(r["month"]): float(r["monthly_change_pct"]) for r in rows}
    annual = {parse_month(r["month"]): float(r["annual_change_pct"]) for r in rows}
    return monthly, annual


def test_bundled_table_is_complete():
    monthly, _ = bundled_table()
    months = sorted(monthly)
    assert months[0] == date(2005, 1, 1)
    assert all(add_months(a, 1) == b for a, b in zip(months, months[1:]))


def test_chain_index_small_example():
    jan, feb = date(2026, 1, 1), date(2026, 2, 1)
    idx = chain_index({jan: 10.0, feb: 10.0}, base_year=None)
    assert idx[jan] == pytest.approx(110.0)
    assert idx[feb] == pytest.approx(121.0)
    assert monthly_pct(idx)[feb] == pytest.approx(10.0)


def test_chain_index_rejects_gaps():
    with pytest.raises(ValueError, match="Gap"):
        chain_index({date(2026, 1, 1): 1.0, date(2026, 3, 1): 1.0}, base_year=None)


def test_rebase_needs_whole_year():
    with pytest.raises(ValueError):
        rebase({date(2025, 1, 1): 100.0}, 2025)


def test_chained_tcmb_table_reproduces_published_annual_changes():
    """Chaining the rounded monthly changes stays within 0.05 points of every
    published annual change (249 months), and the 2025 average is 100."""
    monthly, annual = bundled_table()
    idx = chain_index(monthly, base_year=2025)
    assert sum(v for m, v in idx.items() if m.year == 2025) / 12 == pytest.approx(100.0)
    computed = annual_pct(idx)
    assert len(computed) == len(idx) - 12
    worst = max(abs(computed[m] - annual[m]) for m in computed)
    assert worst < 0.05
    # January 2026, first month published on the 2025=100 base: +4.84 % monthly, +30.65 % annual
    jan = date(2026, 1, 1)
    assert monthly_pct(idx)[jan] == pytest.approx(4.84, abs=1e-9)
    assert computed[jan] == pytest.approx(30.65, abs=0.02)


def test_splice_rescales_older_base_and_prefers_newer():
    months = [date(2025, m, 1) for m in range(1, 13)]
    old = {m: 50.0 + i for i, m in enumerate(months)}
    new = {m: 2 * v for m, v in old.items() if m.month >= 4}
    out = splice(old, new)
    assert out[date(2025, 1, 1)] == pytest.approx(100.0)
    assert out[date(2025, 12, 1)] == new[date(2025, 12, 1)]
    assert list(out) == sorted(out)
    with pytest.raises(ValueError):
        splice(old, {m: v for m, v in new.items() if m.month >= 10})


def test_deflator_monthly_method():
    sep, oct_ = date(2026, 9, 1), date(2026, 10, 1)
    d = Deflator({sep: 100.0, oct_: 110.0}, method="monthly")
    assert d.level(date(2026, 9, 30)) == 100.0
    assert d.factor(date(2026, 9, 5)) == pytest.approx(1.1)  # to October prices
    assert d.factor(date(2026, 10, 20)) == pytest.approx(1.0)
    assert d.factor(date(2026, 9, 5), base=sep) == pytest.approx(1.0)
    assert d.is_extrapolated(date(2026, 11, 2))
    assert not d.is_extrapolated(date(2026, 10, 31))


def test_deflator_interpolates_between_mid_months():
    jan, feb, mar = date(2026, 1, 1), date(2026, 2, 1), date(2026, 3, 1)
    d = Deflator({jan: 100.0, feb: 110.0, mar: 121.0})
    assert d.level(date(2026, 1, 16)) == pytest.approx(100.0)  # middle of a 31-day month
    assert d.level(date(2026, 3, 16)) == pytest.approx(121.0)
    # 31 January: 15 days after the January anchor; the February anchor is 29.5 days after it
    expected = math.exp(math.log(100.0) + 15 / 29.5 * math.log(1.1))
    assert d.level(date(2026, 1, 31)) == pytest.approx(expected)
    assert d.level(date(2025, 12, 1)) == 100.0  # before the series: first value
    assert d.level(date(2026, 3, 31)) == 121.0  # after the last anchor: held
    assert d.is_extrapolated(date(2026, 3, 20))
    assert not d.is_extrapolated(date(2026, 2, 20))


def test_deflator_rejects_bad_input():
    with pytest.raises(ValueError):
        Deflator({})
    with pytest.raises(ValueError):
        Deflator({date(2026, 1, 1): 100.0}, method="other")
    with pytest.raises(ValueError):
        Deflator({date(2026, 1, 1): 100.0, date(2026, 3, 1): 101.0})
    d = Deflator({date(2026, 1, 1): 100.0})
    with pytest.raises(ValueError):
        d.factor(date(2026, 1, 5), base=date(2025, 1, 1))


def test_fisher_real_rate():
    assert fisher_real_rate(0.40, 0.30) == pytest.approx(1.40 / 1.30 - 1)
    assert fisher_real_rate(0.30, 0.30) == pytest.approx(0.0)
