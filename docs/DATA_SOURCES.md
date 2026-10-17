# Data sources

Which data come from where, why that source was chosen, and what to watch
out for. Prices and the CPI as of 2026-10-09, bank statements as of
2026-10-12, fund and share prices and card lists as of 2026-10-17.

## In use

| Data | Source | Key | Frequency | Module |
|---|---|---|---|---|
| Exchange rates (USD, EUR, …) | TCMB indicative rates, XML | not needed | business days, ~15:30 | `prices/tcmb.py` |
| Exchange rates (a range) | TCMB EVDS3 `TP.DK.USD.A.YTL`, `TP.DK.EUR.A.YTL` | needed (free) | daily | `prices/evds.py` |
| Gold (XAU/USD) | Frankfurter v2, gold prices of central banks | not needed | daily | `prices/frankfurter.py` |
| CPI (index) | TCMB EVDS3 `TP.TUKFIY2025.GENEL` (2025=100) | needed (free) | monthly | `prices/evds.py` |
| CPI (changes) | TCMB "Tüketici Fiyatları" (consumer prices) page | not needed | monthly | `prices/tcmb_cpi.py` |
| CPI (copy) | The same table, shipped with the package | — | up to 2026-09 | `financeproject/data/` |
| Fund prices | TEFAS, the site's JSON API | not needed | business days | `prices/tefas.py` |
| Share prices (BIST) | Yahoo Finance chart API (unofficial) | not needed | trading days | `prices/yahoo.py` |

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

### TEFAS (funds)

- **Address:** `POST https://www.tefas.gov.tr/api/funds/fonFiyatBilgiGetir`
  with the JSON body `{"fonKodu": "AFT", "dil": "TR", "periyod": 3}`. The site
  moved to this API in 2026; the older `/api/DB/BindHistoryInfo` endpoints now
  answer 404.
- **Look-back:** `periyod` is 1, 3, 6, 12, 36 or 60 months back from today, for
  one fund at a time. Nothing older than five years.
- **Answer:** `{"errorCode": null, "errorMessage": null, "resultList":
  [{"fonKodu": "AFT", "fonUnvan": "…", "tarih": "2026-05-04", "fiyat":
  1.234567, …}]}`. An unknown code returns an empty list.
