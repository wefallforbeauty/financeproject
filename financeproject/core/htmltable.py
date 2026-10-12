"""The rows of every table in an HTML page, as lists of cell texts.

Used for pages and exports that put their data in ``<table>`` elements: the
TCMB inflation page and the HTML statements banks offer for download. Script
and style contents are skipped; text inside a cell is joined and its runs of
whitespace collapsed.
"""

from __future__ import annotations

from html.parser import HTMLParser

_SKIP = {"script", "style"}


class _TableRows(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[list[str]] = []
        self._row: list[str] | None = None
        self._cell: list[str] | None = None
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in _SKIP:
            self._skip += 1
        elif tag == "tr":
            self._row = []
        elif tag in ("td", "th") and self._row is not None:
            self._cell = []
        elif tag == "br" and self._cell is not None:
            self._cell.append(" ")

    def handle_endtag(self, tag):
        if tag in _SKIP:
            self._skip = max(0, self._skip - 1)
        elif tag in ("td", "th") and self._row is not None and self._cell is not None:
            self._row.append(" ".join("".join(self._cell).split()))
            self._cell = None
        elif tag == "tr" and self._row is not None:
            if self._cell is not None:  # a cell left open
                self._row.append(" ".join("".join(self._cell).split()))
                self._cell = None
            self.rows.append(self._row)
            self._row = None

    def handle_data(self, data):
        if self._cell is not None and not self._skip:
            self._cell.append(data)


def table_rows(html: str) -> list[list[str]]:
    """Every ``<tr>`` of the page in document order, each as its list of cell texts."""
    parser = _TableRows()
    parser.feed(html)
    parser.close()
    return parser.rows
