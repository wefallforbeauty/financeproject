"""Indicative exchange rates of the Central Bank of the Republic of Türkiye
(CBRT, Turkish TCMB): the daily XML bulletin, no key needed.

TCMB publishes a bulletin around 15:30 on business days:
``https://www.tcmb.gov.tr/kurlar/today.xml`` and, for past days,
``https://www.tcmb.gov.tr/kurlar/YYYYMM/DDMMYYYY.xml`` (404 on weekends and
holidays). Each currency has forex and banknote buying/selling rates per
``Unit`` (100 for JPY).

The ledger values foreign currency at the forex buying rate (``ForexBuying``):
what the holder would get for it. The bulletin date is the price date.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

import httpx

from financeproject.errors import SourceError

TODAY_URL = "https://www.tcmb.gov.tr/kurlar/today.xml"
ARCHIVE_URL = "https://www.tcmb.gov.tr/kurlar/{d:%Y%m}/{d:%d%m%Y}.xml"
RATE_FIELDS = ("ForexBuying", "ForexSelling", "BanknoteBuying", "BanknoteSelling")
DEFAULT_FIELD = "ForexBuying"


@dataclass(frozen=True, slots=True)
class CurrencyRate:
    code: str
    unit: int
    name: str
    rates: dict[str, Decimal | None]

    def per_unit(self, field: str = DEFAULT_FIELD) -> Decimal | None:
        """TRY price of one unit of the currency."""
        value = self.rates.get(field)
        return None if value is None else value / self.unit


@dataclass(frozen=True, slots=True)
class Bulletin:
    date: date
    number: str
    currencies: dict[str, CurrencyRate]

    def price(self, code: str, field: str = DEFAULT_FIELD) -> Decimal | None:
        c = self.currencies.get(code)
        return c.per_unit(field) if c else None


def _decimal(text: str | None) -> Decimal | None:
    if text is None or not text.strip():
        return None
    try:
        return Decimal(text.strip())
    except InvalidOperation as exc:
        raise SourceError(f"Cannot read a number in the TCMB XML: {text!r}") from exc


def parse_bulletin(xml: bytes | str) -> Bulletin:
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as exc:
        raise SourceError(f"Cannot read the TCMB XML: {exc}") from exc
    if root.tag != "Tarih_Date" or not root.get("Tarih"):
        raise SourceError("Not a TCMB exchange rate bulletin (no Tarih_Date element).")
    try:
        day = datetime.strptime(root.get("Tarih", ""), "%d.%m.%Y").date()
    except ValueError as exc:
        raise SourceError(f"Cannot read the TCMB bulletin date: {root.get('Tarih')!r}") from exc
    currencies: dict[str, CurrencyRate] = {}
    for node in root.findall("Currency"):
        code = (node.get("CurrencyCode") or node.get("Kod") or "").strip().upper()
        if not code:
            continue
        unit_text = (node.findtext("Unit") or "1").strip()
        try:
            unit = int(unit_text)
        except ValueError as exc:
            raise SourceError(f"Cannot read the TCMB unit: {unit_text!r} ({code})") from exc
        currencies[code] = CurrencyRate(
            code=code,
            unit=unit,
            name=(node.findtext("Isim") or "").strip(),
            rates={f: _decimal(node.findtext(f)) for f in RATE_FIELDS},
        )
    return Bulletin(day, root.get("Bulten_No", ""), currencies)


def fetch_bulletin(client: httpx.Client, day: date | None = None) -> Bulletin | None:
    """Bulletin of ``day`` (``None``: the latest). ``None`` when there is no bulletin that day."""
    url = TODAY_URL if day is None else ARCHIVE_URL.format(d=day)
    response = client.get(url)
    if response.status_code == 404:
        return None
    if response.status_code != 200:
        raise SourceError(f"TCMB returned {response.status_code}: {url}")
    bulletin = parse_bulletin(response.content)
    if day is not None and bulletin.date != day:
        raise SourceError(f"TCMB returned the bulletin of {bulletin.date} for {day}.")
    return bulletin
