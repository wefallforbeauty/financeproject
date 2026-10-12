"""Exact amounts: parsing (Turkish and international formats) and formatting.

Amounts are ``decimal.Decimal`` everywhere in the ledger. Binary floats are
refused on input because 0.1 + 0.2 != 0.3 in floating point.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from financeproject.errors import AmountError

_NOISE = re.compile(r"[\s  '₺$€£]|(?<![A-Za-z])(?:TL|TRY)(?![A-Za-z])", re.IGNORECASE)
_PLAIN = re.compile(r"\d+(?:\.\d+)?|\.\d+")

NUMBER_FORMATS = {
    # name: (thousands separator, decimal separator)
    "1,234.56": (",", "."),
    "1.234,56": (".", ","),
}
DEFAULT_NUMBER_FORMAT = "1,234.56"
_number_format = DEFAULT_NUMBER_FORMAT


def set_number_format(name: str) -> None:
    """Choose how ``format_amount`` writes numbers: ``"1,234.56"`` or ``"1.234,56"``."""
    global _number_format
    if name not in NUMBER_FORMATS:
        raise AmountError(f"Unknown number format {name!r}; use one of: {', '.join(NUMBER_FORMATS)}")
    _number_format = name


def get_number_format() -> str:
    return _number_format


def _strip(text: str) -> tuple[str, bool]:
    """Remove currency signs, spaces and the sign; return the digits part and whether it is negative."""
    s = text.strip()
    negative = False
    if s.startswith("(") and s.endswith(")"):
        negative, s = True, s[1:-1]
    s = _NOISE.sub("", s)
    if s.endswith("-"):
        negative, s = not negative, s[:-1]
    if s.startswith("+"):
        s = s[1:]
    elif s.startswith("-"):
        negative, s = not negative, s[1:]
    return s, negative


def parse_decimal(text: str | int | Decimal, decimal_sep: str | None = None) -> Decimal:
    """Parse an amount such as ``"1.234,56"``, ``"1,234.56"``, ``"-450"`` or ``"(12,5)"``.

    With ``decimal_sep`` given (``","`` or ``"."``) the other character is taken as
    the thousands separator. Without it:

    - if both appear, the one that comes last is the decimal separator;
    - a separator that appears more than once is a thousands separator;
    - a single ``,`` or ``.`` is a decimal separator, so ``"1.234"`` is 1.234.
      Pass ``decimal_sep=","`` for files where it means 1234.
    """
    if isinstance(text, bool):
        raise AmountError(f"Invalid amount: {text!r}")
    if isinstance(text, Decimal):
        if not text.is_finite():
            raise AmountError(f"Invalid amount: {text!r}")
        return text
    if isinstance(text, int):
        return Decimal(text)
    if isinstance(text, float):
        raise AmountError("Amounts cannot be given as floats; use text or Decimal.")
    if decimal_sep not in (None, ",", "."):
        raise AmountError(f"The decimal separator must be ',' or '.', not '{decimal_sep}'.")

    raw = str(text)
    s, negative = _strip(raw)
    if not s:
        raise AmountError(f"Invalid amount: {raw!r}")

    if decimal_sep is None:
        dots, commas = s.count("."), s.count(",")
        if dots and commas:
            decimal_sep = "," if s.rfind(",") > s.rfind(".") else "."
        elif commas:
            decimal_sep = "," if commas == 1 else None
        elif dots:
            decimal_sep = "." if dots == 1 else None
        else:
            decimal_sep = "."

    if decimal_sep is None:
        s = s.replace(",", "").replace(".", "")
    else:
        thousands = "." if decimal_sep == "," else ","
        s = s.replace(thousands, "").replace(decimal_sep, ".")

    if not _PLAIN.fullmatch(s):
        raise AmountError(f"Invalid amount: {raw!r}")
    try:
        value = Decimal(s)
    except InvalidOperation as exc:  # pragma: no cover - the regex already guards this
        raise AmountError(f"Invalid amount: {raw!r}") from exc
    return -value if negative else value


def infer_decimal_separator(texts: Iterable[str]) -> str | None:
    """The decimal separator of a column of amounts, or ``None`` if no value decides it.

    A value decides it when it has both separators (the last one is decimal),
    one separator several times (it groups thousands, so the other is decimal),
    or one separator followed by other than exactly three digits. ``"1.234"`` and
    ``"1,234"`` are ambiguous and decide nothing. Values that disagree are an
    error: the file mixes formats.
    """
    votes: set[str] = set()
    for text in texts:
        s, _ = _strip(str(text))
        dots, commas = s.count("."), s.count(",")
        if dots and commas:
            votes.add("," if s.rfind(",") > s.rfind(".") else ".")
        elif dots > 1:
            votes.add(",")
        elif commas > 1:
            votes.add(".")
        elif dots == 1 or commas == 1:
            sep = "." if dots else ","
            if len(s) - s.rfind(sep) - 1 != 3:
                votes.add(sep)
    if len(votes) > 1:
        raise AmountError("The amounts mix '1.234,56' and '1,234.56' formats; give the decimal separator explicitly.")
    return votes.pop() if votes else None


def is_ambiguous_amount(text: str) -> bool:
    """True for values like ``"1.234"`` whose meaning depends on the decimal separator."""
    s, _ = _strip(str(text))
    dots, commas = s.count("."), s.count(",")
    if dots + commas != 1:
        return False
    sep = "." if dots else ","
    return len(s) - s.rfind(sep) - 1 == 3


def quantum(decimals: int) -> Decimal:
    """``Decimal('0.01')`` for 2 decimals, ``Decimal('1')`` for 0."""
    return Decimal(1).scaleb(-decimals)


def check_decimals(value: Decimal, decimals: int, unit: str = "") -> None:
    """Refuse amounts with more decimal places than the unit allows."""
    exponent = value.as_tuple().exponent
    if isinstance(exponent, int) and -exponent > decimals and value != value.quantize(quantum(decimals)):
        where = f" ({unit})" if unit else ""
        raise AmountError(f"{value}: at most {decimals} decimal places are allowed{where}.")


def round_half_up(value: Decimal, decimals: int = 2) -> Decimal:
    return value.quantize(quantum(decimals), rounding=ROUND_HALF_UP)


def format_amount(value: Decimal | float | int | None, decimals: int = 2, sign: bool = False) -> str:
    """``1234567.891`` → ``"1,234,567.89"`` (or ``"1.234.567,89"``, see ``set_number_format``).

    Missing values (None, NaN) are shown as ``"—"``.
    """
    if value is None:
        return "—"
    d = value if isinstance(value, Decimal) else Decimal(str(value))
    if not d.is_finite():
        return "—"
    q = round_half_up(d, decimals)
    body = f"{abs(q):,.{decimals}f}"
    thousands, point = NUMBER_FORMATS[_number_format]
    if (thousands, point) != (",", "."):
        body = body.replace(",", "\0").replace(".", point).replace("\0", thousands)
    if q < 0:
        return "-" + body
    if sign and q > 0:
        return "+" + body
    return body
