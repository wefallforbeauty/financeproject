"""Reading text out of PDF statements, with the position of every piece.

A PDF page is positioned text, not a table. ``read_pdf`` returns the pieces of
text of each page with their coordinates in PDF units (y grows upwards); bank
templates then tell the columns apart by position.

Some banks print a web page: the whole page is one tall drawing that every PDF
page shows a slice of. Such pages carry the same pieces, so a page whose pieces
equal those of an earlier page is read only once.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from financeproject.errors import FinanceError

SOFT_HYPHEN = "\xad"


@dataclass(frozen=True, slots=True)
class TextPiece:
    x: float
    y: float
    text: str


def clean(text: str) -> str:
    """Whitespace collapsed; soft hyphens (invisible break hints) become spaces."""
    return " ".join(text.replace(SOFT_HYPHEN, " ").split())


def _position(tm: list[float], cm: list[float]) -> tuple[float, float]:
    """Origin of the text matrix ``tm`` in the coordinates of ``cm``."""
    x, y = tm[4], tm[5]
    return x * cm[0] + y * cm[2] + cm[4], x * cm[1] + y * cm[3] + cm[5]


def read_pdf(path: Path) -> list[list[TextPiece]]:
    """Text pieces of every page, pages that repeat an earlier page left out."""
    from pypdf import PdfReader
    from pypdf.errors import PdfReadError

    try:
        reader = PdfReader(str(path))
        if reader.is_encrypted:
            raise FinanceError(f"{Path(path).name} is password-protected; save it again without a password.")
        pages: list[list[TextPiece]] = []
        seen: set[tuple[TextPiece, ...]] = set()
        for page in reader.pages:
            pieces: list[TextPiece] = []

            def visit(text, cm, tm, font, size, pieces=pieces):
                if text := clean(text):
                    x, y = _position(tm, cm)
                    pieces.append(TextPiece(round(x, 2), round(y, 2), text))

            page.extract_text(visitor_text=visit)
            key = tuple(pieces)
            if key not in seen:
                seen.add(key)
                pages.append(pieces)
    except (PdfReadError, OSError, ValueError, KeyError) as exc:
        raise FinanceError(f"Cannot read the PDF {Path(path).name}: {exc}") from exc
    return pages


def top_down(pieces: Iterable[TextPiece]) -> list[TextPiece]:
    """Top to bottom, then left to right."""
    return sorted(pieces, key=lambda p: (-p.y, p.x))


def lines(pieces: Iterable[TextPiece], tolerance: float = 1.0) -> list[list[TextPiece]]:
    """Pieces on the same height (within ``tolerance``) as lines, top to bottom, each left to right."""
    out: list[list[TextPiece]] = []
    for piece in top_down(pieces):
        if out and abs(out[-1][0].y - piece.y) <= tolerance:
            out[-1].append(piece)
        else:
            out.append([piece])
    return [sorted(line, key=lambda p: p.x) for line in out]


def line_text(line: Iterable[TextPiece]) -> str:
    return clean(" ".join(p.text for p in line))
