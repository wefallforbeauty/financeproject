from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from financeproject.core.money import DEFAULT_NUMBER_FORMAT, set_number_format
from financeproject.core.prices import PriceBook, Quote
from financeproject.ledger.db import init_db, make_engine, session_scope

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(autouse=True)
def isolated_dirs(tmp_path, monkeypatch):
    """Never touch the real ledger or settings: point every path at tmp_path."""
    monkeypatch.setenv("FINANCEPROJECT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("FINANCEPROJECT_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.delenv("FINANCEPROJECT_DB", raising=False)
    monkeypatch.delenv("EVDS_API_KEY", raising=False)
    yield
    set_number_format(DEFAULT_NUMBER_FORMAT)


@pytest.fixture
def engine():
    eng = make_engine(":memory:")
    init_db(eng)
    yield eng
    eng.dispose()


@pytest.fixture
def session(engine):
    with session_scope(engine) as s:
        yield s


@pytest.fixture
def fixtures() -> Path:
    return FIXTURES


def simple_book() -> PriceBook:
    """USD/TRY 40 and gold 100 USD/g (4,000 TRY/g) from 2026-01-01, then 44 and 110 from 2026-06-01."""
    return PriceBook(
        [
            Quote("USD", "TRY", date(2026, 1, 1), Decimal("40"), "test"),
            Quote("XAU_G", "USD", date(2026, 1, 1), Decimal("100"), "test"),
            Quote("USD", "TRY", date(2026, 6, 1), Decimal("44"), "test"),
            Quote("XAU_G", "USD", date(2026, 6, 1), Decimal("110"), "test"),
        ]
    )


# EVDS answers in the documented format (a real one needs a personal key).
EVDS_DAILY = {
    "totalCount": 3,
    "items": [
        {"Tarih": "01-10-2026", "TP_DK_USD_A_YTL": "48.94660000", "TP_DK_EUR_A_YTL": "55.29670000", "UNIXTIME": {"$numberLong": "1"}},
        {"Tarih": "03-10-2026", "TP_DK_USD_A_YTL": None, "TP_DK_EUR_A_YTL": None, "UNIXTIME": {"$numberLong": "2"}},
        {"Tarih": "08-10-2026", "TP_DK_USD_A_YTL": "49.12670000", "TP_DK_EUR_A_YTL": "54.95480000", "UNIXTIME": {"$numberLong": "3"}},
    ],
}
EVDS_MONTHLY = {
    "totalCount": 3,
    "items": [
        {"Tarih": "2025-12", "TP_TUKFIY2025_GENEL": "110.39000000"},
        {"Tarih": "2026-1", "TP_TUKFIY2025_GENEL": "115.74000000"},
        {"Tarih": "2026-2", "TP_TUKFIY2025_GENEL": ""},
    ],
}
