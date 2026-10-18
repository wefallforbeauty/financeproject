"""The interface's languages: English, with a Turkish translation.

Texts are written in English in the templates and views and wrapped in
``_()``. A translation file maps each English text to its translation
(``locales/tr.json``); a text without a translation stays English. Texts may
carry named fields: ``_("Transaction #{n} added.", n=3)``. A text with a count
has a singular and a plural form: ``_.n("{n} line", "{n} lines", count)``;
both are keys of the translation file (Turkish uses one form for both).

Messages from the program's core (errors, for example) stay English.
"""

from __future__ import annotations

import json
from functools import cache
from importlib import resources

LANGUAGES = {"en": "English", "tr": "Türkçe"}
DEFAULT = "en"

@cache
def catalog(language: str) -> dict[str, str]:
    if language == DEFAULT or language not in LANGUAGES:
        return {}
    text = resources.files("financeproject.web").joinpath("locales", f"{language}.json").read_text(encoding="utf-8")
    return json.loads(text)


class Translator:
    """``_(text, **fields)``: the text in the language, with its fields filled in."""

    def __init__(self, messages: dict[str, str]):
        self.messages = messages

    def __call__(self, text: str, **fields) -> str:
        out = self.messages.get(text) or text
        return out.format(**fields) if fields else out

    def n(self, one: str, many: str, number: int, **fields) -> str:
        """``one`` when ``number`` is 1, otherwise ``many``; ``{n}`` is ``number`` unless given."""
        fields.setdefault("n", number)
        return self(one if number == 1 else many, **fields)


def translator(language: str) -> Translator:
    return Translator(catalog(language))
