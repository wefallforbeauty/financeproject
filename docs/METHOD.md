# Method (version 03)

This document gives the definition, formula and source of everything the
program computes. A section is added whenever a new model is.

## 1. Double-entry ledger

A transaction $T$ is made of postings (legs). Each posting carries an account
$a$, a commodity $c$ and a signed amount $x$. The basic rule:

$$\forall c:\quad \sum_{p \in T,\; c_p = c} x_p = 0$$

Sign convention (the same as Beancount and ledger):

| Account type | Increase | Example |
|---|---|---|
| Asset | + | bank balance 25,000 → +25,000 |
| Expense | + | 450 TRY of groceries → +450 |
| Liability | − | 3,500 TRY of card debt → −3,500 |
| Income | − | 65,000 TRY salary → −65,000 |
| Equity | − | the counterpart of opening balances |

The balance of account $a$ in commodity $c$ at the end of day $t$ is
$B_{a,c}(t) = \sum x_p$ over the postings dated on or before $t$. Since every
transaction balances in every commodity, all accounts together sum to zero in
every commodity. Valued with any price vector $P(t)$, the total is zero too:

$$\underbrace{\sum_{a \in A \cup L}\sum_c B_{a,c}(t)\,P_c(t)}_{\text{net worth}} \;=\; -\sum_{a \in Q \cup I \cup X}\sum_c B_{a,c}(t)\,P_c(t)$$

Here A, L, Q, I and X are the asset, liability, equity, income and expense
accounts. The tests check this identity on randomly generated ledgers
(Hypothesis). `finance status` shows that every commodity sums to zero.

Amounts are kept as `decimal.Decimal` and written to SQLite as text: binary
floating point (0.1 + 0.2 ≠ 0.3) is never used in the ledger. Each commodity
has a limit on decimal places (TRY 2, grams of gold 4).

## 2. Several commodities: trading accounts

Buying currency or gold turns one commodity into another. Following Selinger,
each commodity has a trading account (`Equity:Trading:<CODE>`), so that the
transaction balances in each commodity on its own. Buying 1,000 USD for
49,126.70 TRY:

| Account | TRY | USD |
|---|---|---|
| Assets:Bank:Checking | −49,126.70 | |
| Equity:Trading:TRY | +49,126.70 | |
| Equity:Trading:USD | | −1,000 |
| Assets:FX:USD | | +1,000 |

If the dollar later costs 50 TRY, the trading accounts are worth
49,126.70 − 50,000 = −873.30 TRY. A negative equity balance is a credit: this
is an unrealized currency gain of 873.30 TRY. These accounts make it possible
to split a change in net worth into saving, exchange results and revaluation
(§9).

## 3. Valuation

- **Price:** $P_c(t)$ is the latest price known on day $t$. Weekends and
  holidays use the rate of the last business day.
- **Which rate?** The indicative **forex buying** rate of TCMB (ForexBuying).
  It is close to what you would get by selling the currency to a bank, a
  prudent value. Since the same rate is used for reporting in USD, a
  1,000 USD account shows exactly 1,000 USD in the USD view.
- **Cross rates:** a commodity without a direct TRY price is converted through
  one intermediate currency. Grams of gold:

  $$P_{\text{XAU\_G}}(t) = \frac{\text{XAU/USD}(t)}{31.1034768} \times \text{USD/TRY}(t)$$

  31.1034768 g is one troy ounce. Since the TRY price of gold and the USD unit
  use the same USD/TRY rate, the two views agree:
  $NW_g = NW_{USD} \cdot 31.1034768 / (\text{XAU/USD})$.
- **Stale and missing prices:** the age of a price is the date of its oldest
  leg. If it is older than 7 days, the value is marked "stale". An asset
  without any price is **not guessed**: it is left out of net worth and a
  warning is shown.

## 4. Net worth in four units

$$NW_{TRY}(t) = \sum_{a \in A \cup L}\sum_c B_{a,c}(t)\,P_c(t)$$

$$NW_{real}(t) = NW_{TRY}(t)\cdot\frac{CPI(b)}{CPI(t)} \qquad NW_{USD}(t) = \frac{NW_{TRY}(t)}{P_{USD}(t)} \qquad NW_{g}(t) = \frac{NW_{TRY}(t)}{P_{\text{XAU\_G}}(t)}$$

$b$ is the base month: by default the latest published CPI month. Real TRY
reads as "in September 2026 prices". TRY is the headline unit; the other three
measure the same wealth with different yardsticks. Under high inflation, and
with currency and gold moves, they drift apart noticeably.

## 5. CPI

### 5.1 The series

From the January 2026 data on, TurkStat (TÜİK) publishes the CPI (TÜFE) with
base year 2025=100 and the COICOP-2018 classification (13 main groups). Past
data were moved to the new base; monthly and annual rates of change did not
change. The current series on EVDS is `TP.TUKFIY2025.GENEL` (from 2005-01).
The old `TP.FG.J0` (2003=100) ended in January 2026 and is archived.

### 5.2 Without a key: chaining

The TCMB page gives the annual and monthly % change of each month. The index
level is built by chaining the monthly changes, and scaled so that the 2025
average is 100:

$$I_m = I_{m-1}\left(1 + \frac{r_m}{100}\right), \qquad \frac{1}{12}\sum_{m \in 2025} I_m = 100$$

**Accuracy.** The published monthly changes are rounded to 0.01 points, so the
error of each month is at most ±0.005 points. Its effect was measured directly:
the annual changes computed from the chained index match all 249 annual
changes published by TCMB **within 0.037 points, 0.008 on average**
(`tests/test_inflation.py`). January 2026 comes out exactly: +4.84 % monthly,
+30.65 % annual. With an EVDS key, TurkStat's official levels are used.

