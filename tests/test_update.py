"""Fetching and storing prices and the CPI, against mocked sources."""

import csv
from datetime import date
from decimal import Decimal
from importlib import resources

import httpx
import pytest
from conftest import EVDS_DAILY

from financeproject.ledger.accounts import create_account
from financeproject.ledger.db import make_engine
from financeproject.prices import update
from financeproject.prices.http import make_client
from financeproject.prices.store import (
    KEPT,
    MANUAL,
    index_sources,
    load_index,
    load_pricebook,
    price_dates,
    upsert_index,
    upsert_price,
)


def tcmb_page_from_bundled_csv() -> str:
    """A TCMB-style CPI page built from the full bundled table."""
    text = resources.files("financeproject.data").joinpath("cpi_tcmb_monthly.csv").read_text(encoding="utf-8")
    rows = list(csv.DictReader(text.splitlines()))
    cells = "".join(
        f"<tr><td>{r['month'][5:]}-{r['month'][:4]}</td><td>{r['annual_change_pct']}</td><td>{r['monthly_change_pct']}</td></tr>"
        for r in reversed(rows)
    )
    return f"<table><thead><tr><th>Ay-Yıl</th><th>Yıllık</th><th>Aylık</th></tr></thead><tbody>{cells}</tbody></table>"


def tcmb_handler(fixtures, counter):
    files = {
        "/kurlar/202610/01102026.xml": fixtures / "tcmb_20261001.xml",
        "/kurlar/202610/08102026.xml": fixtures / "tcmb_20261008.xml",
    }

    def handler(request):
        counter.append(request.url.path)
        if request.url.path in files:
            return httpx.Response(200, content=files[request.url.path].read_bytes())
        return httpx.Response(404)

    return handler


def test_update_fx_from_tcmb_fetches_only_missing_days(session, fixtures):
    calls: list[str] = []
    with make_client(transport=httpx.MockTransport(tcmb_handler(fixtures, calls))) as client:
        report = update.update_fx(session, client, date(2026, 10, 1), date(2026, 10, 9), ["USD", "EUR"], pause=0)
        assert len(calls) == 7  # weekdays only
        assert report.added == 4  # 2 bulletins x 2 currencies
        assert price_dates(session, "USD", "TRY") == {date(2026, 10, 1), date(2026, 10, 8)}
        calls.clear()
        update.update_fx(session, client, date(2026, 10, 1), date(2026, 10, 9), ["USD", "EUR"], pause=0)
        assert len(calls) == 5  # the days that had no bulletin are tried again
    book = load_pricebook(session)
    assert book.resolve("USD", date(2026, 10, 9)).value == Decimal("49.1267")
    assert book.resolve("EUR", date(2026, 10, 7)).value == Decimal("55.2967")


def test_update_fx_uses_evds_with_a_key(session):
    def handler(request):
        assert request.headers["key"] == "the-key"
        return httpx.Response(200, json=EVDS_DAILY)

    with make_client(transport=httpx.MockTransport(handler)) as client:
        report = update.update_fx(session, client, date(2026, 9, 1), date(2026, 10, 9), ["USD", "EUR"], api_key="the-key")
    assert report.added == 4
    assert "EVDS" in report.messages[0]
    book = load_pricebook(session)
    assert book.latest("USD", "TRY", date(2026, 10, 9)).source == "evds"


def test_update_fx_falls_back_to_tcmb_when_the_key_is_rejected(session, fixtures):
    calls: list[str] = []
    tcmb = tcmb_handler(fixtures, calls)

    def handler(request):
        if request.url.host == "evds3.tcmb.gov.tr":
            return httpx.Response(403, json={"message": "bad key"})
        return tcmb(request)

    with make_client(transport=httpx.MockTransport(handler)) as client:
        report = update.update_fx(session, client, date(2026, 9, 28), date(2026, 10, 9), ["USD"], api_key="x", pause=0)
    assert report.warnings and "TCMB" in report.warnings[0]
    assert price_dates(session, "USD", "TRY") == {date(2026, 10, 1), date(2026, 10, 8)}


def test_update_gold_stores_usd_per_gram(session, fixtures):
    payload = (fixtures / "frankfurter_xau_usd.json").read_text()
    with make_client(transport=httpx.MockTransport(lambda r: httpx.Response(200, text=payload))) as client:
        update.update_gold(session, client, date(2026, 9, 28), date(2026, 10, 9))
    book = load_pricebook(session)
    q = book.latest("XAU_G", "USD", date(2026, 9, 28))
    assert q.value == (Decimal("4239.06") / Decimal("31.1034768")).quantize(Decimal("0.00000001"))
    assert q.source == "frankfurter"


def test_gold_ranges_refresh_last_week_and_extend_backwards(session):
    upsert_price(session, "XAU_G", "USD", date(2026, 6, 1), Decimal("100"), "frankfurter")
    upsert_price(session, "XAU_G", "USD", date(2026, 9, 1), Decimal("100"), "frankfurter")
    assert update._gold_ranges(session, date(2026, 1, 1), date(2026, 10, 9)) == [
        (date(2026, 1, 1), date(2026, 5, 31)),
        (date(2026, 8, 25), date(2026, 10, 9)),
    ]


def test_cpi_sources_and_priority(session):
    report = update.load_bundled_cpi(session)
    index = load_index(session)
    assert len(index) == 261 and report.added == 261
    assert sum(v for m, v in index.items() if m.year == 2025) / 12 == pytest.approx(100.0)
    assert index_sources(session) == {"bundled": 261}

    page = tcmb_page_from_bundled_csv()
    with make_client(transport=httpx.MockTransport(lambda r: httpx.Response(200, text=page))) as client:
        update.update_cpi(session, client)
    assert index_sources(session) == {"tcmb-table": 261}

    evds_answer = {"items": [{"Tarih": "2026-9", "TP_TUKFIY2025_GENEL": "137.21000000"}]}
    with make_client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=evds_answer))) as client:
        update.update_cpi(session, client, api_key="k", today=date(2026, 10, 9))
    assert load_index(session)[date(2026, 9, 1)] == pytest.approx(137.21)
    assert index_sources(session) == {"tcmb-table": 260, "evds": 1}
    # A lower-priority source never overwrites EVDS
    assert upsert_index(session, "CPI", date(2026, 9, 1), Decimal("1"), "bundled") == KEPT


def test_cpi_table_with_a_gap_is_an_error(session, fixtures):
    page = (fixtures / "tcmb_cpi_sample.html").read_text(encoding="utf-8")  # 17 rows, not consecutive
    with make_client(transport=httpx.MockTransport(lambda r: httpx.Response(200, text=page))) as client:
        with pytest.raises(update.SourceError, match="Cannot chain"):
            update.update_cpi(session, client)


def test_manual_price_is_never_overwritten(session):
    upsert_price(session, "USD", "TRY", date(2026, 10, 1), Decimal("50"), MANUAL)
    assert upsert_price(session, "USD", "TRY", date(2026, 10, 1), Decimal("48.9466"), "tcmb") == KEPT
    assert load_pricebook(session).latest("USD", "TRY", date(2026, 10, 1)).value == Decimal("50")


def test_tcmb_currencies(session):
    assert update.tcmb_currencies(session) == ["USD"]
    create_account(session, "Assets:FX:Euro", kind="fx", commodity="EUR")
    assert update.tcmb_currencies(session) == ["EUR", "USD"]


def test_make_engine_creates_parent_dirs(tmp_path):
    path = tmp_path / "a" / "b" / "ledger.sqlite"
    eng = make_engine(path)
    with eng.connect():
        pass
    eng.dispose()
    assert path.exists()
