"""Ziraat Bankası credit card transactions ("Kart Hareketleri"), as .pdf.

Internet banking prints the card's transactions as a web page saved as a PDF.
Every transaction is a block:

=================== ===================================== ==============
left                middle                                right
=================== ===================================== ==============
day and month       description (one or two lines)        amount and TL
year                installment note, if any
=================== ===================================== ==============

- Dates are a day, a short Turkish month name and the year on the next line
  (``17 Eki`` / ``2026``).
- Spending has no sign; payments and refunds have ``+``. Amounts are written
  ``1.234,56``.
- An installment purchase shows the installment of the month. The description
  starts with the purchase day (``03/09 SHOP …``) and a note gives the
  purchase total and which installment this is:
  ``(6.000,00 TL İşlemin 2/6 Taksidi)``.
- Bills paid by the card end in ``FATURA ÖDEME``; a payment of the card reads
  ``Ödeme - Teşekkür Ederiz``.
- Shop descriptions end with the place (province, sometimes a plate-code
  abbreviation before it); the payee is the description without it.

Everything else on the page (the account holder's name above the list,
buttons such as ``TAKSİTLENDİR``, the bank's address below) is skipped. The
list has no balances or totals, so the lines cannot be checked against them.

Kinds (``StatementLine.kind``): ``card`` (spending), ``installment``,
``bill``, ``payment`` (of the card) and ``refund``.
"""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal

from financeproject.core.dates import add_months, month_start
from financeproject.core.money import parse_decimal
from financeproject.core.text import fold
from financeproject.errors import FinanceError
from financeproject.ingest.pdf import TextPiece, line_text, lines, top_down
from financeproject.ingest.statement import Statement, StatementLine

NAME = "Ziraat Bankası"
KEY = "ziraat-card"
INPUT = "pdf"
SUFFIXES = (".pdf",)

_AMOUNT = re.compile(r"^([+-]?)(\d{1,3}(?:\.\d{3})*,\d{2})(?:\s+(\S+))?$")
_DAY_MONTH = re.compile(r"^(\d{1,2})\s+(\w{3})$")
_YEAR = re.compile(r"^(\d{4})$")
_NOTE = re.compile(r"^\(\s*(\d{1,3}(?:\.\d{3})*,\d{2})\s*tl\s+islemin\s+(\d+)\s*/\s*(\d+)\s+taksidi\s*\)$")
_PURCHASE_DAY = re.compile(r"^(\d{2})/(\d{2})(?:\s+|$)")
_PLATE = re.compile(r"^\d{2}\.[^\W\d_]+$")
_MONTHS = {
    "oca": 1, "sub": 2, "mar": 3, "nis": 4, "may": 5, "haz": 6,
    "tem": 7, "agu": 8, "eyl": 9, "eki": 10, "kas": 11, "ara": 12,
}
PROVINCES = frozenset(
    """adana adiyaman afyonkarahisar afyon agri aksaray amasya ankara antalya ardahan artvin aydin balikesir bartin
    batman bayburt bilecik bingol bitlis bolu burdur bursa canakkale cankiri corum denizli diyarbakir duzce edirne
    elazig erzincan erzurum eskisehir gaziantep giresun gumushane hakkari hatay igdir isparta istanbul izmir
    kahramanmaras karabuk karaman kars kastamonu kayseri kilis kirikkale kirklareli kirsehir kocaeli konya kutahya
    malatya manisa mardin mersin mugla mus nevsehir nigde ordu osmaniye rize sakarya samsun sanliurfa siirt sinop
    sirnak sivas tekirdag tokat trabzon tunceli usak van yalova yozgat zonguldak""".split()
)
"""Turkey's 81 provinces, folded (``core.text.fold``)."""


def _text(pages: list[list[TextPiece]]) -> list[str]:
    return [fold(line_text(line)) for pieces in pages for line in lines(pieces)]


def sniff(pages: list[list[TextPiece]]) -> bool:
    """True for a Ziraat Bankası card transaction list."""
    texts = _text(pages)
    return any("kart hareketleri" in t for t in texts) and any("ziraatbank" in t for t in texts)


def _amount(sign: str, digits: str) -> Decimal:
    value = parse_decimal(digits, decimal_sep=",")
    return value if sign == "+" else -value


def _currency(anchor: TextPiece, inline: str | None, ordered: list[TextPiece]) -> str | None:
    """The currency word written with the amount or just right of it; ``None``: not an amount of the list."""
    if inline:
        return inline
    right = sorted((p for p in ordered if abs(p.y - anchor.y) <= 1 and p.x > anchor.x), key=lambda p: p.x)
    word = right[0].text.split()[0] if right else ""
    return word if re.fullmatch(r"[A-Z]{2,3}", word) else None


