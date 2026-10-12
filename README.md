# financeproject 01

A personal finance program for a household in Türkiye. The goal covers the
personal balance sheet and net worth, transactions and cash flow, cash-flow
forecasting, investment and asset analytics, debt management, and a
mathematical analysis and scenario engine. Monarch and Firefly III served as
models. The models used come from academic work, and each one's source is
documented.

The project grows version by version. **00** laid the foundation: a
double-entry ledger, exchange rate, gold and CPI data, net worth in four units.
**01** brings the money in and explains it: bank statement import (Ziraat
Bankası), a categorizer that learns from the ledger, installment purchases,
the income statement, the cash flow statement, a Sankey diagram, recurring
payments, household financial ratios, and why net worth changed. What changed
in this version: [UPDATE_LOG.md](UPDATE_LOG.md). Methods and references:
[docs/METHOD.md](docs/METHOD.md). Data sources:
[docs/DATA_SOURCES.md](docs/DATA_SOURCES.md).

- **Double-entry ledger** (SQLite). Every transaction balances to zero in every
  unit. Buying and selling currency and gold goes through trading accounts
  (Selinger). Amounts are stored as `Decimal`; floats are never used.
- **Net worth in four units:** Turkish lira (TRY, the headline unit), real TRY
  (deflated by the CPI), US dollars and grams of gold.
- **Bank statements:** Ziraat Bankası's account statements in Excel (.xlsx) or
  HTML. Each statement is checked line by line against the bank's running
  balance and totals, a new account can start at the statement's opening
  balance, and the ledger is compared with the bank's closing balance
  afterwards. Downloading the same statement in the other format adds nothing
  twice. CSV files with any column layout still work.
- **Categories:** rules first, then a naive Bayes model learned from the
  ledger's own categorized transactions. `finance categorize --evaluate`
  measures how often its confident guesses are right.
- **Installments** (taksit): a card purchase in installments is a debt from
  the day it is made; its schedule shows the installment load of the coming
  months.
- **Reports:** balance sheet, net worth over time, income statement, cash flow
  statement, Sankey diagram, change in net worth split into saving, exchange
  results and revaluation (in TRY, real TRY, USD or grams of gold), household
  ratios against published benchmarks, recurring payments and the payments
  expected next.
- **Data sources:** the daily exchange rate bulletin of the Central Bank of the
  Republic of Türkiye (TCMB), Frankfurter XAU/USD for gold, the Turkish CPI
  from EVDS3 (with a key) or the TCMB inflation table, and a CPI copy shipped
  with the package.
- **Tests:** 158 pytest tests, among them property tests on random ledgers
  (Hypothesis) for the ledger rules and for the decomposition of net worth
  changes, a check of the CPI against TurkStat's real series, and made-up bank
  statements built after a real Ziraat Bankası export.

![Demo household: net worth (made-up household, real exchange rates and gold prices)](docs/example_networth.png)

