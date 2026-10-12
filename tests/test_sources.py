"""Data source parsers and clients, on saved responses (no network).

The TCMB XML bulletins, the Frankfurter answer and the TCMB CPI page are real
responses saved on 2026-10-09. EVDS needs a personal key, so its answers below
are written in the documented format (dots become underscores, values are
strings or null, daily dates DD-MM-YYYY, monthly YYYY-M or YYYY-MM).
"""

import json
from datetime import date
from decimal import Decimal

import httpx
import pytest
from conftest import EVDS_DAILY, EVDS_MONTHLY

from financeproject.errors import EvdsKeyError, SourceError
from financeproject.prices import evds, frankfurter, tcmb, tcmb_cpi
from financeproject.prices.http import make_client


def client_for(handler):
    return make_client(transport=httpx.MockTransport(handler))


def test_parse_tcmb_bulletin(fixtures):
    b = tcmb.parse_bulletin((fixtures / "tcmb_20261008.xml").read_bytes())
    assert b.date == date(2026, 10, 8)
    assert b.number == "2026/190"
    assert b.price("USD") == Decimal("49.1267")
    assert b.price("USD", "ForexSelling") == Decimal("49.2152")
    assert b.price("JPY") == Decimal("0.309711")  # quoted per 100 yen
    assert b.currencies["XDR"].rates["ForexSelling"] is None
    assert b.price("XYZ") is None


@pytest.mark.parametrize(
    "xml",
    [b"<html>not xml", b"<Other Tarih='01.01.2026'/>", b'<Tarih_Date Tarih="2026-10-08"></Tarih_Date>'],
)
def test_parse_tcmb_bulletin_rejects_other_content(xml):
    with pytest.raises(SourceError):
        tcmb.parse_bulletin(xml)


def test_fetch_tcmb_bulletin_urls_and_status(fixtures):
    seen = []

    def handler(request):
        seen.append(request.url.path)
        if request.url.path == "/kurlar/202610/01102026.xml":
            return httpx.Response(200, content=(fixtures / "tcmb_20261001.xml").read_bytes())
        if request.url.path == "/kurlar/202610/02102026.xml":  # wrong file served
            return httpx.Response(200, content=(fixtures / "tcmb_20261008.xml").read_bytes())
        if request.url.path == "/kurlar/today.xml":
            return httpx.Response(200, content=(fixtures / "tcmb_20261008.xml").read_bytes())
        if request.url.path == "/kurlar/202610/03102026.xml":
            return httpx.Response(500)
        return httpx.Response(404)

    with client_for(handler) as client:
        assert tcmb.fetch_bulletin(client, date(2026, 10, 1)).price("USD") == Decimal("48.9466")
        assert tcmb.fetch_bulletin(client, date(2026, 10, 4)) is None
        assert tcmb.fetch_bulletin(client).date == date(2026, 10, 8)
        with pytest.raises(SourceError, match="returned the bulletin of"):
            tcmb.fetch_bulletin(client, date(2026, 10, 2))
        with pytest.raises(SourceError, match="500"):
            tcmb.fetch_bulletin(client, date(2026, 10, 3))
    assert seen[:2] == ["/kurlar/202610/01102026.xml", "/kurlar/202610/04102026.xml"]


def test_frankfurter_gold(fixtures):
    payload = json.loads((fixtures / "frankfurter_xau_usd.json").read_text())
    rows = frankfurter.parse_rates(payload)
    assert rows[0] == (date(2026, 9, 28), "XAU", "USD", Decimal("4239.06"))
    assert frankfurter.ounce_to_gram(Decimal("31.1034768")) == Decimal("1.00000000")

    def handler(request):
        assert request.url.path == "/v2/rates"
        params = dict(request.url.params)
        assert params == {"base": "XAU", "quotes": "USD", "from": "2026-09-28", "to": "2026-10-09"}
        return httpx.Response(200, json=payload)

    with client_for(handler) as client:
        grams = frankfurter.fetch_gold_usd_per_gram(client, date(2026, 9, 28), date(2026, 10, 9))
    assert grams[0] == (date(2026, 9, 28), (Decimal("4239.06") / Decimal("31.1034768")).quantize(Decimal("0.00000001")))
    assert len(grams) == len(payload)