def _purchase_date(description: str, on: date, number: int) -> date | None:
    """The purchase day written before the description, in the year that fits ``number``."""
    m = _PURCHASE_DAY.match(description)
    if not m:
        return None
    day, month = int(m.group(1)), int(m.group(2))
    expected = add_months(month_start(on), -(number - 1))
    year = expected.year if month == expected.month else (on.year if month <= on.month else on.year - 1)
    try:
        return date(year, month, day)
    except ValueError:
        return None


def payee_of(description: str) -> str | None:
    """The shop: the description without the purchase day before it and the place after it."""
    words = description.split()
    if words and _PURCHASE_DAY.match(words[0]):
        words = words[1:]
    kept = list(words)
    if len(kept) >= 2 and fold(kept[-2]) in PROVINCES:
        kept = kept[:-2]
    elif kept and fold(kept[-1]) in PROVINCES:
        kept = kept[:-1]
    if kept and _PLATE.match(kept[-1]):
        kept = kept[:-1]
    return " ".join(kept or words) or None


def read_description(description: str, amount: Decimal, installment: bool) -> tuple[str, str | None]:
    """The kind of a line and its payee."""
    folded = fold(description)
    if installment:
        return "installment", payee_of(description)
    if folded.startswith("odeme") and "tesekkur" in folded:
        return "payment", None
    if folded.endswith("fatura odeme"):
        words = description.split()[:-2]
        while words and words[0].isdigit():
            words = words[1:]
        return "bill", " ".join(words) or None
    return ("refund" if amount > 0 else "card"), payee_of(description)


def _block(anchor: TextPiece, pieces: list[TextPiece], sign: str, digits: str) -> StatementLine:
    amount = _amount(sign, digits)
    left = [p for p in pieces if p.x < anchor.x - 1]
    day_month = next((p for p in left if _DAY_MONTH.match(p.text) and fold(p.text[-3:]) in _MONTHS), None)
    year = next((p for p in left if _YEAR.match(p.text)), None)
    if day_month is None or year is None:
        raise FinanceError(f"The line of {anchor.text} TL has no date; the file layout is not the expected one.")
    day, month = _DAY_MONTH.match(day_month.text).groups()
    try:
        on = date(int(year.text), _MONTHS[fold(month)], int(day))
    except ValueError as exc:
        raise FinanceError(f"Cannot read the date {day_month.text} {year.text}.") from exc
    rest = [p for p in left if p is not day_month and p is not year and p.y > year.y - 1]
    description_lines: list[str] = []
    note = None
    for line in lines(rest):
        text = line_text(line)
        if m := _NOTE.match(fold(text)):
            note = m
        else:
            description_lines.append(text)
    description = " ".join(description_lines) or "(no description)"
    total = number = count = purchased = None
    if note is not None:
        total = parse_decimal(note.group(1), decimal_sep=",")
        number, count = int(note.group(2)), int(note.group(3))
        if not 1 <= number <= count:
            raise FinanceError(f"The installment note of {on} says {number}/{count}.")
        purchased = _purchase_date(description, on, number)
    kind, payee = read_description(description, amount, note is not None)
    return StatementLine(
        date=on,
        description=description,
        amount=amount,
        payee=payee,
        kind=kind,
        installment=number,
        installment_count=count,
        purchase_total=total,
        purchase_date=purchased,
    )


def _page_lines(pieces: list[TextPiece]) -> list[StatementLine]:
    ordered = top_down(pieces)
    anchors = []
    for p in ordered:
        if (m := _AMOUNT.match(p.text)) and (currency := _currency(p, m.group(3), ordered)):
            if currency.upper() not in ("TL", "TRY"):
                raise FinanceError(f"A line in {currency} was found; only lines in TL can be read for now.")
            anchors.append((p, m))
    out = []
    for k, (anchor, m) in enumerate(anchors):
        below = anchors[k + 1][0].y + 1 if k + 1 < len(anchors) else float("-inf")
        block = [p for p in ordered if below < p.y <= anchor.y + 1 and p is not anchor]
        out.append(_block(anchor, block, m.group(1), m.group(2)))
    return out


def _in_date_order(lines_: list[StatementLine]) -> list[StatementLine]:
    """Oldest first; lines of the same day keep the order of the list."""
    if len(lines_) > 1 and lines_[0].date > lines_[-1].date:
        lines_ = list(reversed(lines_))
    return sorted(lines_, key=lambda line: line.date)


def parse(pages: list[list[TextPiece]], file_format: str = "pdf") -> Statement:
    if not sniff(pages):
        raise FinanceError(f"Not a {NAME} card transaction list: the 'Kart Hareketleri' title is missing.")
    found = [line for pieces in pages for line in _page_lines(pieces)]
    if not found:
        raise FinanceError(f"The {NAME} card list has no transactions.")
    return Statement(
        bank=NAME,
        file_format=file_format,
        lines=tuple(_in_date_order(found)),
        currency="TRY",
        card=True,
    )
