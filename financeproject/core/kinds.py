"""Account types, account kinds and liquidity tiers.

Account paths are colon-separated; the first segment is one of the five roots
below and fixes the account type. The kind (``checking``, ``credit_card``…) of
an asset or liability account gives its default liquidity tier or debt term.
"""

from __future__ import annotations

from dataclasses import dataclass

from financeproject.core.text import fold

ASSET, LIABILITY, EQUITY, INCOME, EXPENSE = "asset", "liability", "equity", "income", "expense"

ROOTS = {
    "Assets": ASSET,
    "Liabilities": LIABILITY,
    "Equity": EQUITY,
    "Income": INCOME,
    "Expenses": EXPENSE,
}
ROOT_OF_TYPE = {t: name for name, t in ROOTS.items()}
_ROOT_BY_FOLD = {fold(name): name for name in ROOTS} | {
    "asset": "Assets",
    "liability": "Liabilities",
    "expense": "Expenses",
}

BALANCE_SHEET_TYPES = (ASSET, LIABILITY)

TIERS = {
    0: "Immediate",
    1: "Within a week",
    2: "Restricted",
    3: "Illiquid",
}
SHORT, LONG = "short", "long"
TERMS = {SHORT: "Short-term", LONG: "Long-term"}


@dataclass(frozen=True, slots=True)
class Kind:
    key: str
    label: str
    type: str
    tier: int | None = None  # assets: default liquidity tier
    term: str | None = None  # liabilities: short or long term
    default_commodity: str | None = "TRY"


KINDS: dict[str, Kind] = {
    k.key: k
    for k in (
        Kind("cash", "Cash", ASSET, tier=0),
        Kind("checking", "Checking account (demand deposit)", ASSET, tier=0),
        Kind("time_deposit", "Time deposit", ASSET, tier=1),
        Kind("fx", "Foreign currency (account or cash)", ASSET, tier=0, default_commodity=None),
        Kind("gold", "Gold", ASSET, tier=1, default_commodity="XAU_G"),
        Kind("fund", "Investment fund", ASSET, tier=1, default_commodity=None),
        Kind("stock", "Stock", ASSET, tier=1, default_commodity=None),
        Kind("pension", "Private pension (BES)", ASSET, tier=2),
        Kind("receivable", "Money owed to you", ASSET, tier=2),
        Kind("real_estate", "Real estate", ASSET, tier=3),
        Kind("vehicle", "Vehicle", ASSET, tier=3),
        Kind("other_asset", "Other asset", ASSET, tier=3),
        Kind("credit_card", "Credit card", LIABILITY, term=SHORT),
        Kind("overdraft", "Overdraft account (KMH)", LIABILITY, term=SHORT),
        Kind("personal_debt", "Money owed to people", LIABILITY, term=SHORT),
        Kind("consumer_loan", "Consumer loan", LIABILITY, term=LONG),
        Kind("mortgage", "Mortgage", LIABILITY, term=LONG),
        Kind("vehicle_loan", "Vehicle loan", LIABILITY, term=LONG),
        Kind("other_debt", "Other debt", LIABILITY, term=LONG),
    )
}

DEFAULT_KIND = {ASSET: "other_asset", LIABILITY: "other_debt"}


def canonical_root(segment: str) -> str | None:
    """``"assets"`` or ``"asset"`` → ``"Assets"``; ``None`` if the segment is not a root."""
    return _ROOT_BY_FOLD.get(fold(segment))


def kinds_for(account_type: str) -> list[Kind]:
    return [k for k in KINDS.values() if k.type == account_type]


def effective_tier(account_type: str, kind: str | None, override: int | None) -> int | None:
    """Liquidity tier of an asset account: the override, else the kind's default."""
    if account_type != ASSET:
        return None
    if override is not None:
        return override
    k = KINDS.get(kind or "")
    return k.tier if k else 3


def debt_term(kind: str | None) -> str:
    k = KINDS.get(kind or "")
    return k.term if k and k.term else LONG
