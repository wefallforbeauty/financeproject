"""Fund prices (TEFAS) and share prices (Yahoo Finance), against mocked answers."""

import json
from datetime import UTC, date, datetime
from decimal import Decimal

import httpx
import pytest

from financeproject.errors import FinanceError, SourceError
from financeproject.ledger.accounts import create_account
from financeproject.ledger.models import Commodity
from financeproject.ledger.transactions import (
    opening_balance,
    record_exchange,
    record_split,
)
from financeproject.prices import tefas, update, yahoo
from financeproject.prices.http import make_client
from financeproject.prices.store import load_pricebook

D = Decimal


def tefas_answer(code, rows):
    return {
        "errorCode": None,
        "errorMessage": None,
        "resultList": [
            {"fonKodu": code, "fonUnvan": "ÖRNEK FON", "kategoriDerece": 1, "kategoriFonSay": 9, "tarih": d, "fiyat": p}
            for d, p in rows
        ],
    }


def test_tefas_periods_and_codes():
    today = date(2026, 12, 17)
    assert [tefas.period_for(d, today) for d in (date(2026, 12, 1), date(2026, 11, 1), date(2026, 5, 1), date(2019, 1, 1))] == [
        1, 3, 12, 60,
    ]
    assert tefas.months_back(date(2026, 3, 31), 1) == date(2026, 2, 28)
    assert tefas.fund_code("TEFAS.TST") == "TST"


def test_tefas_prices_are_read_as_decimals():
    text = json.dumps(tefas_answer("TST", [("2026-05-05", 1.234567), ("2026-05-04", 1.2)]))
    payload = json.loads(text, parse_float=Decimal)
    assert tefas.parse_prices(payload, "TST") == [(date(2026, 5, 4), D("1.2")), (date(2026, 5, 5), D("1.234567"))]
    assert tefas.parse_prices(payload, "OTHER") == []
    with pytest.raises(SourceError, match="TEFAS error"):
        tefas.parse_prices({"errorCode": "E1", "errorMessage": "bad", "resultList": None}, "TST")
    with pytest.raises(SourceError, match="Cannot read"):
        tefas.parse_prices({"resultList": [{"fonKodu": "TST", "tarih": "x", "fiyat": 1}]}, "TST")


def yahoo_answer(days_and_closes, splits=(), currency="TRY"):
    def stamp(d):
        return int(datetime(d.year, d.month, d.day, 7, 0, tzinfo=UTC).timestamp())

    return {
        "chart": {
            "result": [
                {
                    "meta": {"currency": currency, "gmtoffset": 10800},
                    "timestamp": [stamp(d) for d, _ in days_and_closes],
                    "indicators": {"quote": [{"close": [c for _, c in days_and_closes]}]},
                    "events": {"splits": {str(stamp(d)): {"date": stamp(d), "numerator": n, "denominator": m} for d, n, m in splits}},
                }
            ],
            "error": None,
        }
    }


def test_yahoo_closes_are_turned_back_into_the_prices_of_their_day():
    answer = yahoo_answer(
        [(date(2026, 5, 5), 71.4), (date(2026, 5, 6), 70.85), (date(2026, 5, 7), 70.4), (date(2026, 5, 8), None)],
        splits=[(date(2026, 5, 7), 1000, 100)],
    )
    chart = yahoo.parse_chart(answer, "TESTA.IS")
    assert chart.currency == "TRY" and chart.splits == [yahoo.Split(date(2026, 5, 7), D(10))]
    assert chart.prices == [(date(2026, 5, 5), D("714.00")), (date(2026, 5, 6), D("708.50")), (date(2026, 5, 7), D("70.40"))]
    assert yahoo.symbol_of("BIST.TESTA") == "TESTA.IS" and yahoo.symbol_of("SPY") == "SPY"
    with pytest.raises(SourceError, match="no data"):
        yahoo.parse_chart({"chart": {"result": None, "error": {"description": "Not Found"}}}, "X.IS")
    with pytest.raises(SourceError, match="Unexpected"):
        yahoo.parse_chart({"nothing": 1}, "X.IS")


@pytest.fixture
def market(session):
    session.add(Commodity(code="TEFAS.TST", name="Örnek fon", kind="fund", decimals=6, price_source="tefas"))
    session.add(Commodity(code="BIST.TESTA", name="Örnek hisse", kind="stock", decimals=0, price_source="yahoo"))
    session.flush()
    create_account(session, "Assets:Bank:Checking", kind="checking")
    create_account(session, "Assets:Shares:TESTA", kind="stock", commodity="BIST.TESTA")
    return session


