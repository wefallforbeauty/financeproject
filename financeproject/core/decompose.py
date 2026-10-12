"""Splitting a change in value into flows and revaluation (summation by parts).

A group of accounts (the whole balance sheet, or only the cash accounts) holds
``q_c(t)`` units of each commodity ``c``. ``V_c(t)`` is the value of one unit
in the unit of account (TRY, real TRY, USD or grams of gold), so the group is
worth ``W(t) = Σ_c q_c(t)·V_c(t)``. Transactions change the holdings on dates
``τ_1 ≤ … ≤ τ_n`` between ``t0`` and ``t1``. Summation by parts gives the
exact identity::

    W(t1) − W(t0) = Σ_k Σ_c Δq_c(τ_k) · V_c(τ_k)                  flows, at the prices of their day
                  + Σ_c Σ_j q_c(s_j) · [V_c(s_j+1) − V_c(s_j)]    revaluation (holding gains and losses)

where ``s_0 = t0``, the ``s_j`` are the transaction dates and the last ``s`` is
``t1``. Every transaction balances in every commodity, so what flows into the
group equals minus its legs outside the group; the flows are therefore told
apart by those counterpart legs: income, expenses, exchange results (trading
accounts), opening balances, and so on.

Commodities without a value on a date where one is needed are left out of the
group, together with all their legs; what moved between them and valued
commodities is shown as ``unvalued``. Methods and references: docs/METHOD.md.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

UNVALUED = "unvalued"
EXCHANGE = "exchange"

Valuer = Callable[[str, date], Decimal | None]
"""Value of one unit of a commodity on a day, in the unit of account; ``None`` if unknown."""


@dataclass(frozen=True, slots=True)
class Leg:
    commodity: str
    quantity: Decimal
    category: str | None = None
    """``None`` for a leg inside the group; otherwise the counterpart's category."""
    detail: str | None = None
    """For example the counterpart account's name."""


@dataclass(frozen=True, slots=True)
class Event:
    on: date
    legs: tuple[Leg, ...]


@dataclass
class Decomposition:
    start: date
    end: date
    start_value: Decimal
    end_value: Decimal
    flows: dict[str, Decimal] = field(default_factory=dict)
    """Category → value that flowed into the group (negative: out of it)."""
    details: dict[str, dict[str, Decimal]] = field(default_factory=dict)
    revaluation: dict[str, Decimal] = field(default_factory=dict)
    """Commodity → holding gain (negative: loss)."""
    unvalued: tuple[str, ...] = ()
    """Commodities left out because they had no value on a date where one was needed."""

    @property
    def change(self) -> Decimal:
        return self.end_value - self.start_value

    @property
    def flow_total(self) -> Decimal:
        return sum(self.flows.values(), Decimal(0))

    @property
    def revaluation_total(self) -> Decimal:
        return sum(self.revaluation.values(), Decimal(0))

    @property
    def residual(self) -> Decimal:
        """Zero up to rounding; anything else is a bug."""
        return self.change - self.flow_total - self.revaluation_total


def _cached(value: Valuer) -> Valuer:
    cache: dict[tuple[str, date], Decimal | None] = {}

    def get(commodity: str, on: date) -> Decimal | None:
        key = (commodity, on)
        if key not in cache:
            cache[key] = value(commodity, on)
        return cache[key]

    return get


def decompose(
    start: date,
    end: date,
    holdings: Mapping[str, Decimal],
    events: Iterable[Event],
    value: Valuer,
) -> Decomposition:
    """Split the change of a group's value from ``start`` to ``end``.

    ``holdings`` are the group's quantities at the end of ``start``; ``events``
    are the transactions dated after ``start`` and up to ``end``.
    """
    value = _cached(value)
    events = sorted(events, key=lambda e: e.on)

    # Pass 1: on which dates is the value of each commodity needed?
    q: dict[str, Decimal] = defaultdict(Decimal, {c: v for c, v in holdings.items() if v})
    needed: dict[str, set[date]] = defaultdict(set)
    for c in q:
        needed[c].add(start)
    for event in events:
        for c, held in q.items():
            if held:
                needed[c].add(event.on)
        for leg in event.legs:
            needed[leg.commodity].add(event.on)
            if leg.category is None:
                q[leg.commodity] += leg.quantity
    for c, held in q.items():
        if held:
            needed[c].add(end)
    unvalued = {c for c, days in needed.items() if any(value(c, d) is None for d in days)}

    # Pass 2: flows by counterpart, then revaluation by commodity.
    flows: dict[str, Decimal] = defaultdict(Decimal)
    details: dict[str, dict[str, Decimal]] = defaultdict(lambda: defaultdict(Decimal))
    for event in events:
        touches_unvalued = any(leg.commodity in unvalued for leg in event.legs)
        for leg in event.legs:
            if leg.category is None or leg.commodity in unvalued:
                continue
            amount = -leg.quantity * value(leg.commodity, event.on)
            category = UNVALUED if touches_unvalued and leg.category == EXCHANGE else leg.category
            flows[category] += amount
            details[category][leg.detail or leg.category] += amount

    revaluation: dict[str, Decimal] = {}
    start_value = end_value = Decimal(0)
    for c in sorted(needed):
        if c in unvalued:
            continue
        held = Decimal(holdings.get(c, 0))
        if held:
            start_value += held * value(c, start)
        gain = Decimal(0)
        previous = start
        for event in events:
            if held:
                gain += held * (value(c, event.on) - value(c, previous))
            held += sum((leg.quantity for leg in event.legs if leg.category is None and leg.commodity == c), Decimal(0))
            previous = event.on
        if held:
            gain += held * (value(c, end) - value(c, previous))
            end_value += held * value(c, end)
        if gain:
            revaluation[c] = gain

    return Decomposition(
        start=start,
        end=end,
        start_value=start_value,
        end_value=end_value,
        flows=dict(flows),
        details={k: dict(v) for k, v in details.items()},
        revaluation=revaluation,
        unvalued=tuple(sorted(unvalued)),
    )
