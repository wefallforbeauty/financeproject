# Update log

## Unreleased

### Name, language and platforms

- The project is now called **financeproject** (it was Hane-Finans). The
  GitHub repository, the Python package (`financeproject`) and the command
  (`finance`) carry the new name.
- Everything in the repository is in English: code, messages, commands,
  account names, documentation and tests.
  - Account roots: `Assets`, `Liabilities`, `Equity`, `Income`, `Expenses`.
  - Account kinds: `checking`, `time_deposit`, `fx`, `gold`, `credit_card`,
    `consumer_loan`, … (`finance account kinds`).
  - Commands: `finance init`, `account add`, `expense`, `balance-sheet`,
    `networth`, … (`finance --help`).
- The setup no longer assumes Linux. Personal data lives in the user data
  directory of each operating system (found with `platformdirs`); the
  instructions show Windows, macOS and Linux.
- Numbers are written `1,234.56` by default; `finance config number-format
  "1.234,56"` switches to the Turkish style. Dates are shown as ISO
  (2026-10-09).
- The CSV import detects the delimiter and the decimal separator from the
  file. A file whose amounts are all ambiguous (`1.234`) needs `--decimal`.
- Ledgers made by 00 have Turkish account names and are refused with a clear
  message (schema version 2).
- Fixed: account paths such as `Assets:Bank:Checking` were printed with an
  emoji, because the terminal library read `:bank:` as an emoji code.

The calculations did not change: on the same made-up household, version 00 and
this version give identical net worth tables.

## 00 (2026-10-09)

The first version. The foundation of the program: a double-entry ledger,
exchange rate, gold and CPI data, net worth in four units. The plan and the
reasons: the roadmap and the design principles in the README.

### Structure

- Python package `hane_finans` (renamed to `financeproject` after 00). Layers:
  - `core`: pure calculations; no database, no network
  - `ledger`: SQLite + SQLAlchemy
  - `prices`: data sources
  - `ingest`: importing
  - `reports`: reports
  - `cli`: command line
- Command line `finans` (Typer + Rich). The commands were in Turkish; account
  and person names can be typed without Turkish letters.
- 106 pytest tests:
  - saved real TCMB and Frankfurter answers
  - random ledgers with Hypothesis
  - a check against TurkStat's 2005–2026 CPI series
  - the command line tried end to end

### Ledger

- Double entry with signed amounts (assets and expenses +, liabilities, income
  and equity −). The postings of a transaction add up to zero in every unit.
  A posting left without an amount takes the balance.
- Amounts are `Decimal`, stored as text in the database; no floats. Each unit
  has a limit on decimal places: TRY 2, grams of gold 4.
- Buying and selling currency, gold and funds goes through Selinger's trading
  accounts. A transaction balances in each unit on its own, and currency or
  price effects can be separated later.
- Account kinds, liquidity tiers and debt terms:
  - assets: cash, checking, time deposit, foreign currency, gold, fund, stock,
    private pension (BES)…
  - debts: credit card, overdraft (KMH), consumer loan, mortgage, vehicle
    loan…
  - the account's owner (a person) or a shared account
  - institution
  - opening and closing dates
- Helpers:
  - opening balance (natural sign)
  - expense, income, transfer
  - currency/gold exchange (the rate goes into the note)
  - general transaction

### Prices and CPI

- **Currencies:** the TCMB daily XML bulletin, no key. Valuation uses the forex
  buying rate; the price date is the bulletin date. With an EVDS key a range
  comes in one request (`TP.DK.USD.A.YTL`, `TP.DK.EUR.A.YTL`).
- **Gold:** Frankfurter v2 XAU/USD. The average of about fourteen central
  banks, since 1999, no key. Stored per gram in USD; the TRY price follows from
  USD/TRY.
- **CPI:**
  - EVDS3 `TP.TUKFIY2025.GENEL` (2025=100, with a key).
  - Without a key, the monthly changes on the TCMB inflation page are chained
    and scaled so that the 2025 average is 100.
  - A copy of the TCMB table ships with the package (2005-01 – 2026-09).
  - The chained index matches all 249 published annual changes within 0.037
    points (0.008 on average).
- **The move to EVDS3:** EVDS moved to evds3 in 2025. The key now goes in the
  `key` HTTP header; the old `TP.FG.J0` and `TP.MK.KUL.YTL` series were
  archived. The client was written for this.
- **Price priority:** an automatic update never overwrites a price entered by
  hand. For the CPI the order is EVDS > TCMB table > bundled copy.

### Valuation and reports

- **Net worth in four units:**
  - TRY: the headline unit
  - real TRY: in the prices of the latest CPI month. The daily price level is
    interpolated log-linearly between CPI values placed in the middle of each
    month.
  - USD
  - grams of gold
- **Prices are never guessed:**
  - An asset without a price is left out of net worth with a warning.
  - A price older than 7 days is marked "stale".
  - Each value is shown with its price date and source.
- **Liquidity:** assets by tier, short- and long-term debt, liquid net assets.
- **Breakdown by person:** the balance sheet of one person or of the shared
  accounts.
- **Reports:**
  - net worth over time, monthly, weekly or daily, with CSV output.
  - a PNG chart. The four units have separate panels; one axis each, no dual
    axes.

### Import and rules

- **CSV:** the standard layout or a column mapping.
  - The amount can be one column, or money in and out in separate columns.
  - Lines before the header can be skipped.
  - The date format can be given, and the sign can be inverted.
  - The encoding is detected (UTF-8, Windows-1254, ISO-8859-9).
- **Duplicate check:** each row has an import key (a hash of the account,
  date, amount, description and the occurrence number of identical rows). The
  same or overlapping statements are not added twice.
- **All or nothing:** if any row is invalid, nothing is written. A dry run
  previews the import.
- **Categorization rules:** text or regex, by priority. They can also be
  applied to uncategorized transactions later.

### Privacy and tools

- **Location:** personal data lives outside the repository; files are created
  readable only by the user. `.gitignore` is a second safety net.
- **EVDS key:** read from an environment variable or from a settings file
  readable only by the user.
- **Backup:** a consistent copy through SQLite's backup API.
- **Demo household:** a made-up household goes into a separate ledger; writing
  it into the real ledger is refused.
- **Status:** a summary of the ledger, including the check that every unit
  sums to zero.