### 5.3 Splicing different bases

Two bases of the same chained index differ by a constant factor. When an older
series is spliced onto a newer one, the factor is the mean ratio over the
common months (at least 6); the newer series wins where both exist
(`core.inflation.splice`).

### 5.4 The daily price level

The monthly CPI measures the average price level of the month, so it is placed
in the middle of the month: $t_m$ = first day of the month $+ (n_m - 1)/2$
days. Days in between are interpolated log-linearly:

$$\ln P(t) = (1-w)\ln I_m + w \ln I_{m+1}, \qquad w = \frac{t - t_m}{t_{m+1} - t_m}$$

- **Before the series starts:** the first month's value is used.
- **After the middle of the last month:** the last value is held. Later
  inflation is not known yet, and the report says so.

The reference index of inflation-indexed bonds (for example US TIPS, 31 CFR 356
Appendix B) also spreads the monthly CPI over the days linearly. There a lag of
two to three months is used because payments are computed from it. Here the
aim is to measure purchasing power, so no lag is used.

The difference is small but matters under high inflation. Converting a value
of 31 August 2026 into September prices:

- interpolation gives a factor of 1.0093;
- "every day uses its month's index" gives 1.0184.

The second method can be chosen with `--cpi-method monthly`.

### 5.5 The Fisher equation

The real interest rate or real return is $(1+i)/(1+\pi) - 1$ (Fisher 1930). In
this version it exists only as a function; the debt (02) and investment (03)
modules will use it.

## 6. Liquidity

| Tier | Meaning | Default account kinds |
|---|---|---|
| 0 Immediate | usable the same day | cash, checking, foreign currency |
| 1 Within a week | ≤ 1 week, at market price | time deposit, gold, fund, stock |
| 2 Restricted | can be withdrawn at a cost | private pension (BES), money owed to you |
| 3 Illiquid | takes months | real estate, vehicle, other |

Each account's tier can be changed (`--tier`). Debts have two terms:

- **Short-term:** credit card, overdraft (KMH), money owed to people.
- **Long-term:** loans.

**Liquid net assets** = assets in tiers 0 and 1 − short-term debt.

In household finance the liquidity ratio (liquid assets / monthly expenses) is
established by DeVaney (1994) and Greninger et al. (1996); it is computed with
the other household ratios (§12).

## 7. Import

Every row has an import key:

$$k = \text{SHA-256}\big(\text{account} \,|\, \text{date} \,|\, \text{amount} \,|\, \text{reference or description} \,|\, n\big)$$

- The account and description are folded (case and Turkish letters removed).
- The amount is normalized (450.00 and 450 count as the same).
- The bank's own number for the line (a receipt number) is used instead of the
  description when the statement has one, so the same statement in two formats
  gives the same keys even if their descriptions differ in spacing.
- $n$ is the occurrence number of identical rows in the same file.

Therefore:

- Two identical purchases on the same day are both recorded.
- Importing the same file again adds nothing.
- Rows that two overlapping statements share are counted once.

**Limit:** without a reference number, a row whose description a bank writes
differently in two statements is taken as new. Bank statements with balances
are checked as in §8.

The decimal separator of a CSV file is detected from all its amounts together
(`core.money.infer_decimal_separator`): a value with both separators, a
repeated separator, or one separator not followed by exactly three digits
decides it. A file whose amounts are all like `1.234` (1,234 or 1.234?) has to
be given the separator explicitly; values that disagree are an error.

Categorization rules ignore case and Turkish letters. Rules are tried by
priority and the first match wins.

## 8. Bank statements

A bank template (`ingest/banks/`) reads a bank's export into a list of lines:
date, description, signed amount, the balance after the line, the bank's
reference number and, when it can be read from the description, the payee.
Before anything is imported:

1. **Order.** Banks list the newest line first or last. Of the two orders whose
   dates do not decrease, the one in which every balance equals the previous
   balance plus the line's amount is taken:
   $B_k = B_{k-1} + x_k$. Lines of the same day are therefore ordered by the
   balances, not guessed.
2. **Running balance.** If neither order satisfies the equation, a line is
   missing or was read wrongly, and nothing is imported.
3. **Totals.** The printed sums of the money-out and money-in lines must equal
   the sums of the lines read.

The balance before the first line, $B_1 - x_1$, can start a new account
(`--opening`). After the import the ledger's balance of the account at the end
of the statement's period is compared with the bank's closing balance; on a
dry run the comparison uses the balance the import would produce. This is the
bank reconciliation of bookkeeping, done for every statement.

