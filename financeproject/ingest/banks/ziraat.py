"""Ziraat Bankası account statements ("Hesap Hareketleri"), as .xlsx or .html.

Both downloads hold the same table, newest line first:

======== ======== ========================== ======================== ========
Tarih    Fiş No   Açıklama                   İşlem Tutarı (or Tutar)  Bakiye
======== ======== ========================== ======================== ========
date     receipt  description                signed amount            balance
======== ======== ========================== ======================== ========

- Dates are written ``DD.MM.YYYY``.
- In the .xlsx file amounts are numbers; in the .html file they are text in
  the Turkish format (``-1.234,56``).
- Above the table: the period (``01.09.2026 - 30.09.2026``) and the account's
  currency (the last word of the account number line in .xlsx, ``Döviz Cinsi``
  in .html).
- Below it, the totals: ``Borç`` (sum of money out) and ``Alacak`` (sum of
  money in).
- The .html page starts with a bot-protection script, which is skipped.

Card purchases name the merchant (``İŞYERİ: … MUTABAKAT: …``) and mobile
transfers start with the other person's name; both become the payee.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from financeproject.core.money import parse_decimal
from financeproject.core.text import fold
from financeproject.errors import FinanceError
from financeproject.ingest.statement import (
    Statement,
    StatementLine,
    check_totals,
    chronological,
)
from financeproject.ingest.tables import Cell

NAME = "Ziraat Bankası"
KEY = "ziraat"
SUFFIXES = (".xlsx", ".html", ".htm")

_HEADERS = {
    "tarih": "date",
    "fis no": "reference",
    "aciklama": "description",
    "islem tutari": "amount",
    "tutar": "amount",
    "bakiye": "balance",
}
_REQUIRED = {"date", "reference", "description", "amount", "balance"}
_DATE = re.compile(r"\d{1,2}\.\d{1,2}\.\d{4}")
_PERIOD = re.compile(r"(\d{1,2}\.\d{1,2}\.\d{4})\s*-\s*(\d{1,2}\.\d{1,2}\.\d{4})")
_TOTAL = re.compile(r"^(borc|alacak)\s*:\s*(.*)$")
_MERCHANT = re.compile(r"(?:İŞYERİ|ISYERI)\s*:\s*(.+?)\s*(?:MUTABAKAT\s*:|$)", re.IGNORECASE)
_MOBILE_TRANSFER = re.compile(r"^(.+?)\s+Ziraat Mobil Havale\b", re.IGNORECASE)
_CURRENCY_WORDS = {"TL": "TRY", "TRY": "TRY", "USD": "USD", "EUR": "EUR", "GBP": "GBP", "CHF": "CHF", "JPY": "JPY"}


def _text(cell: Cell) -> str:
    if cell is None:
        return ""
    return " ".join(str(cell).split())


def _header(rows: list[list[Cell]]) -> tuple[int, dict[str, int]] | None:
    for i, row in enumerate(rows):
        columns: dict[str, int] = {}
        for j, cell in enumerate(row):
            name = _HEADERS.get(fold(_text(cell)))
            if name and name not in columns:
                columns[name] = j
        if _REQUIRED <= columns.keys():
            return i, columns
    return None


def sniff(rows: list[list[Cell]]) -> bool:
    """True if the rows look like a Ziraat Bankası statement."""
    return _header(rows) is not None


def _cell(row: list[Cell], index: int) -> Cell:
    return row[index] if index < len(row) else None


def _date(cell: Cell) -> date | None:
    if isinstance(cell, datetime):
        return cell.date()
    if isinstance(cell, date):
        return cell
    text = _text(cell)
    if not _DATE.fullmatch(text):
        return None
    return datetime.strptime(text, "%d.%m.%Y").date()


def _amount(cell: Cell) -> Decimal | None:
    """Numbers come as floats from .xlsx and as Turkish text (``-1.234,56``) from .html."""
    if cell is None or cell == "":
        return None
    if isinstance(cell, bool):
        raise FinanceError(f"Unexpected value in an amount column: {cell!r}")
    if isinstance(cell, int):
        return Decimal(cell).quantize(Decimal("0.01"))
    if isinstance(cell, float):
        value = Decimal(repr(cell))
        rounded = value.quantize(Decimal("0.01"))
        if abs(value - rounded) > Decimal("0.000001"):
            raise FinanceError(f"An amount has more than two decimals: {cell!r}")
        return rounded
    text = _text(cell)
    if not text:
        return None
    try:
        return parse_decimal(text, decimal_sep=",").quantize(Decimal("0.01"))
    except (FinanceError, InvalidOperation) as exc:
        raise FinanceError(f"Cannot read the amount {text!r}") from exc


def payee_of(description: str) -> str | None:
    """The merchant of a card purchase, or the other person of a mobile transfer."""
    if m := _MERCHANT.search(description):
        return " ".join(m.group(1).split()) or None
    if m := _MOBILE_TRANSFER.match(description.strip()):
        return " ".join(m.group(1).split()) or None
    return None


def _currency(rows: list[list[Cell]]) -> str | None:
    for row in rows:
        texts = [_text(c) for c in row if _text(c)]
        for k, text in enumerate(texts):
            label = fold(text)
            if label in ("doviz cinsi", "hesap numarasi", "musteri/hesap no"):
                values = [t for t in texts[k + 1 :] if t != ":"]
                if values:
                    word = values[0].split()[-1].upper()
                    if word in _CURRENCY_WORDS:
                        return _CURRENCY_WORDS[word]
    return None


def _period(rows: list[list[Cell]]) -> tuple[date, date] | None:
    for row in rows:
        for cell in row:
            if m := _PERIOD.search(_text(cell)):
                start = datetime.strptime(m.group(1), "%d.%m.%Y").date()
                end = datetime.strptime(m.group(2), "%d.%m.%Y").date()
                if start <= end:
                    return start, end
    return None


def _totals(row: list[Cell], amount_column: int) -> dict[str, Decimal]:
    """``Borç:-1.234,56`` in one cell (.xlsx), or ``Borç:`` with the number in the amount column (.html)."""
    out: dict[str, Decimal] = {}
    for cell in row:
        if m := _TOTAL.match(fold(_text(cell))):
            kind = "debit" if m.group(1) == "borc" else "credit"
            raw = _text(cell).split(":", 1)[1].strip()
            value = _amount(raw) if raw else _amount(_cell(row, amount_column))
            if value is not None:
                out[kind] = value
    return out


def parse(rows: list[list[Cell]], file_format: str) -> Statement:
    found = _header(rows)
    if found is None:
        raise FinanceError(f"Not a {NAME} statement: the Tarih / Fiş No / Açıklama / Tutar / Bakiye header is missing.")
    header_row, columns = found
    lines: list[StatementLine] = []
    totals: dict[str, Decimal] = {}
    for row in rows[header_row + 1 :]:
        on = _date(_cell(row, columns["date"]))
        if on is None:
            totals.update(_totals(row, columns["amount"]))
            continue
        if totals:
            raise FinanceError("Statement lines continue after the totals; the file layout is not the expected one.")
        description = _text(_cell(row, columns["description"]))
        amount = _amount(_cell(row, columns["amount"]))
        if amount is None or amount == 0:
            raise FinanceError(f"A line of {on} has no amount: {description[:60]!r}")
        lines.append(
            StatementLine(
                date=on,
                description=description or "(no description)",
                amount=amount,
                balance=_amount(_cell(row, columns["balance"])),
                reference=_text(_cell(row, columns["reference"])) or None,
                payee=payee_of(description),
            )
        )
    if not lines:
        raise FinanceError(f"The {NAME} statement has no lines.")
    statement = Statement(
        bank=NAME,
        file_format=file_format,
        lines=tuple(chronological(lines)),
        currency=_currency(rows[:header_row]),
        period=_period(rows[:header_row]),
        debit_total=totals.get("debit"),
        credit_total=totals.get("credit"),
    )
    check_totals(statement)
    return statement
