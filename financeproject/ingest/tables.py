"""Reading exported files as rows of cells: text decoding, Excel and HTML.

Excel files are read with python-calamine, which also opens files that other
readers reject. Ziraat Bankası's export, for example, has an empty fill in its
style sheet that openpyxl refuses, and no cell references, so cells are placed
in order.
"""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

from financeproject.core.htmltable import table_rows
from financeproject.errors import FinanceError

FALLBACK_ENCODINGS = ("utf-8-sig", "cp1254", "iso-8859-9")
"""UTF-8, then the Turkish Windows and ISO code pages common in bank exports."""

Cell = str | float | int | bool | date | datetime | None


def decode(data: bytes, encoding: str | None = None) -> tuple[str, str]:
    """Text of ``data`` and the encoding that worked."""
    if encoding:
        return data.decode(encoding), encoding
    for enc in FALLBACK_ENCODINGS:
        try:
            return data.decode(enc), enc
        except UnicodeDecodeError:
            continue
    raise FinanceError("Cannot tell the character encoding of the file; give it with --encoding.")  # pragma: no cover


def read_excel(path: Path, sheet: int = 0) -> list[list[Cell]]:
    """Rows of one sheet of an .xlsx, .xls or .ods file. Values keep their types:
    numbers, text, dates."""
    from python_calamine import CalamineError, CalamineWorkbook

    try:
        workbook = CalamineWorkbook.from_path(str(path))
        rows = workbook.get_sheet_by_index(sheet).to_python(skip_empty_area=False)
    except (CalamineError, OSError, ValueError, IndexError) as exc:
        raise FinanceError(f"Cannot read the spreadsheet {Path(path).name}: {exc}") from exc
    return [[None if v == "" else v for v in row] for row in rows]


def read_html(path: Path) -> list[list[Cell]]:
    """Rows of every table in an HTML file, as text cells."""
    text, _ = decode(Path(path).read_bytes())
    return [[c or None for c in row] for row in table_rows(text)]