**Ziraat Bankası** (`banks/ziraat.py`). The table has the columns Tarih,
Fiş No, Açıklama, İşlem Tutarı (Tutar in HTML) and Bakiye, newest line first,
with the period, the currency and the debit/credit totals around it. The
description tells the kind of most lines: card purchases (the merchant between
`İŞYERİ:` and `MUTABAKAT:` becomes the payee), transfers (a mobile transfer
starts with the other person's name), the bank's fees, cash machine
withdrawals, bill payments (the company before `Abone no`) and loan
installments (the loan's number and the installment's number, §17). Only the
table, the period, the currency and the totals are read. Details:
[DATA_SOURCES.md](DATA_SOURCES.md). Credit card lists, which have no balances,
are read from PDF (§22).

## 9. Why net worth changed

Let a group of accounts hold $q_c(t)$ units of commodity $c$, and let
$V_c(t)$ be the value of one unit in the unit of account (TRY, real TRY, USD or
grams of gold). The group is worth $W(t) = \sum_c q_c(t) V_c(t)$. Between
$t_0$ and $t_1$ transactions change the holdings on the dates
$\tau_1 \le \dots \le \tau_n$. Summation by parts gives an exact identity:

$$W(t_1) - W(t_0) = \underbrace{\sum_{k}\sum_c \Delta q_c(\tau_k)\,V_c(\tau_k)}_{\text{flows, at the prices of their day}} + \underbrace{\sum_c \sum_{j} q_c(s_j)\,\big[V_c(s_{j+1}) - V_c(s_j)\big]}_{\text{revaluation}}$$

with $s_0 = t_0$, the $s_j$ the transaction dates and the last $s$ equal to
$t_1$. Because every transaction balances in every commodity, the flow into
the group equals minus the value of its legs outside the group. For the whole
balance sheet those counterparts are:

| Counterpart | Line |
|---|---|
| Income accounts | income |
| Expense accounts | expenses |
| `Equity:Trading:*` | exchange results: what buying or selling currency, gold or funds gained or lost against the market price of that day (the spread) |
| `Equity:Opening Balances` | opening balances: accounts that entered the ledger, not a real change |

Income minus expenses is **net saving**, the "active saving" of the household
literature (Juster et al. 2006). The revaluation term holds the holding gains
and losses of each commodity. The split is the household version of national
accounting, where a sector's change in net worth is split into saving and
capital transfers, other changes in volume, and nominal holding gains (SNA
2008, ch. 12).

**Units.** $V_c(t)$ is the TRY price $P_c(t)$ divided by the numeraire's price:
by $P_{USD}(t)$ for dollars, by $P_{XAU\_G}(t)$ for grams of gold. In real TRY,
$V_c(t) = P_c(t)\,\text{CPI}(b)/\text{CPI}(t)$, so the lira itself has a value
that falls with inflation. Its revaluation term is the purchasing power that
inflation took from the lira held; it corresponds to the SNA's split of
nominal holding gains into **neutral** ones (keeping pace with the general
price level) and **real** ones (SNA 2008, §12.76): in real terms only the real
holding gains remain.

**Exactness.** The identity holds to rounding for any prices; the tests check
it on random ledgers in TRY and in dollars, and that both ends equal the
balance sheet. A commodity without a value on a day where one is needed is
left out of the group with all its legs; what moved between it and valued
commodities is shown as moved to or from assets without a price.

## 10. Income statement and cash flow

The **income statement** values every income and expense posting on its own
day (TRY, or real TRY in the prices of the base month) and sums them by
category and month. Net saving is income minus expenses, the savings rate net
saving over income.

The **cash flow statement** applies §9 to the cash accounts only: asset
accounts of liquidity tier 0 (cash, checking, foreign currency accounts). Their
counterparts are grouped as in a company's statement of cash flows (IAS 7):

| Counterpart | Line |
|---|---|
| Income and expense accounts | operating: income received, expenses paid |
| Other asset accounts (time deposits, gold, funds, pension) | investing |
| Credit cards and overdraft | card payments |
| Loans | loans |
| Trading accounts | exchange results |

A credit card purchase does not move cash on the day it is made; the cash
leaves when the card is paid. That is why the operating cash flow differs from
the income statement's net saving, and why card payments have their own line.
Foreign currency held as cash is revalued every day; that term is the "effect
of exchange rate changes on cash" of IAS 7 §28, needed to reconcile the cash at
the start and at the end of each month.

## 11. Installments

A card purchase in $n$ installments is booked in full on the day it is made:
the expense and the debt exist from that day on (accrual). The plan splits the
amount into $n$ equal parts rounded down to the kuruş, the remainder going to
the first part:

$$a_1 = A - (n-1)\left\lfloor \frac{A}{n} \right\rfloor_{0.01}, \qquad a_2 = \dots = a_n = \left\lfloor \frac{A}{n} \right\rfloor_{0.01}$$

so 1,000 in 3 is 333.34 + 333.33 + 333.33. Part $k$ falls due in month
$m_1 + k - 1$, where $m_1$ is the month after the purchase unless given. The
card's balance then splits into the installments of later months (debt not
billed yet) and the rest, and the schedule gives the installment load of the
coming months. Installments that fall due count as debt payments in the
household ratios (§12): they are purchases financed by the card.

## 12. Household financial ratios

Greninger, Hampton, Kitt and Achacoso (1996) asked 156 financial planners and
educators in three Delphi rounds which ratios and values describe a
financially healthy family. Their "financial well-being profile" (Table 2) is
used as published:

| Ratio | Definition | Benchmark |
|---|---|---|
| Emergency fund | liquid assets / monthly expenses | ≥ 2.5 months |
| Savings rate | (income − expenses) / income | ≥ 10 % |
| Non-mortgage debt payments | (loan payments + card installments due) / after-tax income | ≤ 15 % reasonable, ≥ 20 % danger |
| All debt payments | the same with mortgage payments | ≤ 35 % reasonable, ≥ 45 % danger |
| Liquid assets / net worth | | ≥ 15 % |
| Investment assets / net worth | | ≥ 50 % |
| Inflation protection | % growth of net worth / % CPI inflation over a year | ≥ 2 |

