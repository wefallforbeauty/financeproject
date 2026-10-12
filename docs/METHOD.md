# Method (version 00)

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
to split a change in net worth into "saving + price effect + exchange rate
effect" (version 01).

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

In household finance the liquidity ratio (liquid assets / monthly essential
expenses; 3–6 months is the usual advice) is established by DeVaney (1994) and
Greninger et al. (1996). The ratio itself will be added in version 01, once
monthly expenses are computed.

## 7. Import

Every row has an import key:

$$k = \text{SHA-256}\big(\text{account} \,|\, \text{date} \,|\, \text{amount} \,|\, \text{description} \,|\, n\big)$$

- The account and description are folded (case and Turkish letters removed).
- The amount is normalized (450.00 and 450 count as the same).
- $n$ is the occurrence number of identical rows in the same file.

Therefore:

- Two identical purchases on the same day are both recorded.
- Importing the same file again adds nothing.
- Rows that two overlapping statements share are counted once.

**Limit:** if a bank writes a row's description differently in two statements,
the row is taken as new. Balance checks and fuzzy matching are planned.

The decimal separator of a CSV file is detected from all its amounts together
(`core.money.infer_decimal_separator`): a value with both separators, a
repeated separator, or one separator not followed by exactly three digits
decides it. A file whose amounts are all like `1.234` (1,234 or 1.234?) has to
be given the separator explicitly; values that disagree are an error.

Categorization rules ignore case and Turkish letters. Rules are tried by
priority and the first match wins.

## References

- Campbell, J. Y. (2006). Household finance. *Journal of Finance*, 61(4), 1553–1604.
- DeMiguel, V., Garlappi, L., & Uppal, R. (2009). Optimal versus naive diversification: How inefficient is the 1/N portfolio strategy? *Review of Financial Studies*, 22(5), 1915–1953.
- DeVaney, S. A. (1994). The usefulness of financial ratios as predictors of household insolvency: Two perspectives. *Financial Counseling and Planning*, 5, 5–24.
- Ellerman, D. P. (1985). The mathematics of double entry bookkeeping. *Mathematics Magazine*, 58(4), 226–233.
- Fisher, I. (1930). *The Theory of Interest*. New York: Macmillan.
- Greninger, S. A., Hampton, V. L., Kitt, K. A., & Achacoso, J. A. (1996). Ratios and benchmarks for measuring the financial well-being of families and individuals. *Financial Services Review*, 5(1), 57–70.
- Michaud, R. O. (1989). The Markowitz optimization enigma: Is "optimized" optimal? *Financial Analysts Journal*, 45(1), 31–42.
- Selinger, P. *Tutorial on multiple currency accounting*. https://www.mathstat.dal.ca/~selinger/accounting/tutorial.html
- TurkStat (TÜİK). Announcements on the move of the CPI to base year 2025=100 (2025–2026).
- U.S. Department of the Treasury. 31 CFR Part 356, Appendix B (the reference CPI of inflation-indexed securities).
