"""Valuation of ledger positions and net worth in four units.

Net worth at day t, in TRY:   NW(t) = Σ_assets q·P(t) − Σ_liabilities |q|·P(t)
Real TRY (base month b):      NW(t) · CPI(b) / CPI(t)
US dollars:                   NW(t) / P_USD(t)
Grams of gold:                NW(t) / P_XAU_G(t)

P(t) is the latest price on or before t. Ledger quantities are signed
(liabilities are negative), so the net worth is simply the sum of signed values
over asset and liability accounts. Details: docs/METHOD.md.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Hashable, Iterable
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from financeproject.core.inflation import Deflator
from financeproject.core.kinds import ASSET, LIABILITY, SHORT, debt_term
from financeproject.core.prices import PriceBook, ResolvedPrice

USD, GOLD = "USD", "XAU_G"
DEFAULT_MAX_AGE_DAYS = 7


@dataclass(frozen=True, slots=True)
class Position:
    """Balance of one account in one commodity (signed ledger quantity)."""

    account_id: int
    account: str
    account_type: str
    commodity: str
    quantity: Decimal
    kind: str | None = None
    owner: str | None = None
    tier: int | None = None
    institution: str | None = None


@dataclass(frozen=True, slots=True)
class ValuedPosition:
    position: Position
    price: ResolvedPrice | None
    value: Decimal | None
    """Signed value in the base currency; ``None`` when no price is known."""
    stale: bool = False

    @property
    def missing(self) -> bool:
        return self.value is None


def value_positions(
    positions: Iterable[Position],
    book: PriceBook,
    at: date,
    max_age_days: int = DEFAULT_MAX_AGE_DAYS,
) -> list[ValuedPosition]:
    out: list[ValuedPosition] = []
    for p in positions:
        price = book.resolve(p.commodity, at)
        if price is None:
            out.append(ValuedPosition(p, None, None))
            continue
        stale = p.commodity != book.base and price.age_days(at) > max_age_days
        out.append(ValuedPosition(p, price, p.quantity * price.value, stale))
    return out


@dataclass(frozen=True, slots=True)
class Units:
    """One amount expressed in the four reporting units."""

    try_: Decimal
    real_try: Decimal | None
    usd: Decimal | None
    gold_g: Decimal | None


@dataclass(frozen=True)
class Converter:
    """Converts TRY amounts of day ``at`` into the other three units."""

    at: date
    usd_price: ResolvedPrice | None
    gold_price: ResolvedPrice | None
    cpi_factor: float | None
    cpi_base: date | None
    cpi_extrapolated: bool = False

    @classmethod
    def build(cls, book: PriceBook, at: date, deflator: Deflator | None = None, cpi_base: date | None = None) -> Converter:
        factor = base = None
        extrapolated = False
        if deflator is not None:
            base = cpi_base or deflator.last_month
            factor = deflator.factor(at, base)
            extrapolated = deflator.is_extrapolated(at)
        return cls(at, book.resolve(USD, at), book.resolve(GOLD, at), factor, base, extrapolated)

    def units(self, amount_try: Decimal) -> Units:
        real = None
        if self.cpi_factor is not None:
            real = amount_try * Decimal(repr(self.cpi_factor))
        usd = amount_try / self.usd_price.value if self.usd_price else None
        gold = amount_try / self.gold_price.value if self.gold_price else None
        return Units(amount_try, real, usd, gold)


@dataclass(frozen=True)
class NetWorth:
    at: date
    assets: Decimal
    liabilities: Decimal
    """Debt as a positive amount."""
    net: Units
    converter: Converter
    missing: tuple[str, ...] = ()
    """Accounts left out because their commodity has no price."""
    stale: tuple[str, ...] = ()
    """Accounts valued with a price older than the allowed age."""

    @property
    def complete(self) -> bool:
        return not self.missing


def net_worth(
    valued: Iterable[ValuedPosition],
    book: PriceBook,
    at: date,
    deflator: Deflator | None = None,
    cpi_base: date | None = None,
) -> NetWorth:
    assets = liabilities = Decimal(0)
    missing: list[str] = []
    stale: list[str] = []
    for v in valued:
        t = v.position.account_type
        if t not in (ASSET, LIABILITY):
            continue
        if v.value is None:
            missing.append(f"{v.position.account} ({v.position.commodity})")
            continue
        if v.stale:
            stale.append(f"{v.position.account} ({v.position.commodity})")
        if t == ASSET:
            assets += v.value
        else:
            liabilities -= v.value
    conv = Converter.build(book, at, deflator, cpi_base)
    return NetWorth(at, assets, liabilities, conv.units(assets - liabilities), conv, tuple(missing), tuple(stale))


def group_values(
    valued: Iterable[ValuedPosition], key: Callable[[Position], Hashable]
) -> dict[Hashable, Decimal]:
    """Sum of signed values per group (positions without a price are skipped)."""
    out: dict[Hashable, Decimal] = defaultdict(Decimal)
    for v in valued:
        if v.value is not None:
            out[key(v.position)] += v.value
    return dict(out)


@dataclass(frozen=True)
class Liquidity:
    by_tier: dict[int, Decimal] = field(default_factory=dict)
    """Asset values per liquidity tier."""
    short_term_debt: Decimal = Decimal(0)
    long_term_debt: Decimal = Decimal(0)

    @property
    def liquid_assets(self) -> Decimal:
        """Tiers 0 and 1: usable within about a week."""
        return self.by_tier.get(0, Decimal(0)) + self.by_tier.get(1, Decimal(0))

    @property
    def liquid_net(self) -> Decimal:
        """Liquid assets minus short-term debt (cards, overdraft, personal debts)."""
        return self.liquid_assets - self.short_term_debt


def liquidity(valued: Iterable[ValuedPosition]) -> Liquidity:
    by_tier: dict[int, Decimal] = defaultdict(Decimal)
    short = long = Decimal(0)
    for v in valued:
        if v.value is None:
            continue
        p = v.position
        if p.account_type == ASSET:
            by_tier[p.tier if p.tier is not None else 3] += v.value
        elif p.account_type == LIABILITY:
            if debt_term(p.kind) == SHORT:
                short -= v.value
            else:
                long -= v.value
    return Liquidity(dict(sorted(by_tier.items())), short, long)
