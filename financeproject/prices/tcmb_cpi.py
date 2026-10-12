"""CPI changes from the TCMB inflation page (no key needed).

TCMB lists the CPI of TurkStat (TÜİK; the Turkish acronym is TÜFE) as annual and
monthly % changes, from 2005-01:
https://www.tcmb.gov.tr/wps/wcm/connect/TR/TCMB+TR/Main+Menu/Istatistikler/Enflasyon+Verileri/Tuketici+Fiyatlari

The index levels are rebuilt by chaining the monthly changes
(``core.inflation.chain_index``). The changes are rounded to 0.01 points, so the
chained index drifts slightly from TÜİK's own levels; the annual changes on
the same page measure that drift (see the tests). With an EVDS key the official
levels are used instead.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser

import httpx

from financeproject.errors import SourceError

URL = (
    "https://www.tcmb.gov.tr/wps/wcm/connect/TR/TCMB+TR/Main+Menu/"
    "Istatistikler/Enflasyon+Verileri/Tuketici+Fiyatlari"
)
_MONTH = re.compile(r"(\d{2})-(\d{4})")


@dataclass(frozen=True, slots=True)
class CpiChange:
    month: date
    annual_pct: Decimal
    monthly_pct: Decimal


class _TableCells(HTMLParser):
    """Collects the text of every table cell, row by row."""

    def __init__(self) -> None:
        super().__init__()
        self.rows: list[list[str]] = []
        self._row: list[str] | None = None
        self._cell: list[str] | None = None

    def handle_starttag(self, tag, attrs):
        if tag == "tr":
            self._row = []
        elif tag in ("td", "th") and self._row is not None:
            self._cell = []

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self._row is not None and self._cell is not None:
            self._row.append(" ".join("".join(self._cell).split()))
            self._cell = None
        elif tag == "tr" and self._row is not None:
            self.rows.append(self._row)
            self._row = None

    def handle_data(self, data):
        if self._cell is not None:
            self._cell.append(data)


def _number(text: str) -> Decimal:
    try:
        return Decimal(text.replace(",", "."))
    except InvalidOperation as exc:
        raise SourceError(f"Cannot read a number in the CPI table: {text!r}") from exc


def parse_cpi_table(html: str) -> list[CpiChange]:
    """Rows ``MM-YYYY | annual % | monthly %``, oldest first."""
    parser = _TableCells()
    parser.feed(html)
    out: dict[date, CpiChange] = {}
    for row in parser.rows:
        if len(row) < 3 or not (m := _MONTH.fullmatch(row[0])):
            continue
        month = date(int(m[2]), int(m[1]), 1)
        out[month] = CpiChange(month, _number(row[1]), _number(row[2]))
    if not out:
        raise SourceError("No CPI table on the TCMB page (the page layout may have changed).")
    return [out[m] for m in sorted(out)]


def fetch_cpi_table(client: httpx.Client) -> list[CpiChange]:
    response = client.get(URL)
    if response.status_code != 200:
        raise SourceError(f"The TCMB inflation page returned {response.status_code}.")
    return parse_cpi_table(response.text)
