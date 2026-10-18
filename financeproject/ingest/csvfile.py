"""CSV import: the standard layout or any bank layout via column mapping.

Standard layout (header names as below, in any order)::

    date,description,amount,account,counter_account,commodity,payee,note
    2026-10-01,MIGROS KONYA,-450.00,Liabilities:Credit Card:Bonus,,,,

The delimiter (``,`` ``;`` tab or ``|``) and the decimal separator are detected
from the file unless given. ``amount`` is the signed change of ``account`` from
the holder's point of view: negative = money out (a purchase, including one made
with a credit card), positive = money in (salary, a card payment received). Both
legs follow from that sign, for asset and for liability accounts alike.

``counter_account`` (the other side) may be empty: the categorization rules or
the learned model then pick it. Categories, duplicate keys and the
all-or-nothing write are in ``ingest.importer``.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from sqlalchemy.orm import Session

from financeproject.core.dates import parse_date
from financeproject.core.money import (
    infer_decimal_separator,
    is_ambiguous_amount,
    parse_decimal,
)
from financeproject.core.text import fold
from financeproject.errors import FinanceError
from financeproject.ingest import learn
from financeproject.ingest.importer import ImportResult, Row, import_key, import_rows
from financeproject.ingest.tables import FALLBACK_ENCODINGS, decode

FIELDS = (
    "date", "description", "amount", "inflow", "outflow",
    "account", "counter_account", "commodity", "payee", "note",
)
AMOUNT_FIELDS = ("amount", "inflow", "outflow")
__all__ = ["FALLBACK_ENCODINGS", "CsvFormat", "Row", "import_csv", "import_key", "parse_column_map", "read_rows"]


@dataclass
class CsvFormat:
    delimiter: str | None = None
    """``None``: detect from the file."""
    decimal_sep: str | None = None
    """``","`` or ``"."``; ``None``: detect from the amounts in the file."""
    date_format: str | None = None
    """``strptime`` pattern such as ``%d.%m.%Y``; ``None`` accepts 2026-10-09 and 09.10.2026."""
    encoding: str | None = None
    """``None``: UTF-8, else Windows-1254 / ISO-8859-9."""
    columns: dict[str, str] = field(default_factory=dict)
    """Field name → header in the file, for fields whose header differs."""
    skip_rows: int = 0
    """Lines before the header row (account details at the top of many exports)."""
    invert_sign: bool = False
    """For statements that list spending as positive amounts."""


def parse_column_map(text: str) -> dict[str, str]:
    """``"date=Transaction Date,amount=Amount"`` → ``{"date": "Transaction Date", "amount": "Amount"}``."""
    out: dict[str, str] = {}
    for part in filter(None, (p.strip() for p in text.split(","))):
        if "=" not in part:
            raise FinanceError(f"A column mapping is written as 'field=Header': {part!r}")
        name, header = (s.strip() for s in part.split("=", 1))
        if fold(name) not in FIELDS:
            raise FinanceError(f"Unknown field {name!r}. Fields: {', '.join(FIELDS)}")
        out[fold(name)] = header
    return out


def read_rows(path: Path, fmt: CsvFormat) -> tuple[list[Row], list[str], str]:
    """Parse the file. Returns rows, error messages and the encoding used."""
    text, encoding = decode(Path(path).read_bytes(), fmt.encoding)
    lines = text.splitlines()[fmt.skip_rows :]
    if not lines:
        raise FinanceError("The file is empty.")
    delimiter = fmt.delimiter
    if delimiter is None:
        try:
            delimiter = csv.Sniffer().sniff("\n".join(lines[:20]), delimiters=";,\t|").delimiter
        except csv.Error as exc:
            raise FinanceError("Cannot tell the column delimiter; give it with --delimiter.") from exc
    reader = csv.reader(lines, delimiter=delimiter)
    header = next(reader)
    folded_header = {fold(h): i for i, h in enumerate(header)}

    wanted = {name: fmt.columns.get(name, name) for name in FIELDS}
    index: dict[str, int] = {}
    for name, title in wanted.items():
        if (i := folded_header.get(fold(title))) is not None:
            index[name] = i
    for name, title in fmt.columns.items():
        if name not in index:
            raise FinanceError(f"The file has no '{title}' column. Headers: {', '.join(header)}")
    if "date" not in index or "description" not in index:
        raise FinanceError(f"The file needs date and description columns. Headers: {', '.join(header)}")
    if "amount" not in index and not ({"inflow", "outflow"} & index.keys()):
        raise FinanceError("The file needs an 'amount' column or 'inflow'/'outflow' columns.")

    body = [(n, cells) for n, cells in enumerate(reader, start=fmt.skip_rows + 2) if any(c.strip() for c in cells)]

    def cell(cells: list[str], name: str) -> str | None:
        i = index.get(name)
        if i is None or i >= len(cells):
            return None
        value = cells[i].strip()
        return value or None

    decimal_sep = fmt.decimal_sep
    if decimal_sep is None:
        amounts = [v for _, cells in body for name in AMOUNT_FIELDS if (v := cell(cells, name))]
        decimal_sep = infer_decimal_separator(amounts)
        if decimal_sep is None:
            if any(is_ambiguous_amount(v) for v in amounts):
                raise FinanceError(
                    "Amounts like '1.234' can mean 1234 or 1.234; give the decimal separator with --decimal."
                )
            decimal_sep = "."

    rows: list[Row] = []
    errors: list[str] = []
    for n, cells in body:
        try:
            raw_date = cell(cells, "date") or ""
            on = datetime.strptime(raw_date, fmt.date_format).date() if fmt.date_format else parse_date(raw_date)
            if "amount" in index and cell(cells, "amount") is not None:
                amount = parse_decimal(cell(cells, "amount") or "", decimal_sep)
            else:
                amount = Decimal(0)
                if inflow := cell(cells, "inflow"):
                    amount += abs(parse_decimal(inflow, decimal_sep))
                if outflow := cell(cells, "outflow"):
                    amount -= abs(parse_decimal(outflow, decimal_sep))
            if fmt.invert_sign:
                amount = -amount
            if amount == 0:
                raise FinanceError("the amount is zero or empty")
            description = cell(cells, "description")
            if not description:
                raise FinanceError("the description is empty")
            rows.append(
                Row(
                    line=n,
                    date=on,
                    description=description,
                    amount=amount,
                    account=cell(cells, "account"),
                    counter_account=cell(cells, "counter_account"),
                    commodity=(cell(cells, "commodity") or "").upper() or None,
                    payee=cell(cells, "payee"),
                    note=cell(cells, "note"),
                )
            )
        except (FinanceError, ValueError) as exc:
            errors.append(f"line {n}: {exc}")
    return rows, errors, encoding


def import_csv(
    session: Session,
    path: Path,
    fmt: CsvFormat | None = None,
    account: str | None = None,
    dry_run: bool = False,
    use_model: bool = True,
    min_confidence: float = learn.DEFAULT_MIN_CONFIDENCE,
    overrides: dict[int, str] | None = None,
) -> ImportResult:
    """Import ``path``. ``account`` is used for rows without an ``account`` value.

    With ``dry_run`` (or when any row is invalid) nothing is written.
    ``overrides`` maps line numbers to chosen counter accounts.
    """
    fmt = fmt or CsvFormat()
    rows, errors, encoding = read_rows(Path(path), fmt)
    return import_rows(
        session,
        rows,
        path=Path(path),
        source=f"CSV, {encoding}",
        transaction_source=f"csv:{Path(path).name}"[:80],
        account=account,
        errors=errors,
        dry_run=dry_run,
        use_model=use_model,
        min_confidence=min_confidence,
        overrides=overrides,
    )
