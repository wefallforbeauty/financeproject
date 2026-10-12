"""Text folding for matching names typed with or without Turkish letters."""

from __future__ import annotations

import re
import unicodedata

_TO_ASCII = str.maketrans(
    {"ı": "i", "ş": "s", "ğ": "g", "ü": "u", "ö": "o", "ç": "c", "â": "a", "î": "i", "û": "u"}
)


def tr_lower(text: str) -> str:
    """Lower-case with the Turkish dotted/dotless I rules (I → ı, İ → i)."""
    return text.replace("I", "ı").replace("İ", "i").lower()


def fold(text: str) -> str:
    """Matching key that ignores case, accents, Turkish letters and repeated spaces.

    ``fold("GIDA:Market")``, ``fold("Gıda:market")`` and ``fold("gida:MARKET")``
    are all ``"gida:market"``. Bank statements in Turkey are written in upper
    case with Turkish letters (``MİGROS``), so the Turkish rules matter.
    """
    s = tr_lower(text).translate(_TO_ASCII)
    s = unicodedata.normalize("NFKD", s)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    return re.sub(r"\s+", " ", s).strip()