- **Price:** TRY per share, with up to six decimals, for the day in `tarih`.
  Orders are executed at the price of a later day (the fund's rules), so a
  purchase booked on its order day is valued with that day's price.
- **Spacing:** requests are spaced 0.3 s apart; TEFAS has answered bursts with
  short-lived `429`s.
- **Codes:** a fund is the commodity `TEFAS.<code>` with the price source
  `tefas` (`finance commodity add TEFAS.AFT "…" --kind fund --price-source tefas`).

### Yahoo Finance (shares)

- **Address:** `GET https://query1.finance.yahoo.com/v8/finance/chart/THYAO.IS?period1=…&period2=…&interval=1d&events=split`.
  Free, no key, but not an official or documented service: it may change or
  refuse requests.
- **Answer:** the currency and the time zone offset (`meta`), the trading days
  (`timestamp`), the closes (`indicators.quote[0].close`) and the splits
  (`events.splits`, numerator and denominator).
- **Splits:** the closes are adjusted for every later split, so the program
  undoes the adjustment (METHOD §23); the request always runs to today. On
  FROTO.IS, for example, the close of 2025-05-06 comes as 87.75 and is stored
  as 877.50, the price of that day, because of the 10-for-1 split of
  2025-05-07.
- **Dividends** are not fetched: they are booked as income when they are paid.
- **Codes:** a Borsa İstanbul share is `BIST.<ticker>` with the price source
  `yahoo`; its Yahoo symbol is `<ticker>.IS`. Another code is taken as a Yahoo
  symbol as it is, and its prices are stored in the currency Yahoo gives.

### TCMB CPI page

The annual and monthly changes of TurkStat's CPI, from 2005-01 to today, are
published as an HTML table. The program reads the table and chains the
monthly changes (see [METHOD.md](METHOD.md) §5.2). If the page layout changes,
the program stops with a clear error.

## Bank statements

### Ziraat Bankası account statements

Internet banking offers the account movements ("Hesap Hareketleri") of a
period as Excel (.xlsx) or HTML. Both hold the same table. The template
follows the layout of the bank's downloads and is tested on made-up
statements with the same layout (`tests/fixtures/make_ziraat_samples.py`).

| | Excel (.xlsx) | HTML |
|---|---|---|
| Columns | Tarih, Fiş No, Açıklama, İşlem Tutarı, Bakiye | Tarih, Fiş No, Açıklama, Tutar, Bakiye |
| Order | newest first | newest first |
| Dates | text, `DD.MM.YYYY` | text, `DD.MM.YYYY` |
| Amounts | numbers (`-389.9`) | Turkish text (`-1.234,56`) |
| Period | "`01.09.2026 - 30.09.2026` tarihleri arasındaki…" | `Dönem : 01.09.2026-30.09.2026` |
| Currency | last word of the account number line (`TL`) | `Döviz Cinsi : TRY` |
| Totals | `Borç:-30.413,68` and `Alacak:53.000` in one cell each | `Borç:` and `Alacak:` with the amount in the amount column |
| Other | logo, account holder, IBAN, legal footer | account holder, branch, address, IBAN; a bot-protection script before the page |

- **Receipt number (Fiş No):** the bank's number for each line. It makes the
  import key independent of the description, so the Excel and HTML downloads
  of the same period give the same keys.
- **Descriptions** tell the kind of most lines (the made-up statements in
  the tests have one of each):

  | Kind | Description | Payee |
  |---|---|---|
  | card | `POS ALIŞVERİŞ KART NO: … İŞYERİ: <merchant> MUTABAKAT: <number>` (online: `SANAL POS ALIŞVERİŞ …`) | the merchant |
  | transfer | `<name> Ziraat Mobil Havale`; lines with the word `FAST` | the name (mobile transfers) |
  | fee | `MESAJ ÜCRETİ TUTARI`, `KOMİSYON TUTARI`, and `BSMV TUTARI` for the tax on them | Ziraat Bankası |
  | cash | starts with `ATM` | ATM |
  | bill | `<company>/ Abone no: … / Fatura no: …` | the company |
  | loan | `<loan number> hesap <n>. taksit ödeme tutarı` | Loan \<number\> |

  Other lines are imported with their description as it is. A loan line whose
  number belongs to a loan defined in the ledger is split into principal,
  interest and taxes (METHOD §17).
- **The Excel file** is written by a generator that leaves out the cell
  references and puts an empty `<fill/>` into the style sheet. openpyxl refuses
  the file; python-calamine (a reader written in Rust, also used by pandas)
  opens it, and the cells are placed in order.
- **Personal data** in the header (name, address, IBAN, account number) is not
  read; only the table, the period, the currency and the totals are.

### Ziraat Bankası card lists

Internet banking shows a credit card's transactions ("Kart Hareketleri") as a
web page that can be saved as PDF. The PDF is one tall drawing; every page
shows a slice of it, so the same text is on every page. The template is tested
on a made-up PDF built the same way (`tests/fixtures/make_ziraat_card_sample.py`).

| | |
|---|---|
| Block of a transaction | amount and `TL` at the right; the description below it; the day and short month (`17 Eki`) and the year at the left |
| Order | newest first |
| Amounts | `1.234,56`; spending without a sign, payments and refunds with `+` |
| Installments | the month's installment; the purchase day before the description (`15/07 …`); a note `(9.450,00 TL İşlemin 1/3 Taksidi)` |
| Other | the holder's name above the list, buttons (`TAKSİTLENDİR`, `ERTELE`) beside some lines, the bank's address below |
| Not there | balances, totals, the period, the card number |

| Kind | Description | Payee |
|---|---|---|
| card | the shop, then the place (province, sometimes a plate-code abbreviation before it) | the shop |
| installment | `<dd/mm> <shop> <place>` with the note | the shop |
| bill | `<subscriber number> [<company>] FATURA ÖDEME` | the company |
| payment | `Ödeme - Teşekkür Ederiz -` | — |
| refund | any other line with `+` | the shop |

- Lines in another currency than TL are refused for now.
- The list carries no balances or totals, so its lines cannot be checked
  against them; re-importing an overlapping list adds nothing twice (the
  import key is the date, amount and description).
- **Personal data:** only the transaction blocks are read; the holder's name
  is not stored.

## Tried and not chosen for now

| Source | For | Why not for now |
|---|---|---|
| Yahoo Finance (`GC=F`, `TRY=X`) | gold, rates | Unofficial, and for gold it gives the futures price. Used since 03 for BIST shares only (its chart API answered well in October 2026). |
| TEFAS's old endpoints (`/api/DB/BindHistoryInfo`) | fund prices | Retired in 2026: `404 Method not found or disabled`. The site's own pages also reject automated requests. |
| pdftotext (Poppler) | reading PDFs | A separate program to install on each system; pypdf is pure Python and gives the positions the template needs. |
| Stooq (`xauusd`) | gold | The CSV download now asks for a JavaScript check. |
| LBMA (`prices.lbma.org.uk`) | gold | Cloudflare 403. The prices are licensed by ICE. |
| EVDS `TP.MK.KUL.YTL` | grams of gold | Archived, monthly average. |
| Frankfurter's TCMB provider | past rates in one request | It gives the middle rate, not forex buying, and shifts dates by one day from the bulletin date. Could be an option for a fast first load. |
| `borsapy`, `borsa-mcp`, `tefas-crawler` | BIST, TEFAS, gold, currencies | Broad, but they use unofficial endpoints and are for personal and educational use. `tefas-crawler` showed where TEFAS's new API is; the program calls it directly. |
| `evdspy` | EVDS | A heavy package. A thin client does the same (`prices/evds.py`). |
| openpyxl | reading bank Excel files | Refuses Ziraat Bankası's export (an empty fill in its style sheet); python-calamine reads it. |
| Open banking (ÖHVPS, HBHS) | bank transactions | Access is only for service providers licensed by TCMB; individuals get no API keys. That is why statements are imported. |

## Sources for later versions

- **05, interest rates:** the policy rate and deposit rates from EVDS, as the
  risk-free rate for the portfolio models and for comparing returns.
- **Dividends:** Yahoo's dividend events could suggest the income to book.
- **01 and 09, CPI subgroups:** the 2025=100 COICOP groups on EVDS. Needed for
  personal inflation.