def test_frankfurter_errors():
    with pytest.raises(SourceError):
        frankfurter.parse_rates({"status": 422, "message": "invalid currency: ABC"})
    with pytest.raises(SourceError):
        frankfurter.parse_rates([{"date": "x"}])
    with client_for(lambda r: httpx.Response(429, text="slow down")) as client:
        with pytest.raises(SourceError, match="429"):
            frankfurter.fetch_gold_usd_per_gram(client, date(2026, 1, 1), date(2026, 1, 2))


def test_evds_url_and_periods():
    url = evds.series_url(["TP.DK.USD.A.YTL", "TP.DK.EUR.A.YTL"], date(2026, 10, 1), date(2026, 10, 8))
    assert url == (
        "https://evds3.tcmb.gov.tr/igmevdsms-dis/series=TP.DK.USD.A.YTL-TP.DK.EUR.A.YTL"
        "&startDate=01-10-2026&endDate=08-10-2026&type=json"
    )
    assert evds.series_url(["X"], date(2026, 1, 1), date(2026, 2, 1), frequency=5).endswith("&frequency=5")
    assert evds.parse_period("08-10-2026") == date(2026, 10, 8)
    assert evds.parse_period("2026-9") == evds.parse_period("2026-09") == date(2026, 9, 1)
    with pytest.raises(SourceError):
        evds.parse_period("2026Q3")


def test_evds_parse_series():
    out = evds.parse_series(EVDS_DAILY, ["TP.DK.USD.A.YTL", "TP.DK.EUR.A.YTL"])
    assert out["TP.DK.USD.A.YTL"] == [(date(2026, 10, 1), Decimal("48.94660000")), (date(2026, 10, 8), Decimal("49.12670000"))]
    monthly = evds.parse_series(EVDS_MONTHLY, [evds.CPI_SERIES])[evds.CPI_SERIES]
    assert monthly == [(date(2025, 12, 1), Decimal("110.39000000")), (date(2026, 1, 1), Decimal("115.74000000"))]
    with pytest.raises(SourceError):
        evds.parse_series({"status": "403", "message": "nope"}, ["X"])


def test_evds_sends_key_in_header_and_reports_rejection():
    def handler(request):
        if request.headers.get("key") != "secret":
            return httpx.Response(403, json={"status": "403", "message": "Required request header 'key' is not present"})
        assert "key=" not in str(request.url)  # never in the URL
        return httpx.Response(200, json=EVDS_DAILY)

    with client_for(handler) as client:
        out = evds.fetch_series(client, "secret", ["TP.DK.USD.A.YTL"], date(2026, 10, 1), date(2026, 10, 8))
        assert len(out["TP.DK.USD.A.YTL"]) == 2
        with pytest.raises(EvdsKeyError):
            evds.fetch_series(client, "wrong", ["TP.DK.USD.A.YTL"], date(2026, 10, 1), date(2026, 10, 8))
        with pytest.raises(EvdsKeyError):
            evds.fetch_series(client, "", ["TP.DK.USD.A.YTL"], date(2026, 10, 1), date(2026, 10, 8))


def test_tcmb_cpi_table(fixtures):
    rows = tcmb_cpi.parse_cpi_table((fixtures / "tcmb_cpi_sample.html").read_text(encoding="utf-8"))
    assert len(rows) == 17
    assert rows[0] == tcmb_cpi.CpiChange(date(2005, 1, 1), Decimal("9.24"), Decimal("0.55"))
    assert rows[-1] == tcmb_cpi.CpiChange(date(2026, 9, 1), Decimal("29.73"), Decimal("1.84"))
    assert [r.month for r in rows] == sorted(r.month for r in rows)
    with pytest.raises(SourceError, match="No CPI table"):
        tcmb_cpi.parse_cpi_table("<html><table><tr><td>a</td></tr></table></html>")