def handler(calls):
    def respond(request):
        calls.append(request)
        if request.url.host == "www.tefas.gov.tr":
            body = json.loads(request.content)
            rows = [("2026-05-04", 1.1), ("2026-05-05", 1.11), ("2026-05-06", 1.125)] if body["fonKodu"] == "TST" else []
            return httpx.Response(200, json=tefas_answer(body["fonKodu"], rows))
        answer = yahoo_answer(
            [(date(2026, 5, 4), 61.2), (date(2026, 5, 5), 62.4), (date(2026, 5, 6), 63.3)],
            splits=[(date(2026, 5, 5), 2, 1)],
        )
        return httpx.Response(200, json=answer)

    return respond


def test_update_tefas_and_yahoo(market):
    s = market
    calls = []
    with make_client(transport=httpx.MockTransport(handler(calls))) as client:
        report = update.update_tefas(s, client, date(2026, 5, 5), date(2026, 5, 6), today=date(2026, 5, 7), pause=0)
        assert json.loads(calls[0].content) == {"fonKodu": "TST", "dil": "TR", "periyod": 1}
        assert report.added == 2  # 4 May is before the start
        report = update.update_yahoo(s, client, date(2026, 5, 4), date(2026, 5, 6), today=date(2026, 5, 7), pause=0)
        assert report.added == 3 and not report.warnings  # nothing held: no split warning
    book = load_pricebook(s)
    assert book.resolve("TEFAS.TST", date(2026, 5, 6)).value == D("1.125")
    assert book.resolve("BIST.TESTA", date(2026, 5, 4)).value == D("122.40")  # before the 2-for-1 split
    assert book.resolve("BIST.TESTA", date(2026, 5, 6)).value == D("63.30")
    assert "periyod" in calls[0].content.decode() and calls[1].url.params["events"] == "split"


def test_a_split_while_holding_the_share_is_reported_until_recorded(market):
    s = market
    opening_balance(s, "Checking", D(10000), date(2026, 5, 1))
    record_exchange(s, "Checking", D(6000), "TESTA", D(50), date(2026, 5, 1))
    with make_client(transport=httpx.MockTransport(handler([]))) as client:
        report = update.update_yahoo(s, client, date(2026, 5, 4), date(2026, 5, 6), today=date(2026, 5, 7), pause=0)
        assert len(report.warnings) == 1 and "finance split ACCOUNT 2 --date 2026-05-05" in report.warnings[0]
        tx = record_split(s, "TESTA", D(2), date(2026, 5, 5))
        assert tx.note == "50 → 100" and tx.source == "split"
        report = update.update_yahoo(s, client, date(2026, 5, 4), date(2026, 5, 6), today=date(2026, 5, 7), pause=0)
        assert not report.warnings


def test_unknown_funds_and_quote_currencies_are_reported(market):
    s = market
    s.add(Commodity(code="TEFAS.NOPE", name="Yok", kind="fund", decimals=6, price_source="tefas"))
    s.add(Commodity(code="FOREIGN", name="Yabancı", kind="stock", decimals=0, price_source="yahoo"))
    s.flush()

    def respond(request):
        if request.url.host == "www.tefas.gov.tr":
            body = json.loads(request.content)
            return httpx.Response(200, json=tefas_answer(body["fonKodu"], [("2026-05-04", 2.0)] if body["fonKodu"] == "TST" else []))
        if "FOREIGN" in request.url.path:
            return httpx.Response(200, json=yahoo_answer([(date(2026, 5, 4), 5.0)], currency="JPY"))
        return httpx.Response(404)

    with make_client(transport=httpx.MockTransport(respond)) as client:
        report = update.update_tefas(s, client, date(2026, 4, 1), date(2026, 5, 6), today=date(2026, 5, 7), pause=0)
        assert any("no prices for NOPE" in w for w in report.warnings)
        report = update.update_yahoo(s, client, date(2026, 4, 1), date(2026, 5, 6), today=date(2026, 5, 7), pause=0)
        assert any("does not know the symbol TESTA.IS" in w for w in report.warnings)
        assert any("quoted in JPY" in w for w in report.warnings)


def test_record_split_rules(market):
    s = market
    with pytest.raises(FinanceError, match="holds no BIST.TESTA"):
        record_split(s, "TESTA", D(2), date(2026, 5, 5))
    with pytest.raises(FinanceError, match="shares or fund units"):
        record_split(s, "Checking", D(2), date(2026, 5, 5))
    opening_balance(s, "TESTA", D(105), date(2026, 5, 1))
    with pytest.raises(FinanceError, match="not 1"):
        record_split(s, "TESTA", D(1), date(2026, 5, 5))
    assert record_split(s, "TESTA", D("1.37"), date(2026, 5, 5)).note == "105 → 143"  # 143.85: fractions dropped
    assert record_split(s, "TESTA", D("0.1"), date(2026, 5, 8)).note == "143 → 14"
