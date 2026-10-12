# Method (version 01)

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
with the period, the currency and the debit/credit totals around it. Card
purchases name the merchant between `İŞYERİ:` and `MUTABAKAT:`; mobile
transfers start with the other person's name. Only the table, the period, the
currency and the totals are read. Details: [DATA_SOURCES.md](DATA_SOURCES.md).

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
then learned. On the demo household (215 categorized transactions):

| Measure | Value |
|---|---|
| Right, among all guesses | 88.6 % |
| Guesses reaching 80 % | 75.1 % of the transactions |
| Right, among those | 98.1 % |

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

## References

- Campbell, J. Y. (2006). Household finance. *Journal of Finance*, 61(4), 1553–1604.
- Dawid, A. P. (1984). Present position and potential developments: Some personal views. Statistical theory: The prequential approach. *Journal of the Royal Statistical Society, Series A*, 147(2), 278–292.
- DeMiguel, V., Garlappi, L., & Uppal, R. (2009). Optimal versus naive diversification: How inefficient is the 1/N portfolio strategy? *Review of Financial Studies*, 22(5), 1915–1953.
- DeVaney, S. A. (1994). The usefulness of financial ratios as predictors of household insolvency: Two perspectives. *Financial Counseling and Planning*, 5, 5–24.
- DeVaney, S. A., & Lytton, R. H. (1995). Household insolvency: A review of household debt repayment, delinquency, and bankruptcy. *Financial Services Review*, 4(2), 137–156.
- Ellerman, D. P. (1985). The mathematics of double entry bookkeeping. *Mathematics Magazine*, 58(4), 226–233.
- Fisher, I. (1930). *The Theory of Interest*. New York: Macmillan.
- Greninger, S. A., Hampton, V. L., Kitt, K. A., & Achacoso, J. A. (1996). Ratios and benchmarks for measuring the financial well-being of families and individuals. *Financial Services Review*, 5(1), 57–70.
- IFRS Foundation. IAS 7 *Statement of Cash Flows* (classification into operating, investing and financing activities; §28, the effect of exchange rate changes on cash).
- Juster, F. T., Lupton, J. P., Smith, J. P., & Stafford, F. (2006). The decline in household saving and the wealth effect. *Review of Economics and Statistics*, 88(1), 20–27.
- Lytton, R. H., Garman, E. T., & Porter, N. M. (1991). How to use financial ratios when advising clients. *Financial Counseling and Planning*, 2, 3–23.
- Manning, C. D., Raghavan, P., & Schütze, H. (2008). *Introduction to Information Retrieval*, chapter 13: Text classification and naive Bayes. Cambridge University Press.
- McCallum, A., & Nigam, K. (1998). A comparison of event models for naive Bayes text classification. *AAAI-98 Workshop on Learning for Text Categorization*.
- Michaud, R. O. (1989). The Markowitz optimization enigma: Is "optimized" optimal? *Financial Analysts Journal*, 45(1), 31–42.
- Schmidt, M. (2008). The Sankey diagram in energy and material flow management, Part I: History. *Journal of Industrial Ecology*, 12(1), 82–94.
- Selinger, P. *Tutorial on multiple currency accounting*. https://www.mathstat.dal.ca/~selinger/accounting/tutorial.html
- TurkStat (TÜİK). Announcements on the move of the CPI to base year 2025=100 (2025–2026).
- United Nations, European Commission, IMF, OECD, & World Bank (2009). *System of National Accounts 2008*, chapter 12: Other changes in assets (the revaluation account; nominal, neutral and real holding gains).
- U.S. Department of the Treasury. 31 CFR Part 356, Appendix B (the reference CPI of inflation-indexed securities).
- Zadrozny, B., & Elkan, C. (2001). Obtaining calibrated probability estimates from decision trees and naive Bayesian classifiers. *Proceedings of the 18th International Conference on Machine Learning*, 609–616.