Definitions, after the paper: liquid assets are cash and cash equivalents
(cash, checking and time deposits, foreign currency accounts; gold, funds and
stocks are shown in a wider variant); investment assets are gold, funds,
stocks and private pension savings; monthly expenses include debt repayment,
so the loan principal repaid is added to the expenses; debt payments are loan
principal plus the interest and fees paid with it. Debt over assets is shown
as well: a household with negative net worth is insolvent (DeVaney & Lytton
1995).

**Limits.** The benchmarks come from US planners in 1996, so they are signals
for a closer look, not rules. Salaries are recorded after tax, which matches
the paper's debt ratios (after-tax income) but not its savings ratio (gross
income; the paper found the same 10 % median for both). Inflation protection
compares the twelve months up to the latest published CPI month and needs net
worth a year earlier.

## 13. Recurring payments

The transactions of one payee (or description, digits removed), category and
direction form a candidate series. With the gaps $g_i$ between consecutive
dates:

1. The median gap selects a period:

   | Period | Median gap (days) | Tolerance (days) | Least occurrences |
   |---|---|---|---|
   | weekly | 5–9 | ±2 | 3 |
   | every 2 weeks | 12–16 | ±3 | 3 |
   | monthly | 26–35 | ±6 | 3 |
   | every 2 months | 55–66 | ±8 | 3 |
   | quarterly | 84–98 | ±10 | 3 |
   | every 6 months | 170–196 | ±15 | 2 |
   | yearly | 350–380 | ±20 | 2 |

2. At least 75 % of the gaps must lie within the tolerance of the period's
   nominal length.
3. A series of two occurrences also needs amounts within 10 % of each other,
   so that two visits to a shop half a year apart do not count.

The median is robust to a single late, early or doubled payment. The amount is
"fixed" if all amounts lie within 2 % of their median, otherwise "variable";
the next amount expected is the median of the last three. The next date is one
period after the last payment; for calendar periods on the median day of the
month of the last three payments, clamped to the month's length. A series is
late once that date has passed by more than the tolerance, and stopped once a
whole period has passed. `finance upcoming` lists the expected payments of the
next days and the card installments falling due.

## 14. Learned categories

The categorizer is a multinomial naive Bayes text classifier with Laplace
smoothing (McCallum & Nigam 1998; Manning et al. 2008, ch. 13). A transaction
$d$ with the set of features $F(d)$ gets the category

$$\hat c = \arg\max_c \Big[\log P(c) + \sum_{f \in F(d) \cap V} \log \frac{N_{cf} + 1}{N_c + |V|}\Big]$$

where $N_{cf}$ counts the training transactions of category $c$ with feature
$f$, $N_c = \sum_f N_{cf}$ and $V$ is the vocabulary. Each feature counts once
per transaction ("boolean" multinomial naive Bayes). The features are:

- the words of the description and the payee, folded, of at least two
  characters with at least one letter (receipt and card numbers are dropped);
- the whole payee as one feature;
- the balance-sheet account the money moved through;
- the size of the amount in half-decade bins.

Money out is only compared with categories that money out went to before, and
money in likewise. Without a known word the model makes no guess. The guess is
applied when its posterior probability reaches 80 %.

**Evaluation.** Naive Bayes probabilities are known to be poorly calibrated
(Zadrozny & Elkan 2001), so the threshold is checked empirically, with
prequential ("test then train") evaluation (Dawid 1984): the ledger is walked
in date order, each transaction is guessed from the earlier ones only, and
then learned. On the demo household (216 categorized transactions, shops
with generic names):

| Measure | Value |
|---|---|
| Right, among all guesses | 90.7 % |
| Guesses reaching 80 % | 79.6 % of the transactions |
| Right, among those | 98.8 % |

Beancount's smart_importer uses a linear support vector machine on similar
features. Naive Bayes was chosen because it needs no extra dependency, learns
one transaction at a time and works with few examples.

## 15. Sankey diagram

Income accounts on the left flow into the period's budget in the middle, which
flows to the expense categories and to savings on the right (or, when expenses
exceed income, the gap comes from savings on the left). Band widths are
proportional to the amounts (Schmidt 2008). Up to nine expense categories are
shown; when there are more, the eight largest stay and the rest are grouped as
Other (five and four for income). Colours follow the role of
a flow (income, expense, savings), not the category, so the diagram needs no
legend; every node is labeled with its amount and share of income.

## 16. Loans: payment plans

The contract states a monthly interest rate $r$. On consumer and vehicle loans
the state also takes KKDF (Kaynak Kullanımı Destekleme Fonu, the Resource
Utilization Support Fund) and BSMV (Banka ve Sigorta Muameleleri Vergisi, the
Banking and Insurance Transactions Tax) as fractions $k$ and $b$ of every
interest payment; loans for buying a home are exempt from both. The
installment of an annuity loan uses the gross rate $g = r(1 + k + b)$:

$$A = \left[ P \, \frac{g}{1 - (1+g)^{-n}} \right]_{0.01}$$

Each month splits the installment, every part rounded to the kuruş:

$$I_j = [B_{j-1}\, r], \qquad K_j = [I_j\, k], \qquad S_j = [I_j\, b], \qquad P_j = A - I_j - K_j - S_j, \qquad B_j = B_{j-1} - P_j$$

and the last installment repays the principal that is left, $P_n = B_{n-1}$.
With equal principal, $P_j = [P/n]$ and the installment falls month by month.
This reproduces the payment plans banks print: the worked example of İş
Bankası's note on the annual cost rate, 10,000 TRY at 1 % a month for 12
months, gives 888.49 a month (the last 888.47) and 661.86 of interest; 100,000
TRY at 3.49 % with 15 % KKDF and 15 % BSMV gives 6,924.20, of which the first
month is 2,387.20 principal, 3,490.00 interest and 523.50 of each tax (the
annuity formula itself: Kellison 2009, ch. 3 and 5).

