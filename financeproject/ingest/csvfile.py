"""CSV import: the standard layout or any bank layout via column mapping.

Standard layout (header names as below, in any order)::

    date,description,amount,account,counter_account,commodity,payee,note
    2026-10-01,MIGROS KONYA,-450.00,Liabilities:Credit Card:Bonus,,,,

The delimiter (``,`` ``;`` tab or ``|``) and the decimal separator are detected
from the file unless given. ``amount`` is the signed change of ``account`` from
the holder's point of view: negative = money out (a purchase, including one made
with a credit card), positive = money in (salary, a card payment received). Both
legs follow from that sign, for asset and for liability accounts alike.

``counter_account`` (the other side) may be empty: the categorization rules then
pick it, and if none matches the row goes to ``Expenses:Uncategorized`` or
``Income:Uncategorized``.

Every row gets an import key (hash of account, date, amount, description and
the row's occurrence number among identical rows), so importing the same or an
overlapping statement again adds nothing twice. An import is all or nothing:
if any row is invalid, nothing is written and all errors are listed.
"""

from __future__ import annotations

import csv
import hashlib
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from financeproject.core.dates import parse_date
from financeproject.core.money import (
    infer_decimal_separator,
    is_ambiguous_amount,
    parse_decimal,
)
from financeproject.core.text import fold
from financeproject.errors import FinanceError
from financeproject.ingest.rules import load_rules, match
from financeproject.ledger.accounts import find_account
from financeproject.ledger.db import UNCATEGORIZED_EXPENSE, UNCATEGORIZED_INCOME
from financeproject.ledger.models import Transaction
from financeproject.ledger.transactions import Leg, add_transaction

FIELDS = (
    "date", "description", "amount", "inflow", "outflow",
    "account", "counter_account", "commodity", "payee", "note",
)
AMOUNT_FIELDS = ("amount", "inflow", "outflow")
FALLBACK_ENCODINGS = ("utf-8-sig", "cp1254", "iso-8859-9")
"""UTF-8, then the Turkish Windows and ISO code pages common in bank exports."""

HOW_FILE, HOW_UNCATEGORIZED = "file", "uncategorized"


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


@dataclass(frozen=True, slots=True)
class Row:
    line: int
    date: date
    description: str
    amount: Decimal
    account: str | None
    counter_account: str | None
    commodity: str | None
    payee: str | None
    note: str | None


def _decode(data: bytes, encoding: str | None) -> tuple[str, str]:
    if encoding:
        return data.decode(encoding), encoding
    for enc in FALLBACK_ENCODINGS:
        try:
            return data.decode(enc), enc
        except UnicodeDecodeError:
            continue
    raise FinanceError("Cannot tell the character encoding of the file; give it with --encoding.")  # pragma: no cover


def read_rows(path: Path, fmt: CsvFormat) -> tuple[list[Row], list[str], str]:
    """Parse the file. Returns rows, error messages and the encoding used."""
    text, encoding = _decode(Path(path).read_bytes(), fmt.encoding)
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


def import_key(account_path: str, row: Row, occurrence: int) -> str:
    amount = format(row.amount.normalize(), "f")
    raw = f"{fold(account_path)}|{row.date.isoformat()}|{amount}|{fold(row.description)}|{occurrence}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


@dataclass(frozen=True, slots=True)
class PreviewRow:
    line: int
    date: date
    description: str
    amount: Decimal
    commodity: str
    account: str
    counter_account: str
    how: str
    """``file`` (given in the file), ``rule #n`` or ``uncategorized``."""
    duplicate: bool


@dataclass
class ImportResult:
    path: Path
    encoding: str
    rows: list[PreviewRow] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    written: bool = False

    @property
    def added(self) -> int:
        return sum(1 for r in self.rows if not r.duplicate) if self.written else 0

    @property
    def duplicates(self) -> int:
        return sum(1 for r in self.rows if r.duplicate)

    @property
    def uncategorized(self) -> int:
        return sum(1 for r in self.rows if not r.duplicate and r.how == HOW_UNCATEGORIZED)


def import_csv(
    session: Session,
    path: Path,
    fmt: CsvFormat | None = None,
    account: str | None = None,
    dry_run: bool = False,
) -> ImportResult:
    """Import ``path``. ``account`` is used for rows without an ``account`` value.

    With ``dry_run`` (or when any row is invalid) nothing is written.
    """
    fmt = fmt or CsvFormat()
    rows, errors, encoding = read_rows(Path(path), fmt)
    result = ImportResult(Path(path), encoding, errors=errors)
    rules = load_rules(session)
    default_account = find_account(session, account) if account else None
    uncategorized_expense = find_account(session, UNCATEGORIZED_EXPENSE)
    uncategorized_income = find_account(session, UNCATEGORIZED_INCOME)
    seen: Counter[tuple] = Counter()
    pending: list[tuple[Row, object, object, str]] = []

    for row in rows:
        try:
            acc = find_account(session, row.account) if row.account else default_account
            if acc is None:
                raise FinanceError("no account (the file needs an 'account' column, or give --account)")
            if row.counter_account:
                counter, how = find_account(session, row.counter_account), HOW_FILE
            elif rule := match(rules, " ".join(filter(None, [row.description, row.payee]))):
                counter, how = rule.account, f"rule #{rule.rule_id}"
            else:
                counter = uncategorized_expense if row.amount < 0 else uncategorized_income
                how = HOW_UNCATEGORIZED
            base = (acc.id, row.date, row.amount.normalize(), fold(row.description))
            seen[base] += 1
            key = import_key(acc.path, row, seen[base])
            duplicate = session.scalar(select(Transaction.id).where(Transaction.import_key == key)) is not None
            commodity = acc.commodity_code or row.commodity or "TRY"
            result.rows.append(
                PreviewRow(row.line, row.date, row.description, row.amount, commodity, acc.path, counter.path, how, duplicate)
            )
            if not duplicate:
                pending.append((row, acc, counter, key))
        except FinanceError as exc:
            result.errors.append(f"line {row.line}: {exc}")

    if result.errors or dry_run:
        return result

    source = f"csv:{Path(path).name}"[:80]
    for row, acc, counter, key in pending:
        try:
            add_transaction(
                session,
                row.date,
                row.description,
                [
                    Leg(acc, row.amount, commodity=row.commodity),
                    Leg(counter, -row.amount, commodity=acc.commodity_code or row.commodity),
                ],
                payee=row.payee,
                note=row.note,
                source=source,
                import_key=key,
            )
        except FinanceError as exc:
            result.errors.append(f"line {row.line}: {exc}")
    if result.errors:
        session.rollback()
        return result
    result.written = True
    return result
