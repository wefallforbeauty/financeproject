"""Investments: returns, risk and concentration of a portfolio of ledger accounts.

**The portfolio** is a set of asset accounts: every account of kind fund,
stock, gold or fx, or the accounts under the paths given (a brokerage's cash
account, a time deposit…).

**Cash flows** are what crosses its boundary (``core.returns``). In a
transaction with a leg in the portfolio, the other legs bring money in or
take it out, valued at the day's prices. Two kinds of legs are not flows:

- the trading accounts (``Equity:Trading``), through which one unit is
  exchanged for another;
- the income and expenses of the investments themselves
  (``RETURN_ACCOUNTS``: interest, dividends, ``Income:Investments``,
  ``Expenses:Investments``), which are part of the return.

So buying a fund with money from the bank is an inflow, a dividend paid into
a portfolio account and a fee charged there are return, and spending from a
foreign currency account is an outflow. Income paid outside the portfolio (a
dividend into a bank account that is not part of it) is not seen.

**Risk** uses the portfolio's returns on market days (days with a price of
something it holds); weekends and holidays are folded into the next market
day. **Concentration** looks at the financial assets (liquidity tiers 0–2):
shares by holding, institution and currency, the Herfindahl–Hirschman index,
and every person's deposits at every bank against the deposit insurance limit
of the TMSF.
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from financeproject.core import returns as ret
from financeproject.core import risk
from financeproject.core.kinds import ASSET
from financeproject.core.text import fold
from financeproject.core.valuation import GOLD, USD, value_positions
from financeproject.errors import FinanceError
from financeproject.ledger.accounts import account_tier, find_account, owner_name
from financeproject.ledger.db import TRADING_ROOT
from financeproject.ledger.models import Account, Commodity, Posting, Transaction
from financeproject.ledger.queries import account_balance, positions_at
from financeproject.ledger.transactions import SPLIT_SOURCE
from financeproject.reports.context import MarketData, load_market_data

DEFAULT_KINDS = ("fund", "stock", "gold", "fx")
RETURN_ACCOUNTS = ("Income:Interest", "Income:Dividends", "Income:Investments", "Expenses:Investments")
DEPOSIT_KINDS = ("checking", "time_deposit", "fx", "gold")
INSURED_LIMITS = {2024: Decimal(650_000), 2025: Decimal(950_000), 2026: Decimal(1_200_000)}
"""Deposits insured by the TMSF per person and bank, in TRY, by calendar year."""
THRESHOLD = 0.5
"""Share above which one holding, institution or currency is flagged (Ghostfolio's default)."""
REAL, GOLD_UNIT = "real TRY", "gold (g)"
UNITS = ("TRY", REAL, USD, GOLD_UNIT)


def under(path: str, prefixes) -> bool:
    """True if ``path`` is one of ``prefixes`` or below one of them."""
    p = fold(path)
    return any(p == fold(x) or p.startswith(fold(x) + ":") for x in prefixes)


def portfolio_accounts(session: Session, paths: list[str] | None = None) -> list[Account]:
    """The accounts under ``paths`` (or named by them), or every fund, stock, gold and fx account."""
    assets = list(session.scalars(select(Account).where(Account.type == ASSET).order_by(Account.path)))
    if not paths:
        chosen = [a for a in assets if a.kind in DEFAULT_KINDS]
        if not chosen:
            raise FinanceError("No investment accounts (kinds fund, stock, gold or fx); choose accounts with --account.")
        return chosen
    chosen: list[Account] = []
    for p in paths:
        found = [a for a in assets if under(a.path, [p])]
        if not found:
            account = find_account(session, p)
            if account.type != ASSET:
                raise FinanceError(f"A portfolio holds asset accounts, not {account.path}.")
            found = [account]
        chosen += [a for a in found if a not in chosen]
    return sorted(chosen, key=lambda a: a.path)


class _Prices:
    """Prices in TRY by day, looked up once."""

    def __init__(self, market: MarketData):
        self.book = market.book
        self._cache: dict[tuple[str, date], float] = {}

    def get(self, code: str, on: date) -> float | None:
        if code == self.book.base:
            return 1.0
        key = (code, on)
        if key not in self._cache:
            price = self.book.resolve(code, on)
            self._cache[key] = None if price is None else float(price.value)
        return self._cache[key]

    def __call__(self, code: str, on: date) -> float:
        price = self.get(code, on)
        if price is None:
            raise FinanceError(
                f"No price of {code} on or before {on}: get prices with 'finance price update' or enter one "
                "with 'finance price add'."
            )
        return price


@dataclass(frozen=True)
class History:
    accounts: tuple[str, ...]
    days: list[ret.Day]
    """In TRY, one per calendar day; the first is the day before the start."""
    commodities: tuple[str, ...]
    """What the portfolio held at some point of the period."""
    held: dict[str, set[date]]
    """For every commodity, the days at whose end the portfolio held some."""


def history(session: Session, accounts: list[Account], start: date, end: date, market: MarketData) -> History:
    """The portfolio's value at the end of every day from the day before ``start`` to ``end``, with its flows."""
    if end < start:
        raise FinanceError("The end date is before the start date.")
    ids = {a.id for a in accounts}
    prices = _Prices(market)
    rows = session.execute(
        select(Transaction.id, Transaction.date, Posting.account_id, Posting.commodity_code, Posting.amount, Account.path)
        .join(Posting, Posting.transaction_id == Transaction.id)
        .join(Account, Account.id == Posting.account_id)
        .where(Transaction.date <= end)
        .order_by(Transaction.date, Transaction.id, Posting.id)
    ).all()
    legs: dict[int, list] = defaultdict(list)
    for row in rows:
        legs[row.id].append(row)
    changes: dict[date, dict[str, Decimal]] = defaultdict(lambda: defaultdict(Decimal))
    inflow: dict[date, float] = defaultdict(float)
    outflow: dict[date, float] = defaultdict(float)
    first = start - timedelta(days=1)
    for tx_legs in legs.values():
        inside = [leg for leg in tx_legs if leg.account_id in ids]
        if not inside:
            continue
        on = tx_legs[0].date
        for leg in inside:
            changes[max(on, first)][leg.commodity_code] += leg.amount
        if on <= first:
            continue
        flow = -sum(
            float(leg.amount) * prices(leg.commodity_code, on)
            for leg in tx_legs
            if leg.account_id not in ids and not under(leg.path, [TRADING_ROOT]) and not under(leg.path, RETURN_ACCOUNTS)
        )
        if flow > 0:
            inflow[on] += flow
        elif flow < 0:
            outflow[on] -= flow
    quantities: dict[str, Decimal] = defaultdict(Decimal)
    held: dict[str, set[date]] = defaultdict(set)
    days = []
    on = first
    while on <= end:
        for code, amount in changes.get(on, {}).items():
            quantities[code] += amount
        value = 0.0
        for code, quantity in quantities.items():
            if quantity != 0:
                held[code].add(on)
                value += float(quantity) * prices(code, on)
        days.append(ret.Day(on, value, inflow.get(on, 0.0), outflow.get(on, 0.0)))
        on += timedelta(days=1)
    return History(tuple(a.path for a in accounts), days, tuple(sorted(held)), dict(held))


def first_day(session: Session, accounts: list[Account]) -> date | None:
    """The first day any of the accounts has a posting."""
    ids = [a.id for a in accounts]
    return session.scalar(
        select(Transaction.date).join(Posting).where(Posting.account_id.in_(ids)).order_by(Transaction.date).limit(1)
    )


# --------------------------------------------------------------------------- returns


def unit_rates(market: MarketData, days: list[date]) -> dict[str, dict[date, float] | None]:
    """For every reporting unit, the factor that turns a TRY amount of each day into it (``None``: no data)."""
    out: dict[str, dict[date, float] | None] = {"TRY": {d: 1.0 for d in days}}
    if market.deflator is None:
        out[REAL] = None
    else:
        out[REAL] = {d: market.deflator.factor(d, market.base_month) for d in days}
    for unit, code in ((USD, USD), (GOLD_UNIT, GOLD)):
        rates = {}
        for d in days:
            price = market.book.resolve(code, d)
            if price is None:
                rates = None
                break
            rates[d] = 1 / float(price.value)
        out[unit] = rates
    return out


@dataclass(frozen=True)
class Returns:
    name: str
    """The unit, or the account."""
    start_value: float
    inflows: float
    outflows: float
    end_value: float
    twr: float
    twr_year: float | None
    money_weighted: float | None
    """Money-weighted return a year (XIRR)."""

    @property
    def gain(self) -> float:
        return self.end_value - self.start_value - self.inflows + self.outflows


def returns_of(name: str, days: list[ret.Day]) -> Returns:
    period = (days[-1].on - days[0].on).days
    total = ret.twr(days)
    return Returns(
        name,
        days[0].value,
        sum(d.inflow for d in days[1:]),
        sum(d.outflow for d in days[1:]),
        days[-1].value,
        total,
        ret.annualize(total, period),
        ret.money_weighted(days),
    )


@dataclass(frozen=True)
class ReturnsReport:
    start: date
    end: date
    accounts: tuple[str, ...]
    units: list[Returns]
    by_account: list[Returns]
    base_month: date | None
    cpi_extrapolated: bool
    """The period ends after the last published CPI month (prices held constant since)."""

    @property
    def days(self) -> int:
        return (self.end - self.start).days + 1


def returns_report(
    session: Session,
    start: date | None = None,
    end: date | None = None,
    paths: list[str] | None = None,
    market: MarketData | None = None,
    by_account: bool = True,
) -> ReturnsReport:
    accounts = portfolio_accounts(session, paths)
    market = market or load_market_data(session)
    start = start or first_day(session, accounts)
    if start is None:
        raise FinanceError("The portfolio's accounts have no transactions yet.")
    end = end or date.today()
    whole = history(session, accounts, start, end, market)
    rates = unit_rates(market, [d.on for d in whole.days])
    units = [returns_of(unit, ret.in_unit(whole.days, r)) for unit, r in rates.items() if r is not None]
    per_account = []
    if by_account and len(accounts) > 1:
        for account in accounts:
            days = history(session, [account], start, end, market).days
            if any(d.value or d.inflow or d.outflow for d in days):
                per_account.append(returns_of(account.path, days))
    extrapolated = market.deflator is not None and market.deflator.is_extrapolated(end)
    return ReturnsReport(start, end, whole.accounts, units, per_account, market.base_month, extrapolated)


# --------------------------------------------------------------------------- risk


def market_days(market: MarketData, commodities: tuple[str, ...], start: date, end: date) -> list[date]:
    """Days from ``start`` to ``end`` with a quote of something held (or of the dollar it is priced through)."""
    book = market.book
    days: set[date] = set()
    for code in commodities:
        if code == book.base:
            continue
        days |= set(book.dates(code))
        if book.first_date(code, book.base) is None:
            days |= set(book.dates(USD))
    return sorted(d for d in days if start <= d <= end)


def on_market_days(daily: list[tuple[date, float]], days: list[date]) -> list[tuple[date, float]]:
    """Daily returns compounded into one return per market day (days in between fold into the next)."""
    out = []
    growth, i = 1.0, 0
    for on, r in daily:
        growth *= 1 + r
        while i < len(days) and days[i] < on:
            i += 1
        if i < len(days) and days[i] == on:
            out.append((on, growth - 1))
            growth = 1.0
    return out


def recorded_splits(session: Session, code: str) -> list[tuple[date, float]]:
    """The splits of ``code`` recorded in the ledger (``finance split``): day and new shares per old share."""
    out = {}
    txs = session.scalars(
        select(Transaction).join(Posting).where(Transaction.source == SPLIT_SOURCE, Posting.commodity_code == code)
    ).unique()
    for tx in txs:
        for leg in tx.postings:
            if leg.account.type == ASSET and leg.commodity_code == code:
                before = account_balance(session, leg.account, tx.date - timedelta(days=1)).get(code, Decimal(0))
                if before > 0:
                    out.setdefault(tx.date, float((before + leg.amount) / before))
    return sorted(out.items())


def held_prices(
    session: Session, market: MarketData, code: str, days: list[date], held: set[date]
) -> dict[date, float]:
    """Prices of ``code`` on the market days it was held, in the units after the recorded splits
    (a price before a 2-for-1 split is halved), so that a split is no fall."""
    prices = _Prices(market)
    splits = recorded_splits(session, code)
    out = {}
    for d in days:
        if d in held and (p := prices.get(code, d)) is not None:
            for on, ratio in splits:
                if on > d:
                    p /= ratio
            out[d] = p
    return out


@dataclass(frozen=True)
class HoldingRisk:
    commodity: str
    volatility: float | None
    drawdown: risk.Drawdown | None
    correlation: float | None
    """With the portfolio's returns."""


@dataclass(frozen=True)
class RiskReport:
    start: date
    end: date
    accounts: tuple[str, ...]
    value: float
    """The portfolio's value at the end, TRY."""
    returns: list[tuple[date, float]]
    """On market days."""
    volatility: float | None
    drawdown: risk.Drawdown | None
    historical: list[risk.ValueAtRisk]
    normal: list[risk.ValueAtRisk]
    backtests: list[risk.Backtest]
    window: int
    holdings: list[HoldingRisk]
    correlations: dict[tuple[str, str], float | None]


def risk_report(
    session: Session,
    start: date | None = None,
    end: date | None = None,
    paths: list[str] | None = None,
    market: MarketData | None = None,
    window: int = 250,
) -> RiskReport:
    accounts = portfolio_accounts(session, paths)
    market = market or load_market_data(session)
    start = start or first_day(session, accounts)
    if start is None:
        raise FinanceError("The portfolio's accounts have no transactions yet.")
    end = end or date.today()
    whole = history(session, accounts, start, end, market)
    days = market_days(market, whole.commodities, start, end)
    returns = on_market_days(ret.daily_returns(whole.days), days)
    values = [r for _, r in returns]
    index = risk.growth_index(returns, whole.days[0].on)
    portfolio = {d: math.log1p(r) for d, r in returns}
    holdings = []
    logs: dict[str, dict[date, float]] = {}
    for code in whole.commodities:
        if code == market.book.base:
            continue
        series = held_prices(session, market, code, days, whole.held.get(code, set()))
        dated = sorted(series.items())
        logs[code] = {b: math.log(series[b] / series[a]) for a, b in zip(days, days[1:]) if a in series and b in series}
        both = [(r, portfolio[d]) for d, r in logs[code].items() if d in portfolio]
        holdings.append(
            HoldingRisk(
                code,
                risk.volatility([math.expm1(r) for r in logs[code].values()]),
                risk.max_drawdown(dated),
                risk.correlation([a for a, _ in both], [b for _, b in both]),
            )
        )
    correlations: dict[tuple[str, str], float | None] = {}
    names = sorted(logs)
    for i, a in enumerate(names):
        for b in names[i + 1 :]:
            common = sorted(set(logs[a]) & set(logs[b]))
            correlations[(a, b)] = risk.correlation([logs[a][d] for d in common], [logs[b][d] for d in common])
    return RiskReport(
        start,
        end,
        whole.accounts,
        whole.days[-1].value,
        returns,
        risk.volatility(values),
        risk.max_drawdown(index),
        [v for level in risk.LEVELS if (v := risk.historical_var(values, level))],
        [v for level in risk.LEVELS if (v := risk.normal_var(values, level))],
        [b for level in risk.LEVELS if (b := risk.backtest(values, level, window))],
        window,
        holdings,
        correlations,
    )


# --------------------------------------------------------------------------- concentration


@dataclass(frozen=True)
class Share:
    name: str
    value: Decimal
    share: float


@dataclass(frozen=True)
class InsuredDeposit:
    owner: str
    institution: str
    value: Decimal
    limit: Decimal

    @property
    def over(self) -> Decimal:
        return max(self.value - self.limit, Decimal(0))


def insured_limit(year: int) -> tuple[Decimal, int]:
    """The TMSF limit of ``year``, or of the nearest year the program knows, and that year."""
    known = min(INSURED_LIMITS, key=lambda y: (abs(y - year), -y))
    return INSURED_LIMITS[known], known


@dataclass(frozen=True)
class ConcentrationReport:
    on: date
    accounts: tuple[str, ...]
    total: Decimal
    by_holding: list[Share]
    by_institution: list[Share]
    by_currency: list[Share]
    hhi: float
    """Σ share² over the holdings."""
    threshold: float
    deposits: list[InsuredDeposit]
    limit: Decimal
    limit_year: int
    missing: tuple[str, ...]
    """Accounts left out because their commodity has no price."""
    currencies: frozenset[str] = frozenset()
    """The holdings that are currencies (not counted as single investments)."""

    @property
    def effective_holdings(self) -> float | None:
        return 1 / self.hhi if self.hhi else None

    def flags(self) -> list[str]:
        out = []
        investments = [s for s in self.by_holding if s.name not in self.currencies]
        for label, shares in (("holding", investments), ("institution", self.by_institution), ("currency", self.by_currency)):
            named = [s for s in shares if s.name != NO_INSTITUTION]
            if named and named[0].share > self.threshold:
                out.append(f"{named[0].share * 100:.0f} % in one {label}: {named[0].name}")
        for d in self.deposits:
            if d.over > 0:
                out.append(
                    f"{d.owner} at {d.institution}: deposits of {d.value:,.2f} TRY are {d.over:,.2f} over the insured "
                    f"{d.limit:,.0f}"
                )
        return out


NO_INSTITUTION = "(no institution)"


def _shares(values: dict[str, Decimal], total: Decimal) -> list[Share]:
    return sorted(
        (Share(name, value, float(value / total) if total else 0.0) for name, value in values.items() if value),
        key=lambda s: (-s.value, s.name),
    )


def concentration_report(
    session: Session,
    on: date | None = None,
    paths: list[str] | None = None,
    market: MarketData | None = None,
    threshold: float = THRESHOLD,
    limit: Decimal | None = None,
) -> ConcentrationReport:
    on = on or date.today()
    market = market or load_market_data(session)
    if paths:
        chosen = {a.id for a in portfolio_accounts(session, paths)}
    else:
        chosen = {
            a.id for a in session.scalars(select(Account).where(Account.type == ASSET)) if (account_tier(a) or 0) < 3
        }
    positions = positions_at(session, on, types=(ASSET,))
    valued = value_positions(positions, market.book, on)
    kinds = {c.code: c.kind for c in session.scalars(select(Commodity))}
    holding: dict[str, Decimal] = defaultdict(Decimal)
    institution: dict[str, Decimal] = defaultdict(Decimal)
    currency: dict[str, Decimal] = defaultdict(Decimal)
    missing, names = [], set()
    for v in valued:
        p = v.position
        if p.account_id not in chosen:
            continue
        if v.value is None:
            missing.append(f"{p.account} ({p.commodity})")
            continue
        names.add(p.account)
        holding[p.commodity] += v.value
        institution[p.institution or NO_INSTITUTION] += v.value
        if kinds.get(p.commodity) in ("currency", "gold") or p.commodity == market.book.base:
            currency[p.commodity] += v.value
        else:
            route = v.price.path if v.price else ()
            currency[route[0].quote if route else market.book.base] += v.value
    total = sum(holding.values(), Decimal(0))
    by_holding = _shares(holding, total)
    hhi = sum(s.share**2 for s in by_holding)
    insured, year = (limit, on.year) if limit is not None else insured_limit(on.year)
    deposits: dict[tuple[str, str], Decimal] = defaultdict(Decimal)
    accounts = {a.id: a for a in session.scalars(select(Account))}
    for v in valued:
        account = accounts[v.position.account_id]
        if account.kind in DEPOSIT_KINDS and account.institution and v.value is not None:
            deposits[(owner_name(account), account.institution)] += v.value
    return ConcentrationReport(
        on,
        tuple(sorted(names)),
        total,
        by_holding,
        _shares(institution, total),
        _shares(currency, total),
        hhi,
        threshold,
        [InsuredDeposit(o, i, value, insured) for (o, i), value in sorted(deposits.items())],
        insured,
        year,
        tuple(missing),
        frozenset(code for code, kind in kinds.items() if kind == "currency"),
    )
