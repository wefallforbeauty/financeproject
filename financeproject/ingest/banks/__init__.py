"""Bank statement templates: each module reads one bank's export into a ``Statement``.

A template module provides ``NAME``, ``KEY``, ``INPUT``, ``SUFFIXES``,
``sniff(data)`` and ``parse(data, file_format)``. ``INPUT`` says what ``data``
is: ``"table"`` for spreadsheets and HTML pages (rows of cells, from
``ingest.tables``), ``"pdf"`` for PDF files (text pieces with their positions,
from ``ingest.pdf``). To add a bank, write such a module and list it in
``TEMPLATES``.
"""

from __future__ import annotations

from pathlib import Path

from financeproject.errors import FinanceError
from financeproject.ingest.banks import ziraat, ziraat_card
from financeproject.ingest.pdf import read_pdf
from financeproject.ingest.statement import Statement
from financeproject.ingest.tables import Cell, read_excel, read_html

TEMPLATES = {ziraat.KEY: ziraat, ziraat_card.KEY: ziraat_card}
EXCEL_SUFFIXES = (".xlsx", ".xlsm", ".xls", ".ods")
HTML_SUFFIXES = (".html", ".htm")
PDF_SUFFIXES = (".pdf",)
STATEMENT_SUFFIXES = EXCEL_SUFFIXES + HTML_SUFFIXES + PDF_SUFFIXES


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
    if Path(path).suffix.lower() in PDF_SUFFIXES:
        data, file_format, kind = read_pdf(path), "pdf", "pdf"
    else:
        (data, file_format), kind = read_table(path), "table"
    if bank is not None:
        template = TEMPLATES.get(bank)
        if template is None:
            raise FinanceError(f"Unknown bank {bank!r}. Templates: {', '.join(TEMPLATES)}")
        if template.INPUT != kind:
            raise FinanceError(f"The {bank} template reads {', '.join(template.SUFFIXES)} files.")
        return template.parse(data, file_format)
    for template in TEMPLATES.values():
        if template.INPUT == kind and template.sniff(data):
            return template.parse(data, file_format)
    names = ", ".join(f"{t.NAME} ({t.KEY})" for t in TEMPLATES.values() if t.INPUT == kind)
    raise FinanceError(f"{Path(path).name} does not look like a statement of a known bank ({names}).")
