# financeproject 00

A personal finance program for a household in Türkiye. The goal covers the
personal balance sheet and net worth, transactions and cash flow, cash-flow
forecasting, investment and asset analytics, debt management, and a
mathematical analysis and scenario engine. Monarch and Firefly III served as
models. The models used come from academic work, and each one's source is
documented.

The project grows version by version. **00** lays the foundation: a
double-entry ledger, exchange rate, gold and CPI data, and net worth in four
units. What is in this version: [UPDATE_LOG.md](UPDATE_LOG.md). Methods and
references: [docs/METHOD.md](docs/METHOD.md). Data sources:
[docs/DATA_SOURCES.md](docs/DATA_SOURCES.md).

- **Double-entry ledger** (SQLite). Every transaction balances to zero in every
  unit. Buying and selling currency and gold goes through trading accounts
  (Selinger). Amounts are stored as `Decimal`; floats are never used.
- **Net worth in four units:**
  - Turkish lira, TRY (the headline unit)
  - real TRY (deflated by the CPI, in the prices of the latest published month)
  - US dollars (at the central bank's forex buying rate)
  - grams of gold
- **Liquidity tiers** (immediate, within a week, restricted, illiquid),
  short- and long-term debt, and a breakdown by person (me, my father, my
  brother, shared).
- **Data sources:**
  - the daily exchange rate bulletin of the Central Bank of the Republic of
    Türkiye (TCMB), no key needed
  - Frankfurter XAU/USD (central bank gold prices, no key needed)
  - the Turkish CPI from EVDS3 (with a key) or from the TCMB inflation table
  - a CPI copy shipped with the package, so it works offline
- **CSV import:** a standard layout, or any bank's export through a column
  mapping. Rows already imported are skipped, and an import is all or
  nothing. Categorization rules classify the rows.
- **Command line** (`finance …`), a net worth chart (PNG), and a made-up demo
  household to try things on.
- **Tests:** 110 pytest tests, among them property tests that check the
  ledger rules on random ledgers (Hypothesis) and a check of the CPI against
  TurkStat's real series.

![Demo household: net worth (made-up household, real exchange rates and gold prices)](docs/example_networth.png)

## Quick start

Requires Python 3.11 or newer. Create a virtual environment and install the
package with its development tools:

```sh
python -m venv .venv
# Activate it
#   Windows (PowerShell):  .venv\Scripts\Activate.ps1
#   macOS and Linux:       source .venv/bin/activate
pip install -e ".[dev]"     # sqlalchemy, pandas, httpx, typer, rich, platformdirs, matplotlib, pytest, hypothesis
```

With the environment active:

```sh
finance init                # creates your ledger in the user data directory
finance demo                # a separate demo ledger: made-up household, real prices
finance -d <path printed by demo> balance-sheet
pytest
```

`finance demo --synthetic` works offline with made-up prices. `python -m
financeproject` does the same as `finance`. On systems where `python` points
to Python 2 or is missing, use `python3`.

## Usage

Account names are paths separated by `:`. The first part is `Assets`,
`Liabilities`, `Equity`, `Income` or `Expenses`. Commands accept any unique
part of an account name instead of the full path: `Groceries` →
`Expenses:Food:Groceries`. Matching ignores case and Turkish letters, so
`Gida` finds `Gıda`.

```sh
# Accounts (kinds: finance account kinds)
finance account add "Assets:Bank:Ziraat Checking" --kind checking --institution "Ziraat Bankası" --owner Me
finance account add "Assets:FX:USD Account" --kind fx --commodity USD
finance account add "Assets:Gold:Grams" --kind gold                 # unit: XAU_G (24 carat, grams)
finance account add "Liabilities:Credit Card:Bonus" --kind credit_card
finance account add "Expenses:Food:Groceries"
finance account add "Income:Salary"

# Opening balances (natural sign: a debt is entered as a positive amount owed)
finance opening "Ziraat Checking" 25000 --date 2026-10-01
finance opening Bonus 3500 --date 2026-10-01

# Daily transactions (amounts may be written 1,234.56 or 1.234,56)
finance income 65000 Salary --to "Ziraat Checking"
finance expense 450.75 Groceries --from Bonus --payee Migros
finance transfer 3500 --from "Ziraat Checking" --to Bonus             # paying the card
finance exchange --from "Ziraat Checking" --paid 49126.70 --to "USD Account" --received 1000
finance txn add --description "Loan payment" -p "Liabilities:Loan:Consumer=3000" -p "Expenses:Interest=1500" -p "Ziraat Checking"

# Prices and reports
finance price update                    # TCMB rates, gold, CPI; fills in missing days
finance balance-sheet                   # today's balance sheet; --date, --owner Dad
finance networth --frequency month      # net worth over time (TRY, real TRY, USD, grams of gold); --csv
finance chart                           # networth.png in the output folder
finance cpi                             # the CPI of the last 13 months
finance backup                          # a copy in the backups folder
finance config show                     # where the files are
```

Numbers are shown as `1,234.56`. `finance config number-format "1.234,56"`
switches to the Turkish style.

### Importing (CSV)

The standard layout has these headers, in any order. Example file:
[examples/transactions_standard.csv](examples/transactions_standard.csv).

```
date,description,amount,account,counter_account,commodity,payee,note
2026-10-01,MİGROS KONYA,-450.00,Bonus,,,Migros,
2026-10-02,Salary,"65,000.00",Ziraat Checking,Income:Salary,,,
```

`amount` is the change of `account` from the holder's point of view:

- Negative: money out. A purchase made with a credit card counts as money out
  too.
- Positive: money in.

If `counter_account` is empty, the categorization rules classify the row. If
no rule matches, the row goes to `Expenses:Uncategorized` or
`Income:Uncategorized`.

A bank's own export is read through a column mapping:

```sh
finance import statement.csv --account "Ziraat Checking" --skip 2 --date-format "%d.%m.%Y" \
    --columns "date=İşlem Tarihi,description=Açıklama,outflow=Borç,inflow=Alacak" --dry-run
```

- `--dry-run` previews without writing anything.
- Importing the same file again, or overlapping statements, does not add the
  same rows twice.
- If any row is invalid, nothing is written and all errors are listed.
- The delimiter (`,` `;` tab `|`), the decimal separator and the encoding
  (UTF-8, Windows-1254, ISO-8859-9) are detected; `--delimiter`, `--decimal`
  and `--encoding` set them explicitly.

### Rules

```sh
finance rule add migros Groceries                  # "migros" in the description → Expenses:Food:Groceries
finance rule add "kahve\s+dunyasi" Coffee --regex
finance rule apply                                 # applies the rules to uncategorized transactions
```

### EVDS key (optional)

Everything works without a key. With a key the CPI comes as official index
levels and past exchange rates arrive in one request.

1. Sign up at https://evds3.tcmb.gov.tr.
2. Copy the API key from your profile page.
3. Save it: `finance config evds-key KEY`. It goes into `settings.toml` in your
   user configuration directory, readable only by you.

## Example output

A made-up household with real exchange rates and gold prices
(`finance demo`, 2026-10-09):

```
╭─ Net worth ─────────────────────────────────────────────────────────────────────────────────────────────────╮
│ TRY                      845,266.41                                                                         │
│ Real TRY                 845,266.41   in September 2026 prices; no CPI after that yet                       │
│ USD                       17,205.60   49.1274 TRY · 2026-10-09 · TCMB forex buying                          │
│ Grams of gold              129.15 g   6,545.04 TRY/g · 2026-10-09 · Frankfurter XAU/USD × TCMB forex buying │
╰─────────────────────────────────────────────────────────────────────────────────────────────────────────────╯
```

`finance networth` shows the same net worth over time. Over one year this
household's net worth grew by 105 % in TRY, 63 % in real terms and 75 % in
dollars. In grams of gold the growth was only 69 %, and from December to
January it fell from 79 g to 73 g: that month gold gained value faster than the
household saved. This is why the four units are shown together.

## Layout

| Module | Contents |
|---|---|
| `financeproject/core/` | Pure calculations, no database or network: amounts (`money`), dates, text folding, account kinds and liquidity (`kinds`), the price book (`prices`), CPI (`inflation`), valuation and net worth (`valuation`) |
| `financeproject/ledger/` | SQLAlchemy models, database, accounts, transaction rules (`transactions`), balance queries |
| `financeproject/prices/` | Clients for the TCMB XML, Frankfurter, EVDS3 and the TCMB CPI table; storing and updating |
| `financeproject/ingest/` | CSV import, categorization rules |
| `financeproject/reports/` | Balance sheet, net worth series, chart |
| `financeproject/cli.py` | Command line (`finance`) |
| `financeproject/demo.py` | The made-up demo household |
| `financeproject/data/` | Public data shipped with the package: the CPI copy ([description](financeproject/data/README.md)) |
| `tests/` | pytest + Hypothesis; `fixtures/` holds saved real TCMB and Frankfurter answers |
| `docs/` | Method, data sources, example chart |

## Data and privacy

The repository is public, so **personal data never goes into it**.

- **Location:** the ledger, backups and charts live outside the repository, in
  the user data directory of the operating system (`finance config show`
  prints it):

  | System | Folder |
  |---|---|
  | Windows | `%LOCALAPPDATA%\financeproject` |
  | macOS | `~/Library/Application Support/financeproject` |
  | Linux | `~/.local/share/financeproject` |

  `-d` or the `FINANCEPROJECT_DB` environment variable selects another file.
- **.gitignore:** patterns such as `*.sqlite`, `backups/`, `output/` and
  `settings.toml` keep such files out as a second safety net.
- **Permissions:** the ledger, backups and the settings file are created
  readable only by the user (0600 on macOS and Linux).
- **Demo data:** `finance demo` never writes into the real ledger.
- **Encryption:** the ledger file itself is not encrypted yet. An encrypted
  disk (BitLocker, FileVault, LUKS) is recommended. Encrypting the file itself
  (SQLCipher) is among the next steps.

## Design principles

The "points to watch" from the first evaluation, and how this version meets
them:

1. **The biggest risk is data entry.** Open banking in Türkiye (ÖHVPS) is open
   only to licensed providers, so statement import is taken seriously from the
   start:
   - **00:** CSV with column mapping, duplicate checks, rules, all-or-nothing
     imports.
   - **01:** bank-specific templates and a learning categorizer.
2. **The efficient frontier is fragile at household scale** (Michaud 1989;
   DeMiguel et al. 2009). Version 05 will show it with shrinkage
   (Ledoit-Wolf), resampling, a 1/N comparison and out-of-sample checks, as
   description, not advice.
3. **Say how current each number is.** Every value is shown with its price
   date and source. Prices older than 7 days and assets without a price are
   flagged; they are not guessed and not added to net worth. The last month
   with a known CPI is shown too.
4. **Inflation makes everything look "abnormal".** Real TRY is in this version.
   Forecasting (06) and anomaly detection (07) will work on real amounts.
5. **Privacy.** See "Data and privacy" above.

## Roadmap

| Version | Contents | Status |
|---|---|---|
| **00** | Ledger, accounts, manual entry, CSV import and rules, TCMB, gold and CPI, net worth in four units, liquidity | ✅ this version |
| **01** | Bank and card templates, **installment model**, cash flow and income statement, Sankey, recurring payments, household financial ratios, decomposition of net worth changes, learning categorizer | next |
| **02** | Debt module: payment schedules, true cost of credit (IRR), avalanche and snowball, cash or installments, real debt burden | |
| **03** | Investments: TEFAS and BIST prices, TWR and XIRR, real returns, volatility, maximum drawdown, VaR and CVaR with backtests, correlation, concentration | |
| **04** | Wall monitor (kiosk), guest mode | ⏸ deferred for now |
| **05** | Portfolio theory: efficient frontier, GMV, tangency portfolio, HRP, 1/N | |
| **06** | Cash-flow forecasting: known flows and statistical forecasts, prediction intervals, backtests | |
| **07** | Anomaly detection | |
| **08** | Scenario engine: scenarios, sensitivity (Sobol), Monte Carlo, historical stress tests | |
| **09** | Personal inflation, goals, budgets | |
| **10** | AI: MCP server, weekly summary, reading PDF statements | ⏸ deferred for now |

## Idea pool: what to take from similar apps

All of these ideas are meant to join the program over time. Any that turn out
not to fit will be changed or reworked. Status: ✅ done, ◐ partly, ☐ planned,
⏸ deferred.

| Source | Idea | Version | Status |
|---|---|---|---|
| Monarch Money | Sankey cash-flow diagram | 01 | ☐ |
| | Bill and subscription calendar | 01 | ☐ |
| | Shared household use | 00 | ◐ account owners and per-person balance sheets; no separate logins |
| | "Why did this month change?" | 01 / 10 | ☐ numbers first, then an AI-written explanation |
| | Weekly summary | 10 | ⏸ |
| | Forecasts | 06 | ☐ |
| Firefly III | Double-entry ledger | 00 | ✅ |
| | Rule engine | 00 / 01 | ◐ description or regex → account; amount and payee conditions later |
| | Expected bills | 01 | ☐ |
| | Savings goals ("piggy banks") | 09 | ☐ |
| | REST API | with a web interface | ☐ |
| | Separate import tool | 00 / 01 | ◐ CSV and column mapping; bank templates in 01 |
| Actual Budget | Envelope budgeting | 09 | ☐ |
| | Scheduled transactions | 01 | ☐ |
| | Custom reports | later | ☐ |
| Sure (Maybe) | Modern net worth interface | with a web interface | ☐ |
| | Access for AI agents | 10 | ⏸ |
| Ghostfolio | X-ray: concentration rules (institution, currency, single asset; TMSF deposit insurance limit) | 03 | ☐ |
| | Hiding the numbers (Zen mode) | 04 | ⏸ the monitor's guest mode |
| Wealthfolio | TWR and MWR side by side | 03 | ☐ |
| | Splitting a period's change into its sources (income, gains, fees, taxes, FX) | 01 | ☐ the trading accounts are ready for it |
| | API keys in the operating system keyring | later | ☐ for now a settings file readable only by the user |
| Portfolio Performance | TTWROR, IRR, maximum drawdown and its duration; a reference to check results against | 03 | ☐ |
| Beancount, smart_importer | Categorization that learns from past corrections | 01 | ☐ |
| hledger | `roi`: TWR and IRR side by side | 03 | ☐ |
| | Forecasts from recurring transactions | 06 | ☐ |
| | Balance sheet, cash flow, income statement | 00 / 01 | ◐ balance sheet done |
| Paisa | Bill calendar | 01 | ☐ |
| | Goals | 09 | ☐ |
| | Spreadsheet on ledger data | later | ☐ |
| ezBookkeeping | Light enough for a Raspberry Pi | 04 | ⏸ |
| | Receipt scanning (OCR), MCP | 10 | ⏸ |
| Rotki | Encrypted ledger (SQLCipher) | later | ☐ for now disk encryption and owner-only files |
| | Keeping non-sensitive price data apart | later | ☐ |
| ProjectionLab | Monte Carlo and historical backtests | 08 | ☐ |
| | Inspecting single runs, probability of reaching a goal | 08 / 09 | ☐ |

## Not done in this version

### From the first evaluation (later versions)

The installment model, cash flow, the debt module, investment analytics,
portfolio theory, forecasting, anomaly detection, the scenario engine and
personal inflation. The roadmap shows which version brings each. The wall
monitor and AI are deferred on purpose.

### Found while building 00

- **No bank templates.** Which banks the family uses, and in which formats
  their statements can be downloaded, is not known yet. For now there is a
  general CSV import with column mapping. Excel (`.xlsx`) and PDF statements
  cannot be read.
- **EVDS has not been tried with a real key.** The client follows TCMB's
  documentation, the error a request without a key returns, and published
  real answers, and it is tested on those. It should be tried live once a key
  is available.
- **The gold price is the international parity:** XAU/USD × USD/TRY.
  - It is not the Grand Bazaar (Kapalıçarşı) or a bank's price; their premium
    and spread are not included.
  - There is no conversion for jewelry gold coins (çeyrek, yarım) or 22
    carat gold.
  - Frankfurter's value is the average of about fourteen central banks; single
    banks can differ by ±1 %.
- **Past exchange rates download slowly without an EVDS key:** one request per
  day to TCMB, so a year takes about 1.5 minutes. With a key one request is
  enough.
- **TEFAS funds and BIST stocks** have no automatic prices; enter them with
  `finance price add`.
- **Real TRY stops at the latest published CPI month.** Today's value is shown
  in that month's prices, because the inflation since then is not known yet.
  Chaining the CPI from the TCMB table differs by at most 0.04 points because
  of rounding (see METHOD.md).
- **No database migrations.** The schema version is kept in the `meta` table;
  Alembic should be added when the schema changes.
- **The user interface is only the command line.** There is no web interface
  and no multi-user login.

## Next steps (proposal for 01)

1. **Bank and card templates:** ready-made mappings for the CSV, Excel or PDF
   statements of the banks the family uses, and reading `.xlsx`.
2. **Installment model:** an installment purchase is recorded as debt on the
   day it is made, and its payments are spread over the months; the
   installment load of the coming months is shown.
3. **Cash flow and income statement:** by category and month. A Sankey
   diagram.
4. **Recurring payments and subscriptions**, and a payment calendar.
5. **Household financial ratios:** emergency fund (how many months it lasts),
   savings rate, debt to income, debt service ratio.
6. **Decomposition of net worth changes:** net saving + price effects +
   exchange rate effects. The trading accounts are ready for this.
7. **Learning categorizer:** a model that learns from past corrections (the
   smart_importer approach).
