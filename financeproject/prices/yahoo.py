"""Share prices from Yahoo Finance's chart API, for Borsa İstanbul shares.

The API is free and needs no key, but it is not an official, documented
service and may change:

    GET https://query1.finance.yahoo.com/v8/finance/chart/THYAO.IS
        ?period1=<unix time>&period2=<unix time>&interval=1d&events=split
    → chart.result[0]: meta.currency, meta.gmtoffset, timestamp[],
      indicators.quote[0].close[], events.splits {time: {numerator, denominator}}

Yahoo's closes are adjusted for later splits: after a bonus issue
("bedelsiz sermaye artırımı") of 100 % every earlier close is halved. The
ledger holds the number of shares of each day, so the closes are turned back
into the prices of their day: close × Π numerator / denominator of the
splits after it. A share is the commodity ``BIST.<ticker>`` with the price
source ``yahoo``; another code is taken as a Yahoo symbol as it is.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation

import httpx

from financeproject.errors import SourceError

URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
PREFIX = "BIST."
BIST_DECIMALS = Decimal("0.01")
OTHER_DECIMALS = Decimal("0.0001")


def symbol_of(commodity: str) -> str:
    """``BIST.THYAO`` → ``THYAO.IS``."""
    upper = commodity.upper()
    return f"{commodity[len(PREFIX):]}.IS" if upper.startswith(PREFIX) else commodity


@dataclass(frozen=True)
class Split:
    on: date
    ratio: Decimal
    """New shares per old share: 2 for a 100 % bonus issue."""


@dataclass(frozen=True)
class Chart:
    currency: str
    prices: list[tuple[date, Decimal]]
    """Closes as they were on their day (not split-adjusted)."""
    splits: list[Split]


def _day(timestamp: int, offset: int) -> date:
    return datetime.fromtimestamp(timestamp + offset, UTC).date()


def parse_chart(payload: object, symbol: str) -> Chart:
    try:
        chart = payload["chart"]
        if chart.get("error"):
            error = chart["error"]
            raise SourceError(f"Yahoo has no data for {symbol}: {error.get('description') or error}")
        result = chart["result"][0]
        meta = result["meta"]
        offset = int(meta.get("gmtoffset") or 0)
        currency = str(meta.get("currency") or "").upper()
        stamps = result.get("timestamp") or []
        closes = result["indicators"]["quote"][0].get("close") or []
        splits = [
            Split(_day(int(s["date"]), offset), Decimal(str(s["numerator"])) / Decimal(str(s["denominator"])))
            for s in (result.get("events") or {}).get("splits", {}).values()
        ]
    except (KeyError, IndexError, TypeError, ValueError, InvalidOperation, ZeroDivisionError) as exc:
        raise SourceError(f"Unexpected answer from Yahoo for {symbol}.") from exc
    if not currency:
        raise SourceError(f"Yahoo gives no currency for {symbol}.")
    places = BIST_DECIMALS if symbol.upper().endswith(".IS") else OTHER_DECIMALS
    splits.sort(key=lambda s: s.on)
    prices = {}
    for stamp, close in zip(stamps, closes):
        if close is None:
            continue
        on = _day(int(stamp), offset)
        factor = Decimal(1)
        for split in splits:
            if split.on > on:
                factor *= split.ratio
        value = (Decimal(str(close)) * factor).quantize(places)
        if value > 0:
            prices[on] = value
    return Chart(currency, sorted(prices.items()), splits)


def fetch_chart(client: httpx.Client, symbol: str, start: date, end: date, today: date | None = None) -> Chart:
    """Daily closes of ``symbol`` from ``start`` to ``end``, with the splits since ``start``.

    The request always runs to ``today``: the closes are adjusted for every
    split up to now, so all of them are needed to undo the adjustment.
    """
    period1 = int(datetime(start.year, start.month, start.day, tzinfo=UTC).timestamp())
    stop = max(end, today or date.today()) + timedelta(days=1)
    period2 = int(datetime(stop.year, stop.month, stop.day, tzinfo=UTC).timestamp())
    params = {"period1": period1, "period2": period2, "interval": "1d", "events": "split"}
    try:
        response = client.get(URL.format(symbol=symbol), params=params)
    except httpx.HTTPError as exc:
        raise SourceError(f"Yahoo cannot be reached: {exc}") from exc
    if response.status_code == 404:
        raise SourceError(f"Yahoo does not know the symbol {symbol}.")
    if response.status_code != 200:
        raise SourceError(f"Yahoo returned {response.status_code} for {symbol}: {response.text[:200]}")
    try:
        payload = json.loads(response.text)
    except ValueError as exc:
        raise SourceError("The Yahoo answer is not JSON.") from exc
    chart = parse_chart(payload, symbol)
    return Chart(chart.currency, [(d, p) for d, p in chart.prices if start <= d <= end], chart.splits)
