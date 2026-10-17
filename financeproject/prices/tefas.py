"""Investment fund prices from TEFAS (Türkiye Elektronik Fon Alım Satım Platformu).

The site's JSON API (in use since 2026) gives the daily prices of one fund
for a fixed look-back of 1, 3, 6, 12, 36 or 60 months; nothing older than
five years:

    POST https://www.tefas.gov.tr/api/funds/fonFiyatBilgiGetir
    {"fonKodu": "AFT", "dil": "TR", "periyod": 3}
    → {"errorCode": null, "errorMessage": null,
       "resultList": [{"fonKodu": "AFT", "fonUnvan": "…", "tarih": "2026-05-04", "fiyat": 1.234567, …}, …]}

Prices are TRY per share, with up to six decimals; they are read as decimals,
never as floats. An unknown code answers with an empty list. A fund is the
commodity ``TEFAS.<code>`` with the price source ``tefas``.
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation

import httpx

from financeproject.core.dates import add_months
from financeproject.errors import SourceError

URL = "https://www.tefas.gov.tr/api/funds/fonFiyatBilgiGetir"
PERIODS = (1, 3, 6, 12, 36, 60)
PREFIX = "TEFAS."
MARGIN_DAYS = 7


def fund_code(commodity: str) -> str:
    """``TEFAS.AFT`` → ``AFT``."""
    return commodity[len(PREFIX) :] if commodity.upper().startswith(PREFIX) else commodity


def period_for(start: date, today: date) -> int:
    """The shortest look-back (months) that reaches a week before ``start``; at most five years."""
    for months in PERIODS:
        if months_back(today, months) <= start - timedelta(days=MARGIN_DAYS):
            return months
    return PERIODS[-1]


def months_back(today: date, months: int) -> date:
    """The same day ``months`` months earlier (the last day of a shorter month)."""
    first = add_months(today, -months)
    day = today.day
    while True:
        try:
            return first.replace(day=day)
        except ValueError:
            day -= 1


def parse_prices(payload: object, code: str) -> list[tuple[date, Decimal]]:
    """The fund's (day, price) pairs from an answer."""
    if not isinstance(payload, dict):
        raise SourceError("Unexpected answer from TEFAS (not an object).")
    if payload.get("errorCode") or payload.get("errorMessage"):
        raise SourceError(f"TEFAS error: {payload.get('errorMessage') or payload.get('errorCode')}")
    out = []
    for row in payload.get("resultList") or []:
        try:
            if str(row.get("fonKodu", "")).upper() != code.upper():
                continue
            price = Decimal(str(row["fiyat"]))
            if price > 0:
                out.append((date.fromisoformat(str(row["tarih"])[:10]), price))
        except (KeyError, TypeError, ValueError, InvalidOperation, AttributeError) as exc:
            raise SourceError(f"Cannot read a TEFAS row: {row!r}") from exc
    return sorted(out)


def fetch_fund_prices(client: httpx.Client, code: str, start: date, today: date) -> list[tuple[date, Decimal]]:
    """Daily prices of the fund ``code`` from ``start`` (at most five years back) to the latest."""
    body = {"fonKodu": code.upper(), "dil": "TR", "periyod": period_for(start, today)}
    try:
        response = client.post(URL, json=body, headers={"Accept": "application/json"})
    except httpx.HTTPError as exc:
        raise SourceError(f"TEFAS cannot be reached: {exc}") from exc
    if response.status_code != 200:
        raise SourceError(f"TEFAS returned {response.status_code} for {code}: {response.text[:200]}")
    try:
        payload = json.loads(response.text, parse_float=Decimal)
    except ValueError as exc:
        raise SourceError("The TEFAS answer is not JSON.") from exc
    return [(d, p) for d, p in parse_prices(payload, code) if d >= start]
