"""Accounts: balances by kind, adding an account with its opening balance, closing one."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from fastapi import APIRouter, HTTPException, Request
from sqlalchemy import select

from financeproject import config
from financeproject.core.kinds import ASSET, EQUITY, EXPENSE, INCOME, KINDS, LIABILITY
from financeproject.errors import FinanceError
from financeproject.ledger.accounts import close_account, create_account, owner_name
from financeproject.ledger.db import TRADING_ROOT
from financeproject.ledger.models import Account
from financeproject.ledger.queries import balances_by_account, find_transactions
from financeproject.ledger.transactions import opening_balance
from financeproject.reports.context import load_market_data
from financeproject.web.app import flash, ledger_session, redirect, render
from financeproject.web.forms import (
    account_path,
    amount_value,
    commodities,
    day_value,
    kind_groups,
    optional,
)
from financeproject.web.i18n import translator

router = APIRouter()
SECTIONS = ((ASSET, "Assets"), (LIABILITY, "Debts"), (INCOME, "Income categories"), (EXPENSE, "Expense categories"))


def _rows(s, today: date) -> dict[str, list[dict]]:
    market = load_market_data(s)
    balances = balances_by_account(s, today)
    out: dict[str, list[dict]] = {t: [] for t, _ in SECTIONS}
    for a in s.scalars(select(Account).order_by(Account.path)):
        if a.type == EQUITY or a.path.startswith(TRADING_ROOT):
            continue
        sign = -1 if a.type in (LIABILITY, INCOME) else 1
        held = {code: sign * value for code, value in balances.get(a.id, {}).items()}
        value = Decimal(0)
        priced = True
        for code, quantity in held.items():
            price = market.book.resolve(code, today)
            if price is None:
                priced = False
            else:
                value += quantity * price.value
        out[a.type].append(
            {
                "account": a,
                "kind": KINDS[a.kind].label if a.kind in KINDS else "",
                "held": held,
                "value": value if priced else None,
                "owner": owner_name(a),
            }
        )
    return out


@router.get("/accounts")
def account_list(request: Request):
    today = date.today()
    with ledger_session(request) as s:
        rows = _rows(s, today)
        units = commodities(s)
    return render(
        request, "accounts.html", section="accounts", rows=rows, sections=SECTIONS, kinds=kind_groups(), units=units,
        form={}, error=None,
    )


@router.post("/accounts")
async def add(request: Request):
    form = await request.form()
    data = {k: v for k, v in form.items() if isinstance(v, str)}
    _ = translator(config.language())
    kind = data.get("kind", "checking")
    try:
        with ledger_session(request) as s:
            path = account_path(kind, data.get("name", ""))
            account = create_account(
                s,
                path,
                kind=kind if kind in KINDS else None,
                commodity=optional(data.get("commodity")) if kind in KINDS else None,
                institution=optional(data.get("institution")),
            )
            if optional(data.get("opening")):
                opening_balance(s, account, amount_value(data["opening"], "opening balance", positive=False), day_value(data.get("opening_date")))
            created = account.path
    except (FinanceError, ValueError) as exc:
        today = date.today()
        with ledger_session(request) as s:
            rows = _rows(s, today)
            units = commodities(s)
        return render(
            request, "accounts.html", 400, section="accounts", rows=rows, sections=SECTIONS, kinds=kind_groups(),
            units=units, form=data, error=str(exc),
        )
    flash(request, "ok", _("Account added: {path}", path=created))
    return redirect("/accounts")


def _get(s, account_id: int) -> Account:
    account = s.get(Account, account_id)
    if account is None:
        raise HTTPException(status_code=404, detail=f"No account #{account_id}")
    return account


@router.get("/accounts/{account_id}")
def detail(request: Request, account_id: int):
    today = date.today()
    with ledger_session(request) as s:
        account = _get(s, account_id)
        balances = balances_by_account(s, today).get(account.id, {})
        sign = -1 if account.type in (LIABILITY, INCOME) else 1
        held = {code: sign * value for code, value in balances.items()}
        recent, total = find_transactions(s, account_id=account.id, limit=20)
        return render(
            request,
            "account.html",
            section="accounts",
            account=account,
            kind=KINDS[account.kind].label if account.kind in KINDS else "",
            held=held,
            recent=recent,
            total=total,
            owner=owner_name(account),
        )


@router.post("/accounts/{account_id}/close")
async def close(request: Request, account_id: int):
    form = await request.form()
    _ = translator(config.language())
    with ledger_session(request) as s:
        account = close_account(s, _get(s, account_id), day_value(str(form.get("date", ""))))
        path = account.path
    flash(request, "ok", _("Closed: {path}", path=path))
    return redirect(f"/accounts/{account_id}")
