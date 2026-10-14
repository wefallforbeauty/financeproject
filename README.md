# financeproject 02

A personal finance program for a household in Türkiye. The goal covers the
personal balance sheet and net worth, transactions and cash flow, cash-flow
forecasting, investment and asset analytics, debt management, and a
mathematical analysis and scenario engine. Monarch and Firefly III served as
models. The models used come from academic work, and each one's source is
documented.

The project grows version by version. **00** laid the foundation: a
double-entry ledger, exchange rate, gold and CPI data, net worth in four units.
**01** brought the money in and explained it: bank statement import (Ziraat
Bankası), a categorizer that learns from the ledger, installment purchases,
the income statement, the cash flow statement, a Sankey diagram, recurring
payments, household financial ratios, and why net worth changed. **02** is
about debt: loans with their payment plans, payments split into principal,
interest and taxes (also straight from bank statements), the annual cost rate
of any credit offer, cash or installments, plans for paying off several debts,
and what debts weigh once inflation is counted. What changed in this version:
[UPDATE_LOG.md](UPDATE_LOG.md). Methods and references:
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
  twice. Card purchases, transfers, fees, cash withdrawals, bills and loan
  installments are recognized. CSV files with any column layout still work.
- **Categories:** rules first, then a naive Bayes model learned from the
  ledger's own categorized transactions. `finance categorize --evaluate`
  measures how often its confident guesses are right.
- **Installments** (taksit): a card purchase in installments is a debt from
  the day it is made; its schedule shows the installment load of the coming
  months.
- **Loans:** the terms of a loan give its payment plan (equal installments or
  equal principal, with KKDF and BSMV) and its annual cost rate (yıllık
  maliyet oranı). Each payment is split into principal, interest and taxes,
  including installment lines read from a bank statement. Early repayments and
  installments collected in parts are handled.
- **Debt decisions:** the annual cost rate of any credit offer, cash or
  installments in today's money, avalanche and snowball plans for paying off
  several debts, and every debt's real burden under inflation.
- **Reports:** balance sheet, net worth over time, income statement, cash flow
  statement, Sankey diagram, change in net worth split into saving, exchange
  results and revaluation (in TRY, real TRY, USD or grams of gold), household
  ratios against published benchmarks, recurring payments and the payments
  expected next (loan installments included), debts.
- **Data sources:** the daily exchange rate bulletin of the Central Bank of the
  Republic of Türkiye (TCMB), Frankfurter XAU/USD for gold, the Turkish CPI
  from EVDS3 (with a key) or the TCMB inflation table, and a CPI copy shipped
  with the package.
- **Tests:** 186 pytest tests, among them property tests on random ledgers
  (Hypothesis) for the ledger rules and for the decomposition of net worth
  changes, a check of the CPI against TurkStat's real series, a bank's
  published example of the annual cost rate, and made-up bank statements in
  Ziraat Bankası's formats.

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
finance txn add --description "Gift" -p "Expenses:Gifts=1500" -p "Ziraat Checking"

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
Ziraat Bankası statement, 2026-09-01 – 2026-09-30, 16 lines; balance 12,500.00 → 35,086.32
…
Opening balance added: 12,500.00 on 2026-09-01 (the balance before the statement's first line)
16 transactions added, 1 loan installment split, 0 were already in, 13 uncategorized (…).
Balance check: the bank's balance on 2026-09-30 is 35,086.32; the ledger's is the same.
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
- The payee is read from the description: the shop of a card purchase, the
  other person of a mobile transfer, the company of a bill; the bank's fees
  and tax get the bank, cash withdrawals "ATM".
- A loan installment line names the loan's number. When a loan with that
  number is defined (`finance loan add … --reference`), the line is split into
  principal, interest and taxes; otherwise the import says which loan numbers
  it saw.
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
sure), or `Expenses:Uncategorized` / `Income:Uncategorized`. Installment lines
of defined loans go to the loan.

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

### Loans and debts

A loan is a liability account plus its terms. Rates are as in the contract: the
monthly interest rate, without KKDF and BSMV (15 % of the interest each on
consumer and vehicle loans by default, 0 on others; `--kkdf` and `--bsmv`
change them).