Rates are parameters of each loan, because they change by law: banks'
pre-contract forms from before 2023 show BSMV on consumer loans at 5 %, newer
ones at 15 %. The defaults are 15 % and 15 % for consumer and vehicle loans
and 0 for other loans; the bank's pre-contract information form (sözleşme
öncesi bilgi formu) has the loan's own figures. When the papers show the
installment but not the rate, $g$ is the root of $P = A\,(1 - (1+g)^{-n})/g$
(bisection) and $r = g/(1 + k + b)$.

## 17. Loan payments in the ledger

A loan is a liability account plus its terms (`ledger/loans.py`). A payment is
one transaction: the paying account $-x$, the loan's account $+P$ (principal),
and the interest and the taxes as expenses (one account, or two).

- **As planned.** When the planned installment is paid while the ledger owes
  exactly what the plan says, the plan's own split is used.
- **Otherwise** interest and taxes are paid first and the rest repays
  principal: one month's worth on what was owed when the installment's month
  began. When a bank collects an installment in parts, the parts share that
  month's interest: each pays what the earlier ones left of it.
- **An early repayment** outside the installments lowers the principal only.
- **More than is owed** (late interest, fees) is booked as interest; a
  payment typed by hand is refused instead.
- **After a payment off the plan** the installments left are worked out again
  from the ledger's balance, with the same rate and number of installments
  (the bank may shorten the term instead).
- **A loan from before the ledger** (`--opening-on`): the installments due by
  that day count as paid and the account opens at the plan's balance after
  them, or at the balance given.

On a Ziraat Bankası statement an installment line reads
`<loan number> hesap <n>. taksit ödeme tutarı`. Once a loan has its number
(`--reference`; spaces, dashes and case do not matter), such a line is booked
as a payment of installment $n$ split as above. The import key stays the
line's own, so importing the statement again adds nothing.

## 18. The annual cost rate

The annual cost rate (yıllık maliyet oranı) is the annual rate $X$ at which
what the borrower receives and what they pay back are worth the same:

$$\sum_k C_k\,(1+X)^{-t_k} = \sum_l D_l\,(1+X)^{-s_l}$$

$C_k$ are the amounts paid out to the borrower, $D_l$ the installments, fees
and other charges, and $t_k$, $s_l$ the times in years from the first flow.
This is the APR of the EU consumer credit directive (2008/48/EC, Annex I),
which the Turkish consumer credit regulation (Tüketici Kredisi Sözleşmeleri
Yönetmeliği, 2015) takes over. Time is counted in whole months back from each
flow's date, a month being 1/12 year, and the days left over are divided by
the length of the year that ends on the last of them (365, or 366 when that
year holds 29 February). The root is found by bisection.

İş Bankası's note works through 10,000 TRY for 12 months at 1 % a month with a
50 TRY file fee and finds 13.7504 % (13.75 %). The program finds 13.7506 %;
the gap is less than one kuruş of present value, within the rounding of the
note's own working, and the tests check the result. Without fees the annual
cost rate is the gross monthly rate compounded, $(1+g)^{12} - 1$: 70.31 % for
the 3.49 % loan above, against 54.4 % for $12g$ and 41.9 % for $12r$.
`finance apr` computes it for any offer; `finance loan list` for every loan.

## 19. Cash or installments

Paying $C$ now or $n$ installments of $a$, the first in $m$ months: in today's
money the installments are worth

$$PV = \sum_{j=0}^{n-1} a\,(1+d)^{-(m+j)}$$

at the monthly rate $d$ that the money earns while it stays in the account
(a deposit rate after tax), or at the monthly rate of inflation, which compares
the two in real terms. Without a rate the program takes the CPI's change over
the last twelve months. The installments are cheaper when $PV < C$.
Equivalently, the plan's own rate is the internal rate of return $i$ of the
flows $+C, 0, \ldots, -a, \ldots, -a$ (what the installments charge over the
cash price), and the installments are cheaper when $i < d$. Installments "at
the cash price" charge nothing; at 30 % inflation a year, six of them starting
next month are worth about 7 % less than paying cash.

## 20. Paying off several debts

Given a monthly budget $M$ at least the sum of the least payments, every month
each debt grows by its rate (interest and taxes), each gets its least payment,
and the rest of the budget goes to one debt at a time:

- **avalanche:** the highest rate first. With a fixed budget, fixed least
  payments and no fees, this order pays the least interest;
- **snowball:** the smallest balance first. It pays more interest but clears a
  first debt sooner.

A debt paid off frees its least payment for the next one. The "as now" plan
pays only the least payments. A loan's least payment is its next installment
and its rate the gross monthly rate $g$; other debts take the rate and least
payment given (default 0). Early repayment fees are not counted.

Studies of how people repay debts find that many prefer to close small debts
first even when it costs more ("debt account aversion", Amar et al. 2011), and
that closing debts one by one helps people keep going (Gal & McShane 2012;
Brown & Lahey 2015). The program shows both orders and the cost of the
difference rather than choosing.

## 21. The real burden of debt

A debt in lira loses real value as prices rise. The installments left,
discounted at the expected inflation $\pi$ a year, are worth

$$PV = \sum_j A_j\,(1+\pi)^{-t_j}$$

