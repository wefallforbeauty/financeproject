"""Transactions: the list with search and filters, one transaction with its history, editing, adding."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from financeproject import config
from financeproject.core.dates import parse_month
from financeproject.errors import FinanceError
from financeproject.ledger.accounts import create_account, find_account, get_account
from financeproject.ledger.db import auto_backup
from financeproject.ledger.history import history_of
from financeproject.ledger.installments import record_installment_purchase, schedule
from financeproject.ledger.models import Account, Transaction
from financeproject.ledger.queries import find_transactions
from financeproject.ledger.transactions import (
    SIMPLE,
    TRANSFER,
    delete_transaction,
    record_exchange,
    record_expense,
    record_income,
    record_transfer,
    shape,
    sides,
    update_transaction,
)
from financeproject.web.app import flash, ledger_session, redirect, render
from financeproject.web.forms import amount_value, choices, day_value, optional
from financeproject.web.i18n import translator

router = APIRouter()
PAGE = 50
KINDS = ("expense", "income", "transfer", "exchange")


def _filters(request: Request) -> dict:
    q = request.query_params
    page = q.get("page", "1")
    return {
        "q": q.get("q", "").strip(),
        "account": q.get("account", ""),
        "start": q.get("start", ""),
        "end": q.get("end", ""),
        "uncategorized": q.get("uncategorized") == "1",
        "import_id": q.get("import", ""),
        "page": max(1, int(page)) if page.isdigit() else 1,
    }


@router.get("/transactions")
def transaction_list(request: Request):
    f = _filters(request)
    with ledger_session(request) as s:
        account = get_account(s, f["account"]) if f["account"] else None
        rows, total = find_transactions(
            s,
            text=f["q"] or None,
            account_id=account.id if account else None,
            start=day_value(f["start"], "start date") if f["start"] else None,
            end=day_value(f["end"], "end date") if f["end"] else None,
            uncategorized=f["uncategorized"],
            import_id=int(f["import_id"]) if f["import_id"].isdigit() else None,
            limit=PAGE,
            offset=(f["page"] - 1) * PAGE,
        )
        options = choices(s)
    pages = max(1, -(-total // PAGE))
    template = "_transactions_table.html" if request.headers.get("hx-target") == "tx-results" else "transactions.html"
    return render(
        request, template, section="transactions", rows=rows, total=total, page=f["page"], pages=pages, f=f, options=options
    )


def _detail(request: Request, s, tx: Transaction, status: int = 200, error: str | None = None, form: dict | None = None):
    kind = shape(tx)
    own = cat = None
    if kind == SIMPLE:
        own, cat = sides(tx)
    batch = tx.import_link.batch if tx.import_link else None
    plan = schedule(tx.installment_plan) if tx.installment_plan is not None else None
    return render(
        request,
        "transaction.html",
        status,
        section="transactions",
        tx=tx,
        kind=kind,
        own=own,
        cat=cat,
        batch=batch,
        plan=plan,
        loan=tx.loan_payment,
        history=history_of(s, tx.id),
        options=choices(s),
        error=error,
        form=form or {},
    )


def _get(s, transaction_id: int) -> Transaction:
    tx = s.get(Transaction, transaction_id)
    if tx is None:
        raise HTTPException(status_code=404, detail=f"No transaction #{transaction_id}")
    return tx


@router.get("/transactions/new")
def new_form(request: Request, kind: str = "expense"):
    if kind not in KINDS:
        kind = "expense"
    with ledger_session(request) as s:
        options = choices(s)
    return render(request, "transaction_new.html", section="transactions", kind=kind, options=options, form={}, error=None)


@router.post("/transactions/new")
async def create(request: Request):
    form = await request.form()
    data = {k: v for k, v in form.items() if isinstance(v, str)}
    kind = data.get("kind", "expense")
    _ = translator(config.language())
    try:
        with ledger_session(request) as s:
            on = day_value(data.get("date"))
            description = optional(data.get("description"))
            payee = optional(data.get("payee"))
            if kind in ("expense", "income"):
                amount = amount_value(data.get("amount"))
                category = _category(s, kind, data)
                text = description or category.path.split(":")[-1]
                if kind == "expense":
                    count = data.get("installments", "").strip()
                    if count:
                        first = parse_month(data["first_due"]) if optional(data.get("first_due")) else None
                        tx = record_installment_purchase(s, amount, category, data.get("account", ""), on, text, int(count), first, payee)
                    else:
                        tx = record_expense(s, amount, category, data.get("account", ""), on, text, payee)
                else:
                    tx = record_income(s, amount, category, data.get("account", ""), on, text, payee)
            elif kind == "transfer":
                tx = record_transfer(s, amount_value(data.get("amount")), data.get("from", ""), data.get("to", ""), on, description)
            elif kind == "exchange":
                tx = record_exchange(
                    s, data.get("from", ""), amount_value(data.get("paid"), "amount paid"), data.get("to", ""),
                    amount_value(data.get("received"), "amount received"), on, description,
                )
            else:
                raise FinanceError(f"Unknown kind {kind!r}")
            number = tx.id
    except (FinanceError, ValueError) as exc:
        with ledger_session(request) as s:
            options = choices(s)
        return render(
            request, "transaction_new.html", 400, section="transactions", kind=kind, options=options, form=data, error=str(exc)
        )
    flash(request, "ok", _("Transaction #{n} added.", n=number))
    return redirect(f"/transactions/{number}")


def _category(s, kind: str, data: dict) -> Account:
    """The chosen category, or a new one typed in."""
    new = optional(data.get("new_category"))
    if new:
        root = "Expenses" if kind == "expense" else "Income"
        path = new if new.split(":")[0] in ("Expenses", "Income") else f"{root}:{new}"
        existing = get_account(s, path)
        return existing or create_account(s, path)
    return find_account(s, data.get("category", ""))


@router.get("/transactions/{transaction_id}")
def detail(request: Request, transaction_id: int):
    with ledger_session(request) as s:
        return _detail(request, s, _get(s, transaction_id))


@router.post("/transactions/{transaction_id}")
async def edit(request: Request, transaction_id: int):
    form = await request.form()
    data = {k: v for k, v in form.items() if isinstance(v, str)}
    _ = translator(config.language())
    try:
        with ledger_session(request) as s:
            tx = _get(s, transaction_id)
            changes: dict = {}
            on = day_value(data.get("date"))
            if on != tx.date:
                changes["on"] = on
            for field in ("description", "payee", "note"):
                value = (data.get(field) or "").strip()
                if value != (getattr(tx, field) or ""):
                    changes[field] = value
            kind = shape(tx)
            if kind in (SIMPLE, TRANSFER) and data.get("amount"):
                amount = amount_value(data.get("amount"))
                current = abs(tx.postings[0].amount)
                if amount != current:
                    changes["amount"] = amount
            if kind == SIMPLE:
                own, cat = sides(tx)
                if data.get("category") and data["category"] != cat.account.path:
                    changes["category"] = data["category"]
                if data.get("account") and data["account"] != own.account.path:
                    changes["account"] = data["account"]
            if changes:
                update_transaction(s, transaction_id, **changes)
    except (FinanceError, ValueError) as exc:
        with ledger_session(request) as s:
            return _detail(request, s, _get(s, transaction_id), 400, error=str(exc), form=data)
    flash(request, "ok", _("Saved.") if changes else _("Nothing changed."))
    return redirect(f"/transactions/{transaction_id}")


@router.post("/transactions/{transaction_id}/delete")
def delete(request: Request, transaction_id: int):
    _ = translator(config.language())
    with ledger_session(request) as s:
        _get(s, transaction_id)
    auto_backup(request.app.state.ledger.path, config.backup_dir())
    with ledger_session(request) as s:
        tx = delete_transaction(s, transaction_id)
        label = f"#{transaction_id} {tx.date} {tx.description}"
    flash(request, "ok", _("Deleted {label}. A backup was made first, and the history keeps a copy.", label=label))
    return redirect("/transactions")

