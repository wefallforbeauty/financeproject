"""Fetch prices and the CPI and store them.

- Foreign currencies: TCMB daily bulletins (one request per business day), or
  one EVDS request per range when a key is set.
- Gold: Frankfurter XAU/USD in yearly chunks, stored per gram in USD.
- CPI: EVDS ``TP.TUKFIY2025.GENEL`` with a key, otherwise the TCMB table.
  A snapshot of the table ships with the package so real TRY works offline.
- Funds: TEFAS, one request per fund (price source ``tefas``).
- Shares: Yahoo Finance, one request per share (price source ``yahoo``), with
  a warning when a split happened while the ledger held the share and no
  ``finance split`` was recorded for it.

Funds and shares are fetched from the start date, or from a week before the
last stored price when the start is already covered.
"""

from __future__ import annotations

import csv
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from importlib import resources

import httpx
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from financeproject.core.dates import business_days, parse_month
from financeproject.core.inflation import chain_index
from financeproject.errors import EvdsKeyError, SourceError
from financeproject.ledger.models import Account, Commodity, Posting, Price, Transaction
from financeproject.ledger.transactions import SPLIT_SOURCE
from financeproject.prices import evds, frankfurter, tcmb, tcmb_cpi, tefas, yahoo
from financeproject.prices.store import (
    ADDED,
    CPI,
    UPDATED,
    price_dates,
    price_range,
    upsert_index,
    upsert_price,
)

CPI_BASE_LABEL = "2025=100"
INDEX_DECIMALS = Decimal("0.000001")
BUNDLED_CPI = "cpi_tcmb_monthly.csv"

Progress = Callable[[str], None]


@dataclass
class UpdateReport:
    added: int = 0
    updated: int = 0
    messages: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def count(self, status: str) -> None:
        if status == ADDED:
            self.added += 1
        elif status == UPDATED:
            self.updated += 1

    def merge(self, other: UpdateReport) -> UpdateReport:
        self.added += other.added
        self.updated += other.updated
        self.messages += other.messages
        self.warnings += other.warnings
        return self


def tcmb_currencies(session: Session) -> list[str]:
    """Currencies priced from TCMB: USD always (it is a reporting unit), plus every
    TCMB-priced currency used by an account or a posting."""
    used = set(session.scalars(select(Account.commodity_code).where(Account.commodity_code.is_not(None))))
    used |= set(session.scalars(select(Posting.commodity_code).distinct()))
    tcmb_codes = set(session.scalars(select(Commodity.code).where(Commodity.price_source == "tcmb")))
    return sorted({"USD"} | (used & tcmb_codes))


def update_fx(
    session: Session,
    client: httpx.Client,
    start: date,
    end: date,
    codes: list[str],
    api_key: str | None = None,
    pause: float = 0.25,
    progress: Progress | None = None,
) -> UpdateReport:
    report = UpdateReport()
    if api_key and set(codes) <= set(evds.FX_BUYING_SERIES) and (end - start).days > 7:
        try:
            series = evds.fetch_series(client, api_key, [evds.FX_BUYING_SERIES[c] for c in codes], start, end)
        except EvdsKeyError as exc:
            report.warnings.append(f"{exc} Using the TCMB bulletins instead.")
        else:
            for code in codes:
                for day, value in series[evds.FX_BUYING_SERIES[code]]:
                    report.count(upsert_price(session, code, "TRY", day, value, "evds"))
            report.messages.append(f"Currencies (EVDS): {', '.join(codes)}, {start} – {end}")
            return report

    have = {c: price_dates(session, c, "TRY") for c in codes}
    days = [d for d in business_days(start, end) if any(d not in have[c] for c in codes)]
    fetched = missing = 0
    for i, day in enumerate(days):
        if progress and (i % 20 == 0 or i == len(days) - 1):
            progress(f"TCMB {day} ({i + 1}/{len(days)})")
        bulletin = tcmb.fetch_bulletin(client, day)
        if bulletin is None:
            missing += 1
        else:
            fetched += 1
            for code in codes:
                price = bulletin.price(code)
                if price is None:
                    report.warnings.append(f"The TCMB bulletin of {day} has no buying rate for {code}.")
                    continue
                report.count(upsert_price(session, code, "TRY", bulletin.date, price, "tcmb"))
        if pause and i < len(days) - 1:
            time.sleep(pause)
    report.messages.append(
        f"Currencies (TCMB): {', '.join(codes)}; {fetched} bulletins fetched, "
        f"{missing} days without a bulletin (holiday or not published yet)."
    )
    return report


def _gold_ranges(session: Session, start: date, end: date) -> list[tuple[date, date]]:
    first, last = price_range(session, "XAU_G", "USD")
    if first is None or last is None:
        return [(start, end)]
    ranges = []
    if start < first:
        ranges.append((start, first - timedelta(days=1)))
    refresh_from = max(start, last - timedelta(days=7))
    if end >= refresh_from:
        ranges.append((refresh_from, end))
    return ranges