in today's money, with $t_j$ in years as in §18. A loan's real interest rate
is $(1+g)^{12} / (1+\pi) - 1$ (Fisher, §5.5). $\pi$ defaults to the CPI's change
over the last twelve months; `--inflation` sets another expectation. These are
statements about purchasing power under that assumption, not forecasts: at
30 % inflation the 3.49 % loan above still costs 31 % a year in real terms.
`finance debt` lists every debt this way; a card's balance also shows how much
of it is installments billed in later months (§11).

## 22. Credit card lists

**Reading a PDF.** A PDF page is text placed at coordinates. `ingest/pdf.py`
reads every piece of text with the origin of its text matrix in the drawing's
coordinates ($x' = a x + c y + e$, $y' = b x + d y + f$ for the current
transformation $[a\,b\,c\,d\,e\,f]$). Ziraat Bankası's internet banking prints
a web page: one tall drawing that every PDF page shows a clipped slice of, so
every page carries the same pieces. A page whose pieces equal an earlier
page's is read once.

**The card list** ("Kart Hareketleri", `banks/ziraat_card.py`). Each
transaction is a block anchored on its amount: a number written `1.234,56`
with a currency word at its right. The block reaches down to the next amount.
Left of the amount column are the day and short month (`17 Eki`), the year,
the description (one or more lines, pieces on the same height joined left to
right) and, for an installment, a note `(<total> TL İşlemin <i>/<n> Taksidi)`.
Text right of the amount (the page's buttons) and outside any block (the
holder's name above, the bank's address below) is not read. Spending has no
sign and becomes a negative change of the card's account; payments and
refunds carry `+`.

**Installments.** An installment line shows the month's part $a_i$ of a
purchase of total $T$ in $n$ parts, and the purchase day before the
description (`15/07`). The purchase month is the line's month minus $i-1$; its
year follows from that. The ledger books a purchase in installments in full on
its day (§11), so:

- if the card has a plan with the same $T$ and $n$ bought within 7 days of the
  purchase day, the line is already counted and is skipped;
- otherwise, for $i = 1$ the purchase is booked in full with a plan whose first
  installment falls in the line's month;
- otherwise ($i > 1$, a purchase made before the ledger) the line is booked as
  an expense of $a_i$, with a note; the following months are booked the same
  way when their lists are imported.

**Payments and transfers.** With `--payments-from` the card's payments become
transfers from that account. A row whose counter account is another asset or
liability account is a transfer; if the ledger already has a transaction of
exactly two legs moving the same amount between the same two accounts within
3 days (say, from the bank account's statement, where a rule sent the card
payment to the card), the row is skipped. Each such transaction is matched at
most once per import.

## 23. Fund and share prices

**Funds** come from TEFAS (`prices/tefas.py`): one request per fund gives its
daily prices for 1, 3, 6, 12, 36 or 60 months back; the shortest look-back
reaching a week before the first day needed is asked for. Prices are TRY per
share with up to six decimals, read as decimals.

**Shares** come from Yahoo Finance's chart API (`prices/yahoo.py`). Its closes
are adjusted for later splits: after a bonus issue of 100 % every earlier close
is halved. The ledger holds each day's number of shares, so each close is
turned back into the price of its day:

$$P_t = \tilde P_t \prod_{s:\ t_s > t} \frac{\text{numerator}_s}{\text{denominator}_s}$$

over the splits $s$ after day $t$ (rounded to the kuruş for BIST). The request
always runs to today, because the adjustment covers every split up to now.

**Splits in the ledger.** `finance split` multiplies the shares held at the end
of the day before by the ratio $k$ (new shares per old share), dropping
fractions: $q' = \lfloor k q \rfloor$. The $q' - q$ new shares come from the
trading account of the share (§2), so no value crosses the portfolio's
boundary and the value stays the same while the price falls by $k$. A price
update reports a split of a share the ledger held on that day until such a
transaction is recorded.

## 24. Returns

**The portfolio** is a set of asset accounts: by default every fund, stock,
gold and foreign currency account. Its value at the end of day $t$ is
$V_t = \sum_{a,c} B_{a,c}(t) P_c(t)$ in TRY, every calendar day.

**Cash flows** are what crosses its boundary. A transaction with a leg in the
portfolio brings in or takes out
$F = -\sum_{p \notin \text{portfolio}} x_p P_{c_p}(t)$ over its other legs,
except the trading accounts (the units exchanged, §2) and the income and
expenses of the investments themselves (`Income:Interest`,
`Income:Dividends`, `Income:Investments`, `Expenses:Investments`), which are
return. Buying a fund with money from the bank is an inflow, a dividend paid
into a portfolio account is return, spending from a foreign currency account
is an outflow. A day's inflows $CF^{in}_t$ and outflows $CF^{out}_t$ are summed
separately.

**Time-weighted return.** As in Portfolio Performance's "true time-weighted
rate of return", inflows count at the start of the day and outflows at its
end:

$$1 + r_t = \frac{V_t + CF^{out}_t}{V_{t-1} + CF^{in}_t},\qquad \mathrm{TWR} = \prod_t (1 + r_t) - 1,\qquad \text{a year: } (1+\mathrm{TWR})^{365/D} - 1$$

over the $D$ days of the period. It measures the investments whatever was
added or taken out. With daily values the money bought during a day earns
that whole day's change: a purchase at the day's price counts as if made at
the previous close.

**Money-weighted return** (XIRR): the yearly rate $X$ with

$$-V_0 - \sum_t CF^{in}_t (1+X)^{-\tau_t} + \sum_t CF^{out}_t (1+X)^{-\tau_t} + V_D (1+X)^{-\tau_D} = 0,\qquad \tau_t = \frac{t - t_0}{365}$$

