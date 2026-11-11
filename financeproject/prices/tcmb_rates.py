"""The policy rate of the TCMB from its one-week repo page (no key needed).

TCMB lists the one-week repo auction rate, its policy rate since May 2010,
with the day each rate took effect:
https://www.tcmb.gov.tr/wps/wcm/connect/TR/TCMB+TR/Main+Menu/Temel+Faaliyetler/Para+Politikasi/Merkez+Bankasi+Faiz+Oranlari/1+Hafta+Repo

Rows read ``DD.MM.YYYY | borrowing | lending``; the lending rate is the one
the bank funds the market at, in per cent a year (simple). It is the
risk-free benchmark of the investment reports (``core.rates``).
"""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal, InvalidOperation

import httpx

from financeproject.core.htmltable import table_rows
from financeproject.errors import SourceError

URL = (
    "https://www.tcmb.gov.tr/wps/wcm/connect/TR/TCMB+TR/Main+Menu/Temel+Faaliyetler/"
    "Para+Politikasi/Merkez+Bankasi+Faiz+Oranlari/1+Hafta+Repo"
)
_DAY = re.compile(r"(\d{2})\.(\d{2})\.(\d{4})")


def parse_rate_table(html: str) -> list[tuple[date, Decimal]]:
    """``(day the rate took effect, rate in % a year)``, oldest first."""
    out: dict[date, Decimal] = {}
    for row in table_rows(html):
        if len(row) < 3 or not (m := _DAY.fullmatch(row[0])):
            continue
        try:
            rate = Decimal(row[2].replace(",", "."))
        except InvalidOperation:
            continue
        if rate > 0:
            out[date(int(m[3]), int(m[2]), int(m[1]))] = rate
    if not out:
        raise SourceError("No rate table on the TCMB page (the page layout may have changed).")
    return sorted(out.items())


def fetch_policy_rates(client: httpx.Client) -> list[tuple[date, Decimal]]:
    try:
        response = client.get(URL)
    except httpx.HTTPError as exc:
        raise SourceError(f"The TCMB page cannot be reached: {exc}") from exc
    if response.status_code != 200:
        raise SourceError(f"The TCMB interest rate page returned {response.status_code}.")
    return parse_rate_table(response.text)