![Demo household: where September's income went](docs/example_sankey.png)

## Quick start

Requires Python 3.11 or newer. Create a virtual environment and install the
package with its development tools:

```sh
python -m venv .venv
# Activate it
#   Windows (PowerShell):  .venv\Scripts\Activate.ps1
#   macOS and Linux:       source .venv/bin/activate
pip install -e ".[dev]"     # sqlalchemy, pandas, httpx, typer, rich, platformdirs, python-calamine, matplotlib, pytest, hypothesis
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
`Gida` finds `Gıda`. Every command explains itself with `--help`.

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
finance expense 36000 Electronics --from Bonus --installments 6   # 6 monthly installments from next month
finance transfer 3500 --from "Ziraat Checking" --to Bonus             # paying the card
finance exchange --from "Ziraat Checking" --paid 49126.70 --to "USD Account" --received 1000
finance txn add --description "Loan payment" -p "Liabilities:Loan:Consumer=3000" -p "Expenses:Interest=1500" -p "Ziraat Checking"

# Prices
finance price update                    # TCMB rates, gold, CPI; fills in missing days
finance config show                     # where the files are
```

Numbers are shown as `1,234.56`. `finance config number-format "1.234,56"`
switches to the Turkish style.

### Importing bank statements

Download the account movements ("Hesap Hareketleri") from Ziraat Bankası as
Excel or HTML, then:

```sh
finance import Hesap_Hareketleri.xlsx --account "Ziraat Checking" --opening --dry-run   # preview
finance import Hesap_Hareketleri.xlsx --account "Ziraat Checking" --opening
```

```
Ziraat Bankası statement, 2026-09-01 – 2026-09-30, 10 lines; balance 12,500.00 → 41,724.55
…
Opening balance added: 12,500.00 on 2026-09-01 (the balance before the statement's first line)
10 transactions added, 0 were already in, 7 uncategorized (…).
Balance check: the bank's balance on 2026-09-30 is 41,724.55; the ledger's is the same.
```

- The bank is recognized from the file. The lines are put in date order, and
  the bank's running balance must add up line by line; the debit and credit
  totals must match too. Otherwise nothing is imported.
- `--opening` is for a new account: it starts the account at the balance the
  statement had before its first line.
- After the import the ledger's balance is compared with the bank's closing
  balance. A difference means missing transactions or a missing opening
  balance.
- Lines carry the bank's receipt number, so the same statement downloaded as
  Excel and as HTML, or two overlapping statements, are imported once.
- Card purchases get the shop's name as payee, mobile transfers the other
  person's name.
- Only the transactions are read. The account holder's name, address and IBAN
  at the top of the statement are skipped.

### Importing CSV files

The standard layout has these headers, in any order. Example file:
[examples/transactions_standard.csv](examples/transactions_standard.csv).

```
date,description,amount,account,counter_account,commodity,payee,note
2026-10-01,MİGROS KONYA,-450.00,Bonus,,,Migros,
2026-10-02,Salary,"65,000.00",Ziraat Checking,Income:Salary,,,
```

`amount` is the change of `account` from the holder's point of view: negative
is money out (a credit card purchase too), positive is money in. A bank's own
CSV export is read through a column mapping:

```sh
finance import statement.csv --account "Ziraat Checking" --skip 2 --date-format "%d.%m.%Y" \
    --columns "date=İşlem Tarihi,description=Açıklama,outflow=Borç,inflow=Alacak" --dry-run
```

The delimiter, the decimal separator and the encoding (UTF-8, Windows-1254,
ISO-8859-9) are detected; `--delimiter`, `--decimal` and `--encoding` set them.
If any row is invalid, nothing is written and all errors are listed.

### Categories

Every imported row gets its counter account from, in this order: the file
itself, the first matching rule, the learned model (when it is at least 80 %
sure), or `Expenses:Uncategorized` / `Income:Uncategorized`.

```sh
finance rule add migros Groceries                  # "migros" in the description → Expenses:Food:Groceries
finance rule add "kahve\s+dunyasi" Coffee --regex
finance categorize --dry-run                       # rules, then the model, on uncategorized transactions
finance categorize
finance categorize --evaluate                      # how well would the model have done so far?
finance txn recategorize 251 "Expenses:Gifts"      # fix one; the model learns from it
```

On the demo household, guessing each transaction from the earlier ones only,
the model was right 88.6 % of the time; the 75 % of guesses that reached 80 %
probability were right 98.1 % of the time.

### Reports

```sh
finance balance-sheet                   # today's balance sheet; --date, --owner Dad
finance networth --frequency month      # net worth over time in four units; --csv
finance chart                           # networth.png in the output folder
finance income-statement --months 6     # income and expenses by category and month; --real, --depth, --csv
finance cash-flow --months 6            # where the money in the cash accounts came from and went
finance sankey --month 2026-09          # Sankey diagram (PNG)
finance change --month 2026-09          # why net worth changed; --unit real, usd or gold
finance ratios                          # household ratios against benchmarks
finance recurring                       # subscriptions, bills, rent, salary found in the ledger
finance upcoming --days 30              # payments expected next, card installments included
finance installments                    # installment purchases and their schedule
finance cpi                             # the CPI of the last 13 months
finance backup                          # a copy in the backups folder
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
(`finance demo`, 2026-10-12):

```
╭─ Net worth ─────────────────────────────────────────────────────────────────────────────────────────────────╮
│ TRY                      811,405.27                                                                         │
│ Real TRY                 811,405.27   in September 2026 prices; no CPI after that yet                       │
│ USD                       16,476.20   49.2471 TRY · 2026-10-12 · TCMB forex buying                          │
│ Grams of gold              122.70 g   6,612.70 TRY/g · 2026-10-12 · Frankfurter XAU/USD × TCMB forex buying │
╰─────────────────────────────────────────────────────────────────────────────────────────────────────────────╯
```

Over one year this household's net worth grew by 97 % in TRY, 57 % in real
terms and 68 % in dollars; in grams of gold by 61 %, and from December to
January it fell from 75 g to 68 g, because gold gained value faster than the
household saved. `finance change` shows where September's change came from:

```
Change in net worth · TRY
Net worth on 2026-08-31    730,206 TRY
Income                     +95,542 TRY
Expenses                   -55,009 TRY
Net saving                 +40,533 TRY
Revaluation                 -9,608 TRY
  XAU_G                    -11,656 TRY
  USD                       +2,048 TRY
Net worth on 2026-09-30    761,131 TRY
```

The household saved 40,533 TRY in September, but the gold it held lost 11,656
TRY of value. Measured in grams of gold the same month looks better: the lira
and dollars held gained 5.4 g against gold.

## Layout

| Module | Contents |
|---|---|
| `financeproject/core/` | Pure calculations, no database or network: amounts (`money`), dates, text folding, account kinds (`kinds`), the price book (`prices`), CPI (`inflation`), valuation (`valuation`), the split of a change in value (`decompose`), recurring series (`recurring`), HTML tables (`htmltable`) |
| `financeproject/ledger/` | SQLAlchemy models, database, accounts, transaction rules (`transactions`), installments, balance queries |
| `financeproject/prices/` | Clients for the TCMB XML, Frankfurter, EVDS3 and the TCMB CPI table; storing and updating |
| `financeproject/ingest/` | Reading files (`tables`), bank statements (`statement`) and their templates (`banks/`), CSV, writing imports (`importer`), rules, the learned categorizer (`learn`) |
| `financeproject/reports/` | Balance sheet, net worth series and chart, change in net worth (`change`), income statement (`income`), cash flow, Sankey, ratios, recurring payments |
| `financeproject/cli.py` | Command line (`finance`) |
| `financeproject/demo.py` | The made-up demo household |
| `financeproject/data/` | Public data shipped with the package: the CPI copy ([description](financeproject/data/README.md)) |
| `tests/` | pytest + Hypothesis; `fixtures/` holds saved real TCMB and Frankfurter answers and made-up Ziraat Bankası statements |
| `docs/` | Method, data sources, example figures |

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
- **Bank statements** are read on your computer; nothing is sent anywhere.
  Only the transactions are taken: the name, address and IBAN in the
  statement's header are not stored. The statements under `tests/fixtures`
  are made up.
- **.gitignore:** patterns such as `*.sqlite`, `backups/`, `output/` and
  `settings.toml` keep such files out as a second safety net.
- **Permissions:** the ledger, backups and the settings file are created
  readable only by the user (0600 on macOS and Linux).
- **Demo data:** `finance demo` never writes into the real ledger.
- **Encryption:** the ledger file itself is not encrypted yet. An encrypted
  disk (BitLocker, FileVault, LUKS) is recommended. Encrypting the file itself
  (SQLCipher) is among the next steps.

## Design principles

The "points to watch" from the first evaluation, and how the versions meet
them:

1. **The biggest risk is data entry.** Open banking in Türkiye (ÖHVPS) is open
   only to licensed providers, so statement import is taken seriously:
   - **00:** CSV with column mapping, duplicate checks, rules, all-or-nothing
     imports.
   - **01:** the Ziraat Bankası template; every statement checked against the
     bank's running balance and totals; the ledger compared with the bank's
     closing balance; a categorizer that learns from corrections, with its
     accuracy measured.
2. **The efficient frontier is fragile at household scale** (Michaud 1989;
   DeMiguel et al. 2009). Version 05 will show it with shrinkage
   (Ledoit-Wolf), resampling, a 1/N comparison and out-of-sample checks, as
   description, not advice.
3. **Say how current and how sure each number is.** Every value is shown with
   its price date and source. Prices older than 7 days and assets without a
   price are flagged; they are not guessed and not added to net worth. The
   learned categorizer only acts when it is sure enough, and reports its
   measured accuracy. The ratio benchmarks are labeled as signals, not advice.
4. **Inflation makes everything look "abnormal".** Real TRY is everywhere: the
   income statement and cash flow have `--real`, and the change in net worth
   shows how much inflation took from the lira held.
5. **Privacy.** See "Data and privacy" above.

## Roadmap

| Version | Contents | Status |
|---|---|---|
| **00** | Ledger, accounts, manual entry, CSV import and rules, TCMB, gold and CPI, net worth in four units, liquidity | ✅ |
| **01** | Bank statement template (Ziraat Bankası), installment model, cash flow and income statement, Sankey, recurring payments, household financial ratios, decomposition of net worth changes, learning categorizer | ✅ this version |
| **02** | Debt module: payment schedules, true cost of credit (IRR), avalanche and snowball, cash or installments, real debt burden | next |
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
| Monarch Money | Sankey cash-flow diagram | 01 | ✅ `finance sankey` |
| | Bill and subscription calendar | 01 | ◐ recurring payments are found and the next ones listed (`recurring`, `upcoming`); no reminders |
| | Shared household use | 00 | ◐ account owners and per-person balance sheets; no separate logins |
| | "Why did this month change?" | 01 / 10 | ◐ the numbers are there (`finance change`); an AI-written explanation in 10 |
| | Weekly summary | 10 | ⏸ |
| | Forecasts | 06 | ☐ |
| Firefly III | Double-entry ledger | 00 | ✅ |
| | Rule engine | 00 / 01 | ◐ description or regex → account; amount and payee conditions later |
| | Expected bills | 01 | ◐ found from the ledger, not entered by hand |
| | Savings goals ("piggy banks") | 09 | ☐ |
| | REST API | with a web interface | ☐ |
| | Separate import tool | 00 / 01 | ◐ CSV with column mapping and the Ziraat Bankası template; more banks as samples arrive |
| Actual Budget | Envelope budgeting | 09 | ☐ |
| | Scheduled transactions | 02 / 06 | ☐ recurring payments are detected; booking them ahead is open |
| | Custom reports | later | ☐ |
| Sure (Maybe) | Modern net worth interface | with a web interface | ☐ |
| | Access for AI agents | 10 | ⏸ |
| Ghostfolio | X-ray: concentration rules (institution, currency, single asset; TMSF deposit insurance limit) | 03 | ☐ |
| | Hiding the numbers (Zen mode) | 04 | ⏸ the monitor's guest mode |
| Wealthfolio | TWR and MWR side by side | 03 | ☐ |
| | Splitting a period's change into its sources (income, gains, fees, taxes, FX) | 01 | ✅ `finance change`, in four units |
| | API keys in the operating system keyring | later | ☐ for now a settings file readable only by the user |
| Portfolio Performance | TTWROR, IRR, maximum drawdown and its duration; a reference to check results against | 03 | ☐ |
| Beancount, smart_importer | Categorization that learns from past corrections | 01 | ✅ naive Bayes, evaluated in date order |
| hledger | `roi`: TWR and IRR side by side | 03 | ☐ |
| | Forecasts from recurring transactions | 06 | ☐ the recurring series of 01 are ready for it |
| | Balance sheet, cash flow, income statement | 00 / 01 | ✅ |
| Paisa | Bill calendar | 01 | ◐ `finance upcoming` |
| | Goals | 09 | ☐ |
| | Spreadsheet on ledger data | later | ☐ |
| ezBookkeeping | Light enough for a Raspberry Pi | 04 | ⏸ |
| | Receipt scanning (OCR), MCP | 10 | ⏸ |
| Rotki | Encrypted ledger (SQLCipher) | later | ☐ for now disk encryption and owner-only files |
| | Keeping non-sensitive price data apart | later | ☐ |
| ProjectionLab | Monte Carlo and historical backtests | 08 | ☐ |
| | Inspecting single runs, probability of reaching a goal | 08 / 09 | ☐ |

## Not done in this version

### From the 01 plan

- **Card statements.** Only an account statement sample was available, so
  there is no credit card (Bankkart) template yet. When it comes, the
  installment lines on card statements ("taksit 3/6") have to be matched to
  the installment plans instead of being booked again.
- **Other banks.** Only Ziraat Bankası has a template; a new bank needs one
  sample export.
- **PDF statements** (version 10, deferred).
- **Booking recurring payments ahead** (scheduled transactions) and bills
  entered by hand: 01 only detects recurring payments.
- **The housing cost ratio** of Greninger et al. needs to know which
  categories are housing costs; it is not computed.

### Found while building 01

- **One sample statement.** The Ziraat template follows a real export with four
  card and transfer lines. Other kinds of lines (EFT, FAST, fees, salary) are
  imported, but their payee is not recognized yet.
- **openpyxl cannot open Ziraat's Excel file** (an empty fill in its style
  sheet). Excel files are therefore read with python-calamine.
- **Installments:** the first installment is taken to fall due in the month
  after the purchase; the card's statement day is not modeled, so
  `--first-due` sets it when it differs.
- **Learned categories:** naive Bayes probabilities are not calibrated, so the
  80 % threshold is a choice checked by `--evaluate`, not a guarantee. With
  few examples per shop the model stays unsure and leaves transactions
  uncategorized.
- **Cash** in the cash flow statement means liquidity tier 0 (cash, checking,
  foreign currency accounts); time deposits count as investing.
- **Ratios:** the benchmarks come from US financial planners in 1996. Income is
  recorded after tax, which matches the paper's debt ratios but not its
  savings ratio (gross income). Inflation protection needs a full year of
  data.
- **Recurring payments** are found by a transparent rule (median gap,
  regularity, at least three payments), not a fitted model. A series whose
  payee name changes is split in two.
- **Change in net worth:** commodities without a price on some day of the
  period are left out of the split and listed.
- **EVDS has still not been tried with a real key**, and the gold price is
  still the international parity (see 00).

## Next steps (proposal for 02)

1. **Debt module:** payment schedules (annuity, equal principal), the true cost
   of a loan or of an installment purchase as an internal rate of return,
   avalanche and snowball repayment orders, "cash or installments" by present
   value, and the real burden of debt under inflation.
2. **Card statement template**, once a sample is available, matched with the
   installment plans.
3. **Templates for the other banks** the family uses, one sample each.
4. **Bills and scheduled transactions:** confirm a detected recurring payment
   as a bill, get warned when it is late or its amount changes.
