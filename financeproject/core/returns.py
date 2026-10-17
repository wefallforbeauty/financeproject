"""Returns of a portfolio: time-weighted and money-weighted.

The portfolio is valued at the end of every day. Money that crosses its
boundary is a cash flow: CFin (bought with money from outside, opening
balances) and CFout (sold into an outside account, spent). As in Portfolio
Performance's "true time-weighted rate of return" (TTWROR), inflows count at
the start of the day and outflows at its end:

    1 + r_t = (MVE_t + CFout_t) / (MVB_t + CFin_t),   MVB_t = MVE_{t-1}
    TWR     = Π (1 + r_t) − 1
    a year  = (1 + TWR)^(365 / days) − 1

The time-weighted return measures the investments, whatever was added or
taken out and when; the money-weighted return (XIRR) measures the
investor's money: the rate X with

    − V_0 − Σ CFin_t (1 + X)^−(t/365) + Σ CFout_t (1 + X)^−(t/365) + V_end (1 + X)^−(T/365) = 0

with t and T in days from the start. Both can be taken in any unit (real
TRY, US dollars, grams of gold) by converting values and flows at each day's
rate first.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from financeproject.core.loans import solve


@dataclass(frozen=True, slots=True)
class Day:
    """A day of the portfolio: its value at the end of the day and the money that crossed in or out."""

    on: date
    value: float
    inflow: float = 0.0
    """Money brought in (positive)."""
    outflow: float = 0.0
    """Money taken out (positive)."""


def daily_returns(days: list[Day]) -> list[tuple[date, float]]:
    """r_t for every day after the first; days that start empty and receive nothing are left out."""
    out = []
    for prev, day in zip(days, days[1:]):
        base = prev.value + day.inflow
        if base > 0:
            out.append((day.on, (day.value + day.outflow) / base - 1))
        elif day.value + day.outflow != 0:
            raise ValueError(f"The portfolio is worth {day.value + day.outflow:.2f} on {day.on} without a starting value.")
    return out


def twr(days: list[Day]) -> float:
    """Cumulative time-weighted return from the end of the first day to the end of the last."""
    growth = 1.0
    for _, r in daily_returns(days):
        growth *= 1 + r
    return growth - 1


def annualize(total: float, days: int) -> float | None:
    """A return over ``days`` days as a yearly rate (365-day years)."""
    if days <= 0 or total <= -1:
        return None
    return (1 + total) ** (365 / days) - 1


def xirr(flows: list[tuple[date, float]], low: float = -0.9999, high: float = 1e4) -> float | None:
    """The yearly rate that makes the dated flows worth zero at the first date (actual/365).

    Flows are signed from the investor's side: money in negative, money out and
    the final value positive. ``None`` when no rate in ``[low, high]`` does it.
    """
    flows = [(d, a) for d, a in flows if a != 0]
    if not flows or all(a > 0 for _, a in flows) or all(a < 0 for _, a in flows):
        return None
    t0 = min(d for d, _ in flows)

    def npv(rate: float) -> float:
        return sum(a * (1 + rate) ** (-(d - t0).days / 365) for d, a in flows)

    if npv(low) * npv(high) > 0:
        return None
    return solve(npv, low, high)


def investor_flows(days: list[Day]) -> list[tuple[date, float]]:
    """The portfolio's flows from the investor's side, for ``xirr``: the starting value and the
    inflows paid in (negative), the outflows and the final value received (positive)."""
    if not days:
        return []
    flows = [(days[0].on, -days[0].value)]
    for day in days[1:]:
        flows.append((day.on, day.outflow - day.inflow))
    flows.append((days[-1].on, days[-1].value))
    return flows


def money_weighted(days: list[Day]) -> float | None:
    """The money-weighted return a year (XIRR) of the days' flows."""
    return xirr(investor_flows(days))


def in_unit(days: list[Day], rates: dict[date, float]) -> list[Day]:
    """The days converted to another unit: every amount multiplied by its day's rate."""
    return [Day(d.on, d.value * rates[d.on], d.inflow * rates[d.on], d.outflow * rates[d.on]) for d in days]