solved by bisection. It measures the investor's money and depends on when it
went in. On the example of the spreadsheet function XIRR the program gives
37.336 %.

**Units.** Every $V_t$ and flow is converted at its own day's rate: real TRY
by the CPI factor of §5.4, US dollars by USD/TRY, grams of gold by the TRY
price of a gram. The same formulas then give the return in that unit: the TWR
in real TRY is the return after inflation, in gold it is the return against
holding gold.

## 25. Risk

The portfolio's returns are taken on **market days**, the days with a price of
something it holds (or of the dollar a holding is priced through); the
calendar days in between fold into the next market day. All in TRY.

- **Volatility:** $\sigma = s(\ln(1+r)) \sqrt{252}$, the sample standard
  deviation of daily log returns for about 252 trading days a year.
- **Maximum drawdown:** with the growth index $I_t = \prod_{u \le t}(1+r_u)$,
  $\max_t \left(1 - I_t / \max_{u \le t} I_u\right)$ with the day of the peak,
  the bottom, and the first day back at the peak, if any (Magdon-Ismail &
  Atiya 2004).
- **Value at risk** at level $\alpha$ (95 %, 99 %), one day. Historical: with
  the returns sorted $r_{(1)} \le \dots \le r_{(n)}$ and
  $k = \lceil n(1-\alpha) \rceil$, $\mathrm{VaR} = -r_{(k)}$ and the expected
  shortfall $\mathrm{ES} = -\tfrac{1}{k}\sum_{j \le k} r_{(j)}$, the mean loss
  of the worst days (Acerbi & Tasche 2002). Normal: with the mean $\mu$ and
  standard deviation $\sigma$ of the returns and $z = \Phi^{-1}(\alpha)$,
  $\mathrm{VaR} = z\sigma - \mu$ and $\mathrm{ES} = \sigma\,\varphi(z)/(1-\alpha) - \mu$.
  In TRY: times the portfolio's last value.
- **Backtest:** each day's historical VaR from the 250 market days before it is
  compared with that day's return; a loss beyond it is an exception $h_t = 1$.
  Kupiec's (1995) test of the share of exceptions, $x$ of $n$ against
  $p = 1 - \alpha$:

  $$LR_{POF} = -2\ln\frac{(1-p)^{n-x} p^x}{(1-x/n)^{n-x} (x/n)^x} \sim \chi^2_1$$

  It reproduces the acceptance regions tabulated by Jorion (2007) for 255
  days: at 99 % fewer than 7 exceptions; at 95 % between 7 and 20.
  Christoffersen's (1998) test of independence uses the counts $n_{ij}$ of
  day pairs going from state $i$ to $j$, $\pi_{01} = n_{01}/(n_{00}+n_{01})$,
  $\pi_{11} = n_{11}/(n_{10}+n_{11})$ and $\pi = (n_{01}+n_{11})/\sum n_{ij}$:

  $$LR_{ind} = -2\ln\frac{(1-\pi)^{n_{00}+n_{10}}\pi^{n_{01}+n_{11}}}{(1-\pi_{01})^{n_{00}}\pi_{01}^{n_{01}}(1-\pi_{11})^{n_{10}}\pi_{11}^{n_{11}}} \sim \chi^2_1,\qquad LR_{cc} = LR_{POF} + LR_{ind} \sim \chi^2_2$$

  The p-values are $\operatorname{erfc}(\sqrt{LR/2})$ for one degree of
  freedom and $e^{-LR/2}$ for two. A p-value under 5 % says the VaR had too
  many (or too few) exceptions, or that they came in clusters.
- **Holdings:** each holding's volatility, largest fall and correlation with the
  portfolio use its prices on the market days it was held, in the units after
  the recorded splits (a price before a 2-for-1 split is halved), so that a
  split is not a fall. Correlations are Pearson's, between daily log returns on
  the days both are known.

## 26. Concentration

`finance concentration` looks at the financial assets (liquidity tiers 0–2,
§6), or at the accounts given:

- the shares $s_i$ by holding (commodity), by institution and by currency (a
  fund or share counts in the currency it is priced in);
- the Herfindahl–Hirschman index $H = \sum_i s_i^2$ over the holdings and the
  effective number of holdings $1/H$ (Hirschman 1964): 0.25 is as spread as
  four equal holdings;
- a flag when one investment (not a currency), one institution or one currency
  holds more than 50 % (`--threshold`), the default of Ghostfolio's X-ray
  rules for accounts and currencies;
- every person's deposits at every bank (checking, time deposit, foreign
  currency and gold accounts with a bank set), against the deposit insurance
  of the TMSF, per person and bank, principal and interest together:
  650,000 TRY in 2024, 950,000 in 2025, 1,200,000 in 2026. The limit is
  raised every year by the revaluation rate; `--insured-limit` gives another.
  A joint account's insurance is shared among its holders, which the ledger
  does not know.

## References

