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

The description tells the kind of most lines (``StatementLine.kind``):

- ``card``: card purchases name the merchant (``İŞYERİ: … MUTABAKAT: …``),
  which becomes the payee;
- ``transfer``: mobile transfers (``<name> Ziraat Mobil Havale``, the name
  becomes the payee) and FAST transfers;
- ``fee``: the bank's charges (``MESAJ ÜCRETİ TUTARI``, ``KOMİSYON TUTARI``
  and the ``BSMV TUTARI`` tax on them), payee Ziraat Bankası;
- ``cash``: ATM withdrawals and deposits, payee ATM;
- ``bill``: bill payments with a subscriber number (``<company>/ Abone no:
  …``), the company becomes the payee;
- ``loan``: loan installments (``<loan number> hesap <n>. taksit ödeme
  tutarı``), with the loan's number and the installment's number, so that the
  import can split them into principal, interest and taxes.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
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
_LOAN_INSTALLMENT = re.compile(r"^\s*(\S+)\s+hesap\s+(\d+)\s*\.\s*taksit ödeme tutarı\s*$", re.IGNORECASE)
_FEES = {"mesaj ucreti tutari", "komisyon tutari", "bsmv tutari"}
_SUBSCRIBER = re.compile(r"\bABONE\s+NO\b", re.IGNORECASE)
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


@dataclass(frozen=True)
class Reading:
    kind: str | None = None
    payee: str | None = None
    loan_reference: str | None = None
    installment: int | None = None


def read_description(description: str) -> Reading:
    """What a description says: the kind of line, the payee, and for loans the numbers."""
    text = " ".join(description.split())
    folded = fold(text)
    if m := _LOAN_INSTALLMENT.match(text):
        return Reading("loan", f"Loan {m.group(1)}", m.group(1), int(m.group(2)))
    if folded in _FEES:
        return Reading("fee", NAME)
    if m := _MERCHANT.search(text):
        return Reading("card", " ".join(m.group(1).split()) or None)
    if m := _MOBILE_TRANSFER.match(text):
        return Reading("transfer", " ".join(m.group(1).split()) or None)
    if folded.startswith("atm"):
        return Reading("cash", "ATM")
    if m := _SUBSCRIBER.search(text):
        company = text[: m.start()].split("/", 1)[0].strip(" /-:")
        return Reading("bill", company or None)
    if re.search(r"(?<![a-z])fast(?![a-z])", folded):
        return Reading("transfer")
    return Reading()


def payee_of(description: str) -> str | None:
    """The payee the template reads from a description, if any."""
    return read_description(description).payee


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
        reading = read_description(description)
        lines.append(
            StatementLine(
                date=on,
                description=description or "(no description)",
                amount=amount,
                balance=_amount(_cell(row, columns["balance"])),
                reference=_text(_cell(row, columns["reference"])) or None,
                payee=reading.payee,
                kind=reading.kind,
                loan_reference=reading.loan_reference,
                installment=reading.installment,
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
