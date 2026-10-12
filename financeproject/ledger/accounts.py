"""Account paths, creation and lookup."""

from __future__ import annotations

import re
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from financeproject.core.kinds import (
    ASSET,
    DEFAULT_KIND,
    KINDS,
    LIABILITY,
    ROOTS,
    TIERS,
    canonical_root,
    effective_tier,
    kinds_for,
)
from financeproject.core.text import fold
from financeproject.errors import AccountError, AccountNotFound, AmbiguousAccount
from financeproject.ledger.db import TRADING_ROOT
from financeproject.ledger.models import Account, Commodity, Person

SHARED_OWNER = "shared"


def normalize_path(path: str) -> str:
    """Trim the segments and spell the root canonically: ``"assets: Bank"`` → ``"Assets:Bank"``."""
    parts = [re.sub(r"\s+", " ", p).strip() for p in path.split(":")]
    if any(not p for p in parts):
        raise AccountError(f"Invalid account name {path!r} (empty segment).")
    root = canonical_root(parts[0])
    if root is None:
        raise AccountError(f"Account names start with one of: {', '.join(ROOTS)}. Got {path!r}")
    if len(parts) < 2:
        raise AccountError(f"A root account cannot be used on its own; add a sub-account, for example '{root}:Bank'.")
    return ":".join([root, *parts[1:]])


def account_type_of(path: str) -> str:
    return ROOTS[normalize_path(path).split(":")[0]]


def get_person(session: Session, name: str, create: bool = False) -> Person | None:
    key = fold(name)
    for person in session.scalars(select(Person)):
        if fold(person.name) == key:
            return person
    if not create:
        return None
    person = Person(name=name.strip())
    session.add(person)
    session.flush()
    return person


def create_account(
    session: Session,
    path: str,
    *,
    kind: str | None = None,
    commodity: str | None = None,
    owner: str | None = None,
    institution: str | None = None,
    tier: int | None = None,
    opened_on: date | None = None,
    note: str | None = None,
) -> Account:
    p = normalize_path(path)
    key = fold(p)
    for existing in session.scalars(select(Account.path)):
        if fold(existing) == key:
            raise AccountError(f"This account already exists: {existing}")
    account_type = ROOTS[p.split(":")[0]]

    if account_type in (ASSET, LIABILITY):
        kind = kind or DEFAULT_KIND[account_type]
        k = KINDS.get(kind)
        if k is None or k.type != account_type:
            options = ", ".join(x.key for x in kinds_for(account_type))
            raise AccountError(f"'{kind}' is not a valid kind for this account. Options: {options}")
        code = commodity or k.default_commodity
        if code is None:
            raise AccountError(f"A '{k.label}' account needs a commodity (for example --commodity USD).")
    else:
        if kind:
            raise AccountError("Only asset and liability accounts have a kind.")
        code = commodity
    if code is not None:
        code = code.strip().upper()
        if session.get(Commodity, code) is None:
            raise AccountError(f"Unknown commodity {code}. Add it first with 'finance commodity add'.")
    if tier is not None:
        if account_type != ASSET:
            raise AccountError("Only asset accounts have a liquidity tier.")
        if tier not in TIERS:
            raise AccountError(f"The liquidity tier must be between {min(TIERS)} and {max(TIERS)}.")

    person = None
    if owner and fold(owner) != SHARED_OWNER:
        person = get_person(session, owner, create=True)
    account = Account(
        path=p,
        type=account_type,
        kind=kind if account_type in (ASSET, LIABILITY) else None,
        commodity_code=code,
        owner=person,
        institution=institution,
        liquidity_tier=tier,
        opened_on=opened_on,
        note=note,
    )
    session.add(account)
    session.flush()
    return account


def get_account(session: Session, path: str) -> Account | None:
    return session.scalar(select(Account).where(Account.path == normalize_path(path)))


def get_or_create_account(session: Session, path: str, **kwargs) -> Account:
    return get_account(session, path) or create_account(session, path, **kwargs)


def trading_account(session: Session, commodity: str) -> Account:
    """``Equity:Trading:<CODE>``: the per-commodity trading account of exchanges."""
    return get_or_create_account(session, f"{TRADING_ROOT}:{commodity}", commodity=commodity)


def find_account(session: Session, query: str | Account) -> Account:
    """Find an account by its full path or an unambiguous part of it.

    Matching ignores case, accents and Turkish letters. Tried in order: the full
    path, a unique ending made of whole segments (``"Groceries"`` →
    ``"Expenses:Food:Groceries"``), then a unique substring.
    """
    if isinstance(query, Account):
        return query
    q = fold(query)
    if not q:
        raise AccountNotFound("The account name is empty.")
    keyed = [(fold(a.path), a) for a in session.scalars(select(Account))]
    for k, a in keyed:
        if k == q:
            return a
    for matcher in (lambda k: k.endswith(":" + q), lambda k: q in k):
        hits = [a for k, a in keyed if matcher(k)]
        if len(hits) == 1:
            return hits[0]
        if hits:
            raise AmbiguousAccount(query, sorted(a.path for a in hits))
    raise AccountNotFound(f"No account matches {query!r}. List them with: finance account list")


def account_tier(account: Account) -> int | None:
    return effective_tier(account.type, account.kind, account.liquidity_tier)


def owner_name(account: Account) -> str:
    return account.owner.name if account.owner else SHARED_OWNER
