# Data sources

Which data come from where, why that source was chosen, and what to watch
out for. As of 2026-10-09.

## In use

| Data | Source | Key | Frequency | Module |
|---|---|---|---|---|
| Exchange rates (USD, EUR, …) | TCMB indicative rates, XML | not needed | business days, ~15:30 | `prices/tcmb.py` |
| Exchange rates (a range) | TCMB EVDS3 `TP.DK.USD.A.YTL`, `TP.DK.EUR.A.YTL` | needed (free) | daily | `prices/evds.py` |
| Gold (XAU/USD) | Frankfurter v2, gold prices of central banks | not needed | daily | `prices/frankfurter.py` |
| CPI (index) | TCMB EVDS3 `TP.TUKFIY2025.GENEL` (2025=100) | needed (free) | monthly | `prices/evds.py` |
| CPI (changes) | TCMB "Tüketici Fiyatları" (consumer prices) page | not needed | monthly | `prices/tcmb_cpi.py` |
| CPI (copy) | The same table, shipped with the package | — | up to 2026-09 | `financeproject/data/` |

TCMB is the Central Bank of the Republic of Türkiye (CBRT); EVDS is its
electronic data delivery system. The CPI (TÜFE in Turkish) is published by
TurkStat (TÜİK).

### TCMB exchange rate bulletin

- **Addresses:**
  - Today: `https://www.tcmb.gov.tr/kurlar/today.xml`
  - A past day: `https://www.tcmb.gov.tr/kurlar/YYYYMM/DDMMYYYY.xml`
  - Weekends and holidays return 404. The program skips them and tries again at
    the next update.
- **Contents:** for each currency a `Unit` (100 for JPY) and four rates: forex
  buying and selling, banknote buying and selling. The program stores **forex
  buying / Unit**.
- **Date:** the bulletin's own date (`Tarih="08.10.2026"`). The rate announced
  at 15:30 that day counts as that day's closing value.
- **Speed:** the past needs one request per day. A year is about 250 requests
  and 1.5 minutes. With an EVDS key one request is enough.

### TCMB EVDS3

- **Address:** EVDS moved to `evds3.tcmb.gov.tr` in 2025. The API root is
  `https://evds3.tcmb.gov.tr/igmevdsms-dis/`. The old
  `evds2.tcmb.gov.tr/service/evds` address now redirects to the web interface.
- **Request:** `series=CODE1-CODE2&startDate=DD-MM-YYYY&endDate=DD-MM-YYYY&type=json`.
- **Key:** it must be sent **in the `key` HTTP header**. A request without it
  returns `403 Required request header 'key' is not present` (tried on
  2026-10-09).
- **Answer:** `{"totalCount": n, "items": [{"Tarih": "08-10-2026",
  "TP_DK_USD_A_YTL": "49.12670000"}, …]}`.
  - Dots in series codes become underscores.
  - Values come as text or `null` (holidays).
  - Monthly dates may look like `2026-09` or `2026-9`.
- **Archived series:**
  - `TP.FG.J0`: CPI 2003=100, ended 2026-01.
  - `TP.MK.KUL.YTL`: Grand Bazaar gold bullion, monthly average.
- **Getting a key:** sign up at evds3.tcmb.gov.tr; the key is on the profile
  page. Save it with `finance config evds-key KEY`.
- **Not tried live yet:** the client has not been tried with a key; it follows
  the documentation, the error a request without a key returns, and real
  answers others have published.

### Frankfurter v2 (gold)

- **Address:** `https://api.frankfurter.dev/v2/rates?base=XAU&quotes=USD&from=…&to=…`.
- **License and cost:** no key needed; MIT-licensed open source, and it can be
  run on your own server with Docker.
- **Gold data:** about fourteen central banks publish a gold price, among them
  the central banks of Russia, Poland, Ukraine, Romania, Moldova, Azerbaijan and
  Armenia. Without `providers` the API returns the average of their latest
  values. Daily data since 1999.
- **Why the average:** banks publish at different times of the day, so a
  single bank can lag by a day or catch one moment of the day.
  - On calm days the banks differ by less than ±1 %.
  - On very volatile days the spread grows. On 2026-01-30, for example, single
    banks gave between 4,994 and 5,586 USD/oz; the average was 5,328.
  - The average is therefore steadier than any single bank.
- **Grams of gold:** the TRY price is the international parity: XAU/USD ÷
  31.1034768 × TCMB USD/TRY.
  - It is not the Grand Bazaar or a bank's price; the domestic premium and the
    bank spread are not included.
  - There is no conversion for jewelry gold or 22 carat yet.

### TCMB CPI page

The annual and monthly changes of TurkStat's CPI, from 2005-01 to today, are
published as an HTML table. The program reads the table and chains the
monthly changes (see [METHOD.md](METHOD.md) §5.2). If the page layout changes,
the program stops with a clear error.

## Tried and not chosen for now

| Source | For | Why not for now |
|---|---|---|
| Yahoo Finance (`yfinance`, `GC=F`, `TRY=X`) | gold, rates, BIST | Unofficial. It returned `429 Too Many Requests` while testing. For gold it gives the futures price. To be looked at again for BIST in 03. |
| Stooq (`xauusd`) | gold | The CSV download now asks for a JavaScript check. |
| LBMA (`prices.lbma.org.uk`) | gold | Cloudflare 403. The prices are licensed by ICE. |
| EVDS `TP.MK.KUL.YTL` | grams of gold | Archived, monthly average. |
| Frankfurter's TCMB provider | past rates in one request | It gives the middle rate, not forex buying, and shifts dates by one day from the bulletin date. Could be an option for a fast first load. |
| `borsapy`, `borsa-mcp` | BIST, TEFAS, gold, currencies | Broad, but they use unofficial endpoints and are for personal and educational use. To be used as a map of data sources in 03. |
| `evdspy` | EVDS | A heavy package. A thin client does the same (`prices/evds.py`). |
| Open banking (ÖHVPS, HBHS) | bank transactions | Access is only for service providers licensed by TCMB; individuals get no API keys. That is why statements are imported. |

## Sources for later versions

- **03, TEFAS fund prices:** TEFAS's own JSON endpoints or `tefas-crawler`.
  TEFAS returns a short-lived `429` after a few requests; requests should be
  spaced out.
- **03, BIST stocks:** a data source is still to be chosen. Free sources are
  usually 15 minutes delayed.
- **03, interest rates:** the policy rate and deposit rates from EVDS, for
  comparing real returns and as the risk-free rate.
- **01 and 09, CPI subgroups:** the 2025=100 COICOP groups on EVDS. Needed for
  personal inflation.