- Acerbi, C., & Tasche, D. (2002). On the coherence of expected shortfall. *Journal of Banking & Finance*, 26(7), 1487–1503.
- Amar, M., Ariely, D., Ayal, S., Cryder, C. E., & Rick, S. I. (2011). Winning the battle but losing the war: The psychology of debt management. *Journal of Marketing Research*, 48(SPL), S38–S50.
- Bacon, C. R. (2008). *Practical Portfolio Performance Measurement and Attribution* (2nd ed.). Wiley.
- Brown, A. L., & Lahey, J. N. (2015). Small victories: Creating intrinsic motivation in task completion and debt repayment. *Journal of Marketing Research*, 52(6), 768–783.
- Campbell, J. Y. (2006). Household finance. *Journal of Finance*, 61(4), 1553–1604.
- Christoffersen, P. F. (1998). Evaluating interval forecasts. *International Economic Review*, 39(4), 841–862.
- Dawid, A. P. (1984). Present position and potential developments: Some personal views. Statistical theory: The prequential approach. *Journal of the Royal Statistical Society, Series A*, 147(2), 278–292.
- DeMiguel, V., Garlappi, L., & Uppal, R. (2009). Optimal versus naive diversification: How inefficient is the 1/N portfolio strategy? *Review of Financial Studies*, 22(5), 1915–1953.
- DeVaney, S. A. (1994). The usefulness of financial ratios as predictors of household insolvency: Two perspectives. *Financial Counseling and Planning*, 5, 5–24.
- DeVaney, S. A., & Lytton, R. H. (1995). Household insolvency: A review of household debt repayment, delinquency, and bankruptcy. *Financial Services Review*, 4(2), 137–156.
- Ellerman, D. P. (1985). The mathematics of double entry bookkeeping. *Mathematics Magazine*, 58(4), 226–233.
- European Parliament and Council (2008). Directive 2008/48/EC on credit agreements for consumers, Annex I: calculation of the annual percentage rate of charge.
- Fisher, I. (1930). *The Theory of Interest*. New York: Macmillan.
- Gal, D., & McShane, B. B. (2012). Can small victories help win the war? Evidence from consumer debt management. *Journal of Marketing Research*, 49(4), 487–501.
- Ghostfolio. X-ray rule settings (default thresholds). https://github.com/ghostfolio/ghostfolio/blob/main/apps/api/src/models/rules/rule-settings.ts
- Greninger, S. A., Hampton, V. L., Kitt, K. A., & Achacoso, J. A. (1996). Ratios and benchmarks for measuring the financial well-being of families and individuals. *Financial Services Review*, 5(1), 57–70.
- Gümrük ve Ticaret Bakanlığı (2015). Tüketici Kredisi Sözleşmeleri Yönetmeliği. *Resmî Gazete* 29363, 22 May 2015 (the annual cost rate).
- Hirschman, A. O. (1964). The paternity of an index. *American Economic Review*, 54(5), 761.
- IFRS Foundation. IAS 7 *Statement of Cash Flows* (classification into operating, investing and financing activities; §28, the effect of exchange rate changes on cash).
- Jorion, P. (2007). *Value at Risk: The New Benchmark for Managing Financial Risk* (3rd ed.). McGraw-Hill.
- Juster, F. T., Lupton, J. P., Smith, J. P., & Stafford, F. (2006). The decline in household saving and the wealth effect. *Review of Economics and Statistics*, 88(1), 20–27.
- Kellison, S. G. (2009). *The Theory of Interest* (3rd ed.). McGraw-Hill/Irwin.
- Kupiec, P. H. (1995). Techniques for verifying the accuracy of risk measurement models. *Journal of Derivatives*, 3(2), 73–84.
- Lytton, R. H., Garman, E. T., & Porter, N. M. (1991). How to use financial ratios when advising clients. *Financial Counseling and Planning*, 2, 3–23.
- Magdon-Ismail, M., & Atiya, A. F. (2004). Maximum drawdown. *Risk*, 17(10), 99–102.
- Manning, C. D., Raghavan, P., & Schütze, H. (2008). *Introduction to Information Retrieval*, chapter 13: Text classification and naive Bayes. Cambridge University Press.
- McCallum, A., & Nigam, K. (1998). A comparison of event models for naive Bayes text classification. *AAAI-98 Workshop on Learning for Text Categorization*.
- Michaud, R. O. (1989). The Markowitz optimization enigma: Is "optimized" optimal? *Financial Analysts Journal*, 45(1), 31–42.
- Microsoft. XIRR function (the worked example). https://support.microsoft.com/en-us/office/xirr-function-de1242ec-6477-445b-b11b-a303ad9adc9d
- Portfolio Performance. Time-weighted rate of return (TTWROR). https://help.portfolio-performance.info/en/concepts/performance/time-weighted
- Schmidt, M. (2008). The Sankey diagram in energy and material flow management, Part I: History. *Journal of Industrial Ecology*, 12(1), 82–94.
- Selinger, P. *Tutorial on multiple currency accounting*. https://www.mathstat.dal.ca/~selinger/accounting/tutorial.html
- Tasarruf Mevduatı Sigorta Fonu (TMSF). Decisions of the Fund Board on the insured amount of deposits and participation funds, for 2025 and 2026 (Resmî Gazete, December 2024 and December 2025).
- Türkiye İş Bankası. Yıllık maliyet oranının hesaplanması (a note with a worked example). https://www.isbank.com.tr/bankamizi-taniyin/Documents/hakkimizda/yillik-maliyet-orani-hesaplanmasi.pdf
- TurkStat (TÜİK). Announcements on the move of the CPI to base year 2025=100 (2025–2026).
- U.S. Department of the Treasury. 31 CFR Part 356, Appendix B (the reference CPI of inflation-indexed securities).
- United Nations, European Commission, IMF, OECD, & World Bank (2009). *System of National Accounts 2008*, chapter 12: Other changes in assets (the revaluation account; nominal, neutral and real holding gains).
- Zadrozny, B., & Elkan, C. (2001). Obtaining calibrated probability estimates from decision trees and naive Bayesian classifiers. *Proceedings of the 18th International Conference on Machine Learning*, 609–616.