```sh
# A new loan, paid out into the checking account; its statement lines are recognized by --reference
finance loan add "Car loan" "Liabilities:Loans:Car" --principal 300000 --rate 2.99 --count 36 \
    --first-due 2026-11-15 --paid-into "Ziraat Checking" --reference 12345678-0001
# A loan taken before the ledger: the installments due by --opening-on count as paid.
# --installment instead of --rate: the rate is worked out from the installment on your papers.
finance loan add "Old loan" "Liabilities:Loans:Old" --principal 100000 --installment 6924.20 --count 24 \
    --first-due 2026-05-13 --opening-on 2026-10-01
finance loan list                                   # owed, rates, annual cost rate, next installment, left to pay
finance loan schedule "Car loan" --csv plan.csv     # the payment plan, with what is paid
finance loan pay "Car loan" --from "Ziraat Checking"                      # the next installment, as planned
finance loan pay "Car loan" --from "Ziraat Checking" --amount 20000 --extra  # an early repayment

finance debt                                        # every debt, and the installments left in today's money
finance payoff --budget 15000 --rate "Bonus=3.5" --minimum "Bonus=3000"   # avalanche or snowball
finance apr --amount 10000 --count 12 --installment 888.49 --last 888.47 --fees 50
finance cash-or-installments --cash 34000 --installment 6000 --count 6 --rate 2.5
```

The annual cost rate of a credit offer, here the worked example of İş
Bankası's note on the yıllık maliyet oranı:

```
Credit 10,000.00; 12 installments of 888.49 (the last 888.47), the first in 1 month; fees 50.00
Paid back in all: 10,711.86 (cost 711.86)
Annual cost rate (yıllık maliyet oranı): 13.75 %
Cost a month: 1.079 %; a year 12.95 % simple (× 12), 13.75 % compounded
```

Cash or installments, when the money would earn 2.5 % a month meanwhile:

```
Cash 34,000.00 now, or 6 × 6,000.00 = 36,000.00, the first in 1 month.
The installments charge 1.66 % a month (21.81 % a year) over the cash price.
Money kept meanwhile: 2.50 % a month (34.49 % a year; given).
In today's money the installments cost 33,048.75: 951.25 less than cash → the installments are cheaper.
```

Without `--rate` the installments are discounted at the CPI's inflation of the
last twelve months: a comparison in real terms.

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
finance upcoming --days 30              # payments expected next: recurring, card installments, loan installments
finance installments                    # installment purchases and their schedule
finance debt                            # debts: cost, real rate, what is left in today's money
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
(`finance demo`, 2026-10-14):

```
╭─ Net worth ─────────────────────────────────────────────────────────────────────────────────────────────────╮
│ TRY                      779,595.80                                                                         │
│ Real TRY                 779,595.80   in September 2026 prices; no CPI after that yet                       │
│ USD                       15,811.99   49.3041 TRY · 2026-10-14 · TCMB forex buying                          │
│ Grams of gold              117.88 g   6,613.64 TRY/g · 2026-10-14 · Frankfurter XAU/USD × TCMB forex buying │
╰─────────────────────────────────────────────────────────────────────────────────────────────────────────────╯
```

From the end of October 2025 to 14 October 2026 this household's net worth
grew by 102 % in TRY, 60 % in real terms and 72 % in dollars; in grams of gold
by 64 %, and from November to January it fell from 74 g to 65 g, because gold
gained value faster than the household saved. `finance change` shows where
September's change came from:

```
Change in net worth · TRY
Net worth on 2026-08-31    698,681 TRY
Income                     +95,542 TRY
Expenses                   -55,478 TRY
Net saving                 +40,065 TRY
Revaluation                 -9,608 TRY
  XAU_G                    -11,656 TRY
  USD                       +2,048 TRY
Net worth on 2026-09-30    729,138 TRY
```

The household saved 40,065 TRY in September, but the gold it held lost 11,656
TRY of value. Measured in grams of gold the same month looks better: the lira
and dollars held gained 5.1 g against gold.

