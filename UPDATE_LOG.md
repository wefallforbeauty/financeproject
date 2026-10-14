# Update log

## 02 (2026-10-14)

Pull request #4. Version 02 is the debt module: loans with their payment
plans and annual cost rate, payments split into principal, interest and taxes
(also from bank statement lines), cash or installments, plans for paying off
several debts and the real burden of debt.

### Loans

- **Loans** (`core/loans.py`, `ledger/loans.py`): a liability account plus its
  terms: principal, the contractual monthly rate, the number of installments,
  the first due date, the method (equal installments or equal principal),
  KKDF and BSMV, fees, and the bank's number for the loan. New tables `loans`
  and `loan_payments`.
- **Payment plans** as banks print them: the annuity at the rate with KKDF and
  BSMV, interest on the balance each month, every part rounded to the kuruş,
  the last installment taking what is left. They reproduce the worked example
  of İş Bankası's note on the annual cost rate (888.49 a month, the last
  888.47). KKDF and BSMV default to 15 % each on consumer and vehicle loans
  and 0 on others; each loan keeps its own rates.
- **The annual cost rate** (yıllık maliyet oranı) by the formula of the EU
  consumer credit directive and the Turkish consumer credit regulation, with
  time in whole months and days. The bank's example gives 13.7504 %; the
  program 13.7506 %, less than a kuruş of present value apart.
- **The rate from an installment:** when the papers show the installment and
  not the rate, it is worked out from the installment.
- **Payments:** the planned split when the installment is paid as planned;
  otherwise interest and taxes first, one month's worth on the balance, the
  rest principal. Installments collected in parts share one month's interest;
  early repayments lower the principal only; whatever is paid beyond what is
  owed counts as interest. After a payment off the plan, the installments left
  are worked out from the ledger's balance.
- **Loans from before the ledger:** with `--opening-on` the installments due
  by that day count as paid and the account opens at the plan's balance.
- Commands: `finance loan add`, `loan list`, `loan schedule` (`--csv`),
  `loan pay` (`--extra` for an early repayment), `loan delete`.

### Bank statements

- The Ziraat Bankası template tells the kind of each line it can: card
  purchases, transfers (mobile and FAST), the bank's fees and the tax on them,
  cash withdrawals, bill payments (the company becomes the payee) and loan
  installments (the loan's number and the installment's number).
- An installment line of a loan defined with `--reference` is booked as that
  loan's payment, split into principal, interest and taxes. Loan numbers no
  defined loan has are listed after the import with the command to add the
  loan. Installment lines dated before the loan's start in the ledger are
  refused.
- The made-up statements in the tests have one line of each kind.

### Debt decisions

- `finance apr`: the annual cost rate of any offer, from its installments or
  its rate, with fees.
- `finance cash-or-installments`: the installments in today's money at what
  the money would earn (`--rate`, `--annual-rate`) or, by default, at the
  CPI's inflation of the last twelve months; and the rate the plan charges
  over the cash price.
- `finance payoff`: the defined loans and the debts named on the command line,
  paid off highest rate first (avalanche) or smallest debt first (snowball),
  against paying only the least amounts.
- `finance debt`: every debt with its rates, its annual cost rate, its real
  rate after inflation, the installments left and what they are worth in
  today's money; for cards, the installments billed in later months.
- `finance upcoming` lists loan installments too, overdue ones first.

### Demo and documents

- The demo household's consumer loan now has terms: it started five months
  before the ledger, and every installment is split into principal, interest
  and taxes. The household also pays back money borrowed from a friend.
- METHOD.md §16–21: payment plans, payments in the ledger, the annual cost
  rate, cash or installments, paying off several debts, the real burden of
  debt; with the directive, the regulation, the bank's note and studies of how
  people repay debts (Amar et al. 2011, Gal & McShane 2012, Brown & Lahey
  2015).
- DATA_SOURCES.md lists the kinds of statement lines the template
  recognizes.
- 186 tests (158 before).

## 01 (2026-10-12)

Pull requests #2 and #3. The project got its new name and became English and
platform-neutral; then version 01 brought bank statements in and explained
where the money goes: the Ziraat Bankası template, a learned categorizer,
installments, the income statement, the cash flow statement, a Sankey
diagram, recurring payments, household ratios and the decomposition of net
worth changes.

### Name, language and platforms (#2)

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
- The calculations did not change: on the same made-up household, version 00
  and the renamed code give identical net worth tables.