def update_gold(
    session: Session,
    client: httpx.Client,
    start: date,
    end: date,
    providers: str | None = None,
    progress: Progress | None = None,
) -> UpdateReport:
    report = UpdateReport()
    rows = 0
    for a, b in _gold_ranges(session, start, end):
        chunk = a
        while chunk <= b:
            chunk_end = min(b, date(chunk.year, 12, 31))
            if progress:
                progress(f"Gold {chunk} – {chunk_end}")
            for day, usd_per_gram in frankfurter.fetch_gold_usd_per_gram(client, chunk, chunk_end, providers):
                report.count(upsert_price(session, "XAU_G", "USD", day, usd_per_gram, "frankfurter"))
                rows += 1
            chunk = chunk_end + timedelta(days=1)
    report.messages.append(f"Gold (Frankfurter XAU/USD): {rows} days processed.")
    return report


def _store_cpi(session: Session, levels: dict[date, float | Decimal], source: str, report: UpdateReport) -> None:
    for month, level in levels.items():
        value = Decimal(str(level)).quantize(INDEX_DECIMALS)
        report.count(upsert_index(session, CPI, month, value, source, CPI_BASE_LABEL))


def cpi_from_changes(changes: list[tcmb_cpi.CpiChange]) -> dict[date, float]:
    try:
        return chain_index({c.month: float(c.monthly_pct) for c in changes}, base_year=2025)
    except ValueError as exc:
        raise SourceError(f"Cannot chain the CPI table: {exc}") from exc


def load_bundled_cpi(session: Session) -> UpdateReport:
    """Store the CPI snapshot shipped with the package (lowest priority)."""
    report = UpdateReport()
    text = resources.files("financeproject.data").joinpath(BUNDLED_CPI).read_text(encoding="utf-8")
    changes = [
        tcmb_cpi.CpiChange(
            parse_month(row["month"]), Decimal(row["annual_change_pct"]), Decimal(row["monthly_change_pct"])
        )
        for row in csv.DictReader(text.splitlines())
    ]
    _store_cpi(session, cpi_from_changes(changes), "bundled", report)
    report.messages.append(f"CPI (bundled copy): {changes[0].month:%Y-%m} – {changes[-1].month:%Y-%m}")
    return report


def update_cpi(session: Session, client: httpx.Client, api_key: str | None = None, today: date | None = None) -> UpdateReport:
    report = UpdateReport()
    today = today or date.today()
    if api_key:
        try:
            series = evds.fetch_series(client, api_key, [evds.CPI_SERIES], date(2005, 1, 1), today)
        except SourceError as exc:
            report.warnings.append(f"Could not get the CPI from EVDS ({exc}); using the TCMB table.")
        else:
            values = series[evds.CPI_SERIES]
            if values:
                _store_cpi(session, {m: v for m, v in values}, "evds", report)
                report.messages.append(f"CPI (EVDS {evds.CPI_SERIES}): {values[0][0]:%Y-%m} – {values[-1][0]:%Y-%m}")
                return report
            report.warnings.append("EVDS returned an empty CPI series; using the TCMB table.")
    changes = tcmb_cpi.fetch_cpi_table(client)
    _store_cpi(session, cpi_from_changes(changes), "tcmb-table", report)
    report.messages.append(f"CPI (TCMB table, chained): {changes[0].month:%Y-%m} – {changes[-1].month:%Y-%m}")
    return report


REFRESH_DAYS = 7


def priced_by(session: Session, source: str) -> list[str]:
    """Commodities whose prices come from ``source``."""
    return list(session.scalars(select(Commodity.code).where(Commodity.price_source == source).order_by(Commodity.code)))


def _fetch_from(session: Session, code: str, source: str, start: date) -> date:
    """``start``, or a week before the last stored price when ``start`` is already covered."""
    first, last = session.execute(
        select(func.min(Price.date), func.max(Price.date)).where(Price.commodity_code == code, Price.source == source)
    ).one()
    if first is None or start < first:
        return start
    return max(start, last - timedelta(days=REFRESH_DAYS))