The household also pays a consumer loan taken before the ledger starts and
pays back money borrowed from a friend. `finance debt` (some columns left
out):

```
Debts · 2026-10-14 · inflation 29.7 % a year (CPI, 12 months to 2026-09)
Debt                                          Owed  Rate a month  Cost a year  Real a year  Left to pay  In today's money
Liabilities:Credit Card:Sample Card         42,428.02             —            —            —            —                 —
Consumer loan                               29,305.86       2.990 %      58.03 %      21.82 %    34,035.89         31,785.92
Liabilities:Personal:Borrowed from a Friend  5,000.00             —            —            —            —                 —
Liabilities:Credit Card:Sample Card: 12,708.16 of the 42,428.02 owed are installments billed in later months.
```

The loan's contractual rate of 2.99 % a month becomes 3.887 % with KKDF and
BSMV and 58 % a year compounded; after 29.7 % inflation it still costs 21.8 %
a year in real terms. With 9,000 a month for the two debts,
`finance payoff --budget 9000 --minimum "Friend=1250"`:

```
Budget 9,000.00 a month: least payments 6,112.27 + extra 2,887.73
Plan                           Debt-free in  Interest and taxes  Order (month paid off)
As now: least payments only        7 months            4,730.02  Borrowed from a Friend (4), Consumer loan (7)
Avalanche: highest rate first      5 months            3,024.10  Borrowed from a Friend (4), Consumer loan (5)
Snowball: smallest debt first      5 months            3,305.31  Borrowed from a Friend (2), Consumer loan (5)
Avalanche saves 281.21 in interest; snowball clears a first debt sooner, which helps some people keep going.
```

## Layout

| Module | Contents |
|---|---|
| `financeproject/core/` | Pure calculations, no database or network: amounts (`money`), dates, text folding, account kinds (`kinds`), the price book (`prices`), CPI (`inflation`), valuation (`valuation`), the split of a change in value (`decompose`), recurring series (`recurring`), loans and the annual cost rate (`loans`), HTML tables (`htmltable`) |
| `financeproject/ledger/` | SQLAlchemy models, database, accounts, transaction rules (`transactions`), installments, loans and their payments (`loans`), balance queries |
| `financeproject/prices/` | Clients for the TCMB XML, Frankfurter, EVDS3 and the TCMB CPI table; storing and updating |
| `financeproject/ingest/` | Reading files (`tables`), bank statements (`statement`) and their templates (`banks/`), CSV, writing imports (`importer`), rules, the learned categorizer (`learn`) |
| `financeproject/reports/` | Balance sheet, net worth series and chart, change in net worth (`change`), income statement (`income`), cash flow, Sankey, ratios, recurring payments, debts (`debt`) |
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
  are made up, and so are all names, numbers and amounts in the examples.
- **Loans:** their terms and numbers are kept in your ledger only.
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
   - **02:** more kinds of statement lines recognized; loan installments on a
     statement are split into principal, interest and taxes without typing.
2. **The efficient frontier is fragile at household scale** (Michaud 1989;
   DeMiguel et al. 2009). Version 05 will show it with shrinkage
   (Ledoit-Wolf), resampling, a 1/N comparison and out-of-sample checks, as
   description, not advice.
3. **Say how current and how sure each number is.** Every value is shown with
   its price date and source. Prices older than 7 days and assets without a
   price are flagged; they are not guessed and not added to net worth. The
   learned categorizer only acts when it is sure enough, and reports its
   measured accuracy. The ratio benchmarks are labeled as signals, not advice.
   The debt tools state their assumptions (the inflation used, rates given by
   hand, no early repayment fees) and show both repayment orders instead of
   choosing one.
4. **Inflation makes everything look "abnormal".** Real TRY is everywhere: the
   income statement and cash flow have `--real`, the change in net worth shows
   how much inflation took from the lira held, and 02 gives every loan a real
   interest rate and its remaining installments in today's money.
5. **Privacy.** See "Data and privacy" above.

## Roadmap

