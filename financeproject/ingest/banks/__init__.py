"""Bank statement templates: each module reads one bank's export into a ``Statement``.

A template module provides ``NAME``, ``KEY``, ``SUFFIXES``, ``sniff(rows)``
and ``parse(rows, file_format)``. To add a bank, write such a module and list
it in ``TEMPLATES``.
"""

from __future__ import annotations

from pathlib import Path

from financeproject.errors import FinanceError
from financeproject.ingest.banks import ziraat
from financeproject.ingest.statement import Statement
from financeproject.ingest.tables import Cell, read_excel, read_html

TEMPLATES = {ziraat.KEY: ziraat}
EXCEL_SUFFIXES = (".xlsx", ".xlsm", ".xls", ".ods")
HTML_SUFFIXES = (".html", ".htm")


def read_table(path: Path) -> tuple[list[list[Cell]], str]:
    """Rows of a spreadsheet or HTML export and its format name."""
    suffix = Path(path).suffix.lower()
    if suffix in EXCEL_SUFFIXES:
        return read_excel(path), suffix.lstrip(".")
    if suffix in HTML_SUFFIXES:
        return read_html(path), "html"
    raise FinanceError(f"No bank template reads {suffix or 'files without an extension'} files.")


def read_statement(path: Path, bank: str | None = None) -> Statement:
    """Read ``path`` with the template of ``bank``, or with the first template that recognizes it."""
    rows, file_format = read_table(path)
    if bank is not None:
        template = TEMPLATES.get(bank)
        if template is None:
            raise FinanceError(f"Unknown bank {bank!r}. Templates: {', '.join(TEMPLATES)}")
        return template.parse(rows, file_format)
    for template in TEMPLATES.values():
        if template.sniff(rows):
            return template.parse(rows, file_format)
    names = ", ".join(t.NAME for t in TEMPLATES.values())
    raise FinanceError(f"{Path(path).name} does not look like a statement of a known bank ({names}).")