def update_tefas(
    session: Session,
    client: httpx.Client,
    start: date,
    end: date,
    today: date | None = None,
    pause: float = 0.3,
    progress: Progress | None = None,
) -> UpdateReport:
    report = UpdateReport()
    today = today or date.today()
    codes = priced_by(session, "tefas")
    for i, code in enumerate(codes):
        if pause and i:
            time.sleep(pause)
        fund = tefas.fund_code(code)
        if progress:
            progress(f"TEFAS {fund}")
        since = _fetch_from(session, code, "tefas", start)
        oldest = tefas.months_back(today, tefas.PERIODS[-1])
        if since < oldest:
            report.warnings.append(f"TEFAS gives five years of prices: {code} gets none before {oldest}.")
        try:
            rows = tefas.fetch_fund_prices(client, fund, since, today)
        except SourceError as exc:
            report.warnings.append(f"{code}: {exc}")
            continue
        if not rows:
            report.warnings.append(f"TEFAS has no prices for {fund} since {since}; is the fund code right?")
        for day, price in rows:
            if day <= end:
                report.count(upsert_price(session, code, "TRY", day, price, "tefas"))
    if codes:
        report.messages.append(f"Funds (TEFAS): {', '.join(codes)}")
    return report


def _held_on(session: Session, code: str, on: date) -> bool:
    """True if asset accounts held some of ``code`` at the end of the day before ``on``."""
    amounts = session.scalars(
        select(Posting.amount)
        .join(Transaction)
        .join(Account, Account.id == Posting.account_id)
        .where(Posting.commodity_code == code, Transaction.date < on, Account.type == "asset")
    )
    return sum(amounts, Decimal(0)) != 0


def _split_recorded(session: Session, code: str, on: date) -> bool:
    return (
        session.scalar(
            select(func.count())
            .select_from(Transaction)
            .join(Posting)
            .where(
                Transaction.source == SPLIT_SOURCE,
                Posting.commodity_code == code,
                Transaction.date >= on - timedelta(days=REFRESH_DAYS),
                Transaction.date <= on + timedelta(days=REFRESH_DAYS),
            )
        )
        > 0
    )


def update_yahoo(
    session: Session,
    client: httpx.Client,
    start: date,
    end: date,
    today: date | None = None,
    pause: float = 0.3,
    progress: Progress | None = None,
) -> UpdateReport:
    report = UpdateReport()
    codes = priced_by(session, "yahoo")
    for i, code in enumerate(codes):
        if pause and i:
            time.sleep(pause)
        symbol = yahoo.symbol_of(code)
        if progress:
            progress(f"Yahoo {symbol}")
        since = _fetch_from(session, code, "yahoo", start)
        try:
            chart = yahoo.fetch_chart(client, symbol, since, end, today)
        except SourceError as exc:
            report.warnings.append(f"{code}: {exc}")
            continue
        if session.get(Commodity, chart.currency) is None:
            report.warnings.append(f"{code} is quoted in {chart.currency}, which is not a commodity of the ledger.")
            continue
        for day, price in chart.prices:
            report.count(upsert_price(session, code, chart.currency, day, price, "yahoo"))
        for split in chart.splits:
            if split.on >= since and _held_on(session, code, split.on) and not _split_recorded(session, code, split.on):
                report.warnings.append(
                    f"{code}: a {split.ratio.normalize():f}-for-1 split on {split.on}. If the ledger still has the old "
                    f"number of shares, record the new ones: finance split ACCOUNT {split.ratio.normalize():f} --date {split.on}"
                )
    if codes:
        report.messages.append(f"Shares (Yahoo Finance): {', '.join(codes)}")
    return report


SOURCES = ("fx", "gold", "cpi", "tefas", "yahoo")


def update_range(session: Session, start: date | None, end: date | None, today: date) -> tuple[date, date]:
    """The days to update: from ``start`` (default: the first transaction, or 30 days ago) to ``end`` (today)."""
    from financeproject.ledger.queries import transaction_date_range

    first, _ = transaction_date_range(session)
    start_day = start or first or today - timedelta(days=30)
    end_day = end or today
    if end_day < start_day:
        raise SourceError("The end date is before the start date.")
    return start_day, end_day


def update_all(
    session: Session,
    client: httpx.Client,
    start: date,
    end: date,
    *,
    only: str | None = None,
    api_key: str | None = None,
    today: date | None = None,
    progress: Progress | None = None,
) -> UpdateReport:
    """Every source in turn (or ``only`` one), committing after each."""
    if only is not None and only not in SOURCES:
        raise SourceError(f"Unknown price source {only!r}; one of: {', '.join(SOURCES)}")
    today = today or date.today()
    report = UpdateReport()
    if only in (None, "fx"):
        report.merge(update_fx(session, client, start, end, tcmb_currencies(session), api_key, progress=progress))
        session.commit()
    if only in (None, "gold"):
        report.merge(update_gold(session, client, start, end, progress=progress))
        session.commit()
    if only in (None, "cpi"):
        if progress:
            progress("CPI")
        report.merge(update_cpi(session, client, api_key, today))
        session.commit()
    if only in (None, "tefas"):
        report.merge(update_tefas(session, client, start, end, today, progress=progress))
        session.commit()
    if only in (None, "yahoo"):
        report.merge(update_yahoo(session, client, start, end, today, progress=progress))
        session.commit()
    return report