| Version | Contents | Status |
|---|---|---|
| **00** | Ledger, accounts, manual entry, CSV import and rules, TCMB, gold and CPI, net worth in four units, liquidity | ✅ |
| **01** | Bank statement template (Ziraat Bankası), installment model, cash flow and income statement, Sankey, recurring payments, household financial ratios, decomposition of net worth changes, learning categorizer | ✅ |
| **02** | Debt module: loans and payment plans, payments split from statements, the annual cost rate, avalanche and snowball, cash or installments, real debt burden | ✅ this version |
| **03** | Investments: TEFAS and BIST prices, TWR and XIRR, real returns, volatility, maximum drawdown, VaR and CVaR with backtests, correlation, concentration | next |
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
| | Bill and subscription calendar | 01 / 02 | ◐ recurring payments, card and loan installments are listed ahead (`upcoming`); no reminders |
| | Shared household use | 00 | ◐ account owners and per-person balance sheets; no separate logins |
| | "Why did this month change?" | 01 / 10 | ◐ the numbers are there (`finance change`); an AI-written explanation in 10 |
| | Weekly summary | 10 | ⏸ |
| | Forecasts | 06 | ☐ |
| Firefly III | Double-entry ledger | 00 | ✅ |
| | Liabilities with interest (loans, debts, mortgages) | 02 | ✅ `finance loan`: terms, payment plans, payments split into principal, interest and taxes |
| | Rule engine | 00 / 01 | ◐ description or regex → account; amount and payee conditions later |
| | Expected bills | 01 | ◐ found from the ledger, not entered by hand |
| | Savings goals ("piggy banks") | 09 | ☐ |
| | REST API | with a web interface | ☐ |
| | Separate import tool | 00 / 01 | ◐ CSV with column mapping and the Ziraat Bankası template; more banks as samples arrive |
| Actual Budget | Envelope budgeting | 09 | ☐ |
| | Scheduled transactions | 06 | ☐ recurring payments are detected and loan installments are planned; booking them ahead is open |
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

### From the 02 plan

- **Bills and scheduled transactions** (confirming a recurring payment as a
  bill, warnings when it is late or its amount changes) were proposed for 02
  but left for a later version; 02 kept to debt.
- **Card statements.** A credit card statement template still waits for a
  sample; its installment lines ("taksit 3/6") will have to be matched to the
  installment plans instead of being booked again.

### Found while building 02

- **Taxes on interest change by law.** The defaults (KKDF 15 %, BSMV 15 % on
  consumer and vehicle loans; none on others) are those of 2026. Every loan
  keeps its own rates, and `--installment` finds the rate from the installment
  on the papers when the plan does not match.
- **The plan follows the common convention** (an annuity at the rate with
  taxes, interest on the balance each month). A bank that counts days for the
  first installment, or rounds differently, gives slightly different amounts;
  the ledger then follows the bank's amounts and splits them with one month of
  interest on the balance.
- **After an early repayment** the program works out lower installments over
  the same months; a bank may shorten the term instead. Early repayment fees
  and discounts are not modeled, in `payoff` either.
- **Statement lines of loans defined later** stay where the import put them;
  to split them, delete those transactions and import the statement again.
  Installment lines dated before the loan's start in the ledger are refused,
  so that nothing is counted twice.
- **`payoff`** takes the rates and least payments of debts other than loans
  from the command line; card installments are not part of the plan.
- **Inflation** for the real burden and for cash or installments defaults to
  the last twelve months of the CPI: a look back, not a forecast.
- Carried over from 01: other banks need one sample export each; PDF
  statements are deferred (10); the housing cost ratio is not computed; EVDS
  has still not been tried with a real key; the gold price is the
  international parity.

## Next steps (proposal for 03)

1. **Investments**, as on the roadmap: TEFAS fund prices and BIST prices; time-
   and money-weighted returns (TWR, XIRR), in real terms too; volatility,
   maximum drawdown, VaR and CVaR with backtests; correlation and
   concentration.
2. **Card statement template**, once a sample is available, matched with the
   installment plans.
3. **Bills and scheduled transactions:** confirm a detected recurring payment
   as a bill, get warned when it is late or its amount changes.
4. **Templates for the other banks** the family uses, one sample each.