### Bank statements

- **Ziraat Bankası** account statements, as .xlsx or .html
  (`ingest/banks/ziraat.py`), written after a real export:
  - the lines are put oldest first; the bank's running balance must add up
    line by line and the printed debit and credit totals must match,
    otherwise nothing is imported;
  - the merchant of card purchases and the other person of mobile transfers
    become the payee;
  - the bank's receipt number goes into the import key, so the Excel and HTML
    downloads of the same period, or overlapping periods, are imported once;
  - only the table, the period, the currency and the totals are read; the
    account holder's name, address and IBAN are not.
- `finance import FILE --account ACCOUNT` recognizes the bank. `--opening`
  starts a new account at the balance before the statement's first line.
  After the import the ledger's balance is compared with the bank's closing
  balance (predicted on a dry run).
- Excel files are read with python-calamine; openpyxl refuses Ziraat's export.
- `tests/fixtures/make_ziraat_samples.py` writes made-up statements that copy
  the layout and the quirks of the real export (no cell references, an empty
  fill in the style sheet, a bot-protection script in the HTML).
- The CSV import and the statement import share one writer
  (`ingest/importer.py`): categories, duplicate keys, all or nothing.

### Categories

- A **learned categorizer** (`ingest/learn.py`): boolean multinomial naive
  Bayes on the words of the description and the payee, the whole payee, the
  account and the size of the amount. It learns from the ledger's own
  categorized transactions and is used after the rules, when it is at least
  80 % sure.
- `finance categorize` applies the rules and then the model to uncategorized
  transactions (`--dry-run` previews). `--evaluate` walks the ledger in date
  order and guesses each transaction from the earlier ones (prequential
  evaluation). On the demo household: 88.6 % right overall, 75.1 % of the
  guesses reached 80 %, and 98.1 % of those were right.
- `finance txn recategorize ID ACCOUNT` moves a simple transaction to another
  category; the model learns from the correction.
- `finance rule apply` is unchanged; rules can now be previewed.

### Installments

- `finance expense AMOUNT CATEGORY --from CARD --installments N [--first-due
  YYYY-MM]` books the purchase in full and stores its installment plan
  (`ledger/installments.py`, table `installment_plans`). The amount is split
  into equal parts, the remainder going to the first.
- `finance installments` lists the plans, the installments due in the coming
  months, and each card's balance split into installments of later months
  and the rest.

### Reports

- **Income statement** (`finance income-statement`): income and expenses by
  category and month, net saving and savings rate; `--depth`, `--real`,
  `--csv`.
- **Cash flow statement** (`finance cash-flow`): cash at the start and end of
  each month of the tier 0 accounts, with income received, expenses paid,
  investing, card payments, loans, exchange results and the revaluation of
  foreign currency cash (IAS 7).
- **Sankey diagram** (`finance sankey`, PNG): income sources → budget →
  expense categories and savings.
- **Change in net worth** (`finance change`): income, expenses, net saving,
  exchange results, opening balances and the revaluation of each commodity,
  in TRY, real TRY (with the inflation loss on the lira), USD or grams of
  gold. The split is exact (summation by parts; `core/decompose.py`).
- **Household ratios** (`finance ratios`): emergency fund, savings rate, debt
  payments, liquid and investment assets over net worth, inflation protection
  and debt over assets, with the benchmarks of Greninger et al. (1996).
- **Recurring payments** (`finance recurring`) found from the median gap and
  regularity of each payee's transactions, and the payments expected next
  (`finance upcoming`), card installments included.

### Demo household

- Shops as payees, two streaming subscriptions (one gets more expensive), a
  phone and an internet bill, a laptop in 6 and a washing machine in 9
  installments; the card is paid for the month's purchases plus the
  installments due. In the last two months a third of the card purchases stay
  uncategorized, for `finance categorize` to fix.

### Behaviour changes

- `finance import` takes bank statements as well as CSV files; its summary
  says "N were already in" and reports the learned categories.
- New dependency: `python-calamine`.
- The ledger gets a new table, `installment_plans`; existing ledgers of
  schema version 2 get it automatically when opened.

### Tests

- 158 tests (110 before): statements and the Ziraat template, the
  categorizer, installments, the decomposition (a hand-worked example and a
  property test on random ledgers), the income and cash flow statements,
  ratios, recurring payments, the Sankey diagram and the new commands.

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
