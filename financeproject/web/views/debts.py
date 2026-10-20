"""Debts: every debt with its cost, the loans with their payment plans (added, paid, deleted here), card
installments, a plan for paying debts off, and two calculators (an offer's annual cost rate; cash or
installments)."""

from __future__ import annotations

from collections import defaultdict
from datetime import date
from decimal import Decimal

from fastapi import APIRouter, HTTPException, Query, Request
from sqlalchemy import select

from financeproject import config
from financeproject.core.dates import add_months, month_start
from financeproject.core.kinds import LIABILITY
from financeproject.core.loans import (
    ANNUITY,
    AVALANCHE,
    EQUAL_PRINCIPAL,
    LEAST,
    SNOWBALL,
    LoanTerms,
    cash_or_installments,
    effective_annual,
    monthly_from_annual,
    offer_cost_rate,
    schedule,
)
from financeproject.core.loans import payoff as simulate
from financeproject.errors import FinanceError
from financeproject.ledger.accounts import find_account
from financeproject.ledger.db import auto_backup
from financeproject.ledger.installments import not_billed, plans
from financeproject.ledger.installments import schedule as installment_schedule
from financeproject.ledger.loans import (
    DEFAULT_TAXES,
    LOAN_KINDS,
    cost_rate,
    define_loan,
    delete_loan,
    loan_state,
    plan,
    record_payment,
    terms_of,
)
from financeproject.ledger.models import Account, Loan
from financeproject.ledger.queries import account_balance
from financeproject.reports.debt import (
    Inflation,
    debt_report,
    payoff_debts,
    trailing_inflation,
)
from financeproject.web.app import flash, ledger_session, money, redirect, render
from financeproject.web.forms import (
    amount_value,
    bounded,
    choices,
    day_value,
    optional,
    percent_value,
)
from financeproject.web.i18n import translator

router = APIRouter()

TABS = (
    ("overview", "Debts", "/debts"),
    ("installments", "Card installments", "/debts/installments"),
    ("payoff", "Paying off", "/debts/payoff"),
    ("calculators", "Calculators", "/debts/calculators"),
)
METHODS = {"annuity": ANNUITY, "equal-principal": EQUAL_PRINCIPAL}
KIND_LABELS = {
    "consumer_loan": "Consumer loan",
    "vehicle_loan": "Vehicle loan",
    "mortgage": "Mortgage",
    "other_debt": "Other debt",
    "personal_debt": "Money owed to people",
}
PLANS = ("As now: least payments only", "Avalanche: highest rate first", "Snowball: smallest debt first")
PAYMENT_STATUS = ("paid", "before the ledger", "overdue")
TEXTS = (*(label for _, label, _url in TABS), *KIND_LABELS.values(), *PLANS, *PAYMENT_STATUS)
"""Texts this module translates through a variable (the translation test reads them)."""


def _page(request: Request, template: str, tab: str, status: int = 200, **context):
    return render(request, template, status, section="debts", tabs=TABS, tab=tab, **context)


def _get_loan(s, loan_id: int) -> Loan:
    loan = s.get(Loan, loan_id)
    if loan is None:
        raise HTTPException(status_code=404, detail=f"No loan #{loan_id}")
    return loan


def inflation_source(inflation: Inflation | str, _) -> str:
    """Where an inflation figure comes from, in the interface's language."""
    source = inflation if isinstance(inflation, str) else inflation.source
    if source == "given":
        return _("given")
    if isinstance(inflation, Inflation) and inflation.month is not None:
        return _("CPI, 12 months to {month}", month=f"{inflation.month:%Y-%m}")
    return source


# --------------------------------------------------------------------------- every debt


@router.get("/debts")
def debts_page(request: Request, on: str = Query("", alias="date"), inflation: str = ""):
    at = day_value(on) if optional(on) else date.today()
    rate = float(percent_value(inflation, "inflation")) if optional(inflation) else None
    _ = translator(config.language())
    with ledger_session(request) as s:
        report = debt_report(s, at, rate)
        return _page(
            request, "debts.html", "overview", report=report, at=at, form={"date": on, "inflation": inflation},
            inflation_text=inflation_source(report.inflation, _) if report.inflation else "",
        )


# --------------------------------------------------------------------------- a new loan


def _loan_form_context(s, form: dict, error: str | None = None) -> dict:
    liabilities = [
        a for a in s.scalars(select(Account).where(Account.type == LIABILITY, Account.closed_on.is_(None)).order_by(Account.path))
        if a.kind in LOAN_KINDS and not s.scalar(select(Loan.id).where(Loan.account_id == a.id))
    ]
    return {
        "liabilities": liabilities, "options": choices(s), "kinds": KIND_LABELS, "form": form, "error": error,
        "taxes": {k: (float(v[0] * 100), float(v[1] * 100)) for k, v in DEFAULT_TAXES.items()},
    }


@router.get("/debts/loans/new")
def new_loan_form(request: Request):
    with ledger_session(request) as s:
        return _page(request, "debts_loan_new.html", "overview", **_loan_form_context(s, {}))


def _create_loan(s, data: dict):
    name = (data.get("name") or "").strip()
    account = optional(data.get("new_account")) or optional(data.get("account"))
    if not account:
        raise FinanceError("Choose the loan's account, or give the path of a new one.")
    terms_by = data.get("terms", "rate")
    method = data.get("method", "annuity")
    if method not in METHODS:
        raise FinanceError(f"Unknown method {method!r}.")
    start = data.get("start", "new")
    kwargs = {}
    if start == "new":
        kwargs["disburse_to"] = optional(data.get("paid_into"))
        if not kwargs["disburse_to"]:
            raise FinanceError("Choose the account the loan was paid into.")
    elif start == "before":
        kwargs["opening_on"] = day_value(data.get("opening_on"), "day of the opening balance")
        if optional(data.get("owed")):
            kwargs["opening_owed"] = amount_value(data.get("owed"), "principal owed")
    if optional(data.get("disbursed")):
        kwargs["disbursed"] = day_value(data.get("disbursed"), "payout day")
    count = data.get("count", "").strip()
    if not count.isdigit():
        raise FinanceError("The number of installments is missing.")
    return define_loan(
        s,
        name,
        account,
        principal=amount_value(data.get("principal"), "principal"),
        count=int(count),
        first_due=day_value(data.get("first_due"), "first due date"),
        monthly_rate=percent_value(data.get("rate"), "interest rate") if terms_by == "rate" else None,
        installment=amount_value(data.get("installment"), "installment") if terms_by == "installment" else None,
        method=METHODS[method],
        kkdf=percent_value(data.get("kkdf"), "KKDF") if optional(data.get("kkdf")) else None,
        bsmv=percent_value(data.get("bsmv"), "BSMV") if optional(data.get("bsmv")) else None,
        kind=data.get("kind", "consumer_loan"),
        fees=amount_value(data.get("fees"), "fees", positive=False) if optional(data.get("fees")) else Decimal(0),
        reference=optional(data.get("reference")),
        **kwargs,
    )


@router.post("/debts/loans/new")
async def create_loan(request: Request):
    form = await request.form()
    data = {k: v for k, v in form.items() if isinstance(v, str)}
    _ = translator(config.language())
    try:
        with ledger_session(request) as s:
            loan, _created, _inferred = _create_loan(s, data)
            loan_id, name = loan.id, loan.name
    except (FinanceError, ValueError) as exc:
        with ledger_session(request) as s:
            return _page(request, "debts_loan_new.html", "overview", 400, **_loan_form_context(s, data, str(exc)))
    flash(request, "ok", _("Loan added: {name}", name=name))
    return redirect(f"/debts/loans/{loan_id}")


# --------------------------------------------------------------------------- one loan


def _loan_context(s, loan: Loan, today: date) -> dict:
    items = plan(loan)
    paid = {p.number: p.transaction.date for p in loan.payments if p.number is not None}
    rows = []
    for i in items:
        if i.number in paid:
            status, tone = "paid", "good"
        elif i.number < loan.start_number:
            status, tone = "before the ledger", ""
        elif i.due < today:
            status, tone = "overdue", "warning"
        else:
            status, tone = "", ""
        rows.append({"item": i, "status": status, "tone": tone, "paid_on": paid.get(i.number)})
    totals = {attr: sum((getattr(i, attr) for i in items), Decimal(0)) for attr in ("payment", "principal", "interest", "kkdf", "bsmv")}
    return {
        "loan": loan, "terms": terms_of(loan), "rows": rows, "totals": totals, "cost": cost_rate(loan),
        "state": loan_state(s, loan, today), "first": items[0] if items else None, "last": items[-1] if items else None,
        "options": choices(s), "kind_label": KIND_LABELS.get(loan.account.kind, ""),
    }


@router.get("/debts/loans/{loan_id}")
def loan_page(request: Request, loan_id: int):
    with ledger_session(request) as s:
        loan = _get_loan(s, loan_id)
        return _page(request, "debts_loan.html", "overview", error=None, form={}, **_loan_context(s, loan, date.today()))


@router.post("/debts/loans/{loan_id}/pay")
async def pay_loan(request: Request, loan_id: int):
    form = await request.form()
    data = {k: v for k, v in form.items() if isinstance(v, str)}
    _ = translator(config.language())
    try:
        with ledger_session(request) as s:
            loan = _get_loan(s, loan_id)
            tx = record_payment(
                s, loan, day_value(data.get("date")), data.get("from", ""),
                amount_value(data.get("amount"), "amount") if optional(data.get("amount")) else None, None,
                extra=data.get("extra") == "1",
            )
            principal = sum((p.amount for p in tx.postings if p.account_id == loan.account_id), Decimal(0))
            paid = -sum((p.amount for p in tx.postings if p.amount < 0), Decimal(0))
            number = tx.id
    except (FinanceError, ValueError) as exc:
        with ledger_session(request) as s:
            loan = _get_loan(s, loan_id)
            return _page(request, "debts_loan.html", "overview", 400, error=str(exc), form=data, **_loan_context(s, loan, date.today()))
    flash(request, "ok", _(
        "Payment #{n}: {paid} = principal {principal} + interest and taxes {rest}.",
        n=number, paid=money(paid), principal=money(principal), rest=money(paid - principal),
    ))
    return redirect(f"/debts/loans/{loan_id}")


@router.post("/debts/loans/{loan_id}/delete")
def delete_loan_page(request: Request, loan_id: int):
    _ = translator(config.language())
    with ledger_session(request) as s:
        _get_loan(s, loan_id)
    auto_backup(request.app.state.ledger.path, config.backup_dir())
    with ledger_session(request) as s:
        loan = _get_loan(s, loan_id)
        name = loan.name
        delete_loan(s, loan)
    flash(request, "ok", _("The terms of {name} were deleted; its account and transactions are kept.", name=name))
    return redirect("/debts")


# --------------------------------------------------------------------------- card installments


@router.get("/debts/installments")
def installments_page(request: Request, months: int | None = None):
    today = date.today()
    current = month_start(today)
    months_ = bounded(months, 12, 1, 36)
    with ledger_session(request) as s:
        items = [p for p in plans(s) if p.transaction.date <= today]
        rows, dues = [], defaultdict(Decimal)
        for item in items:
            parts = installment_schedule(item)
            billed = [i for i in parts if i.month <= current]
            done = sum((i.amount for i in billed), Decimal(0))
            for i in parts:
                if current <= i.month < add_months(current, months_):
                    dues[i.month] += i.amount
            rows.append({"plan": item, "billed": len(billed), "done": done, "left": item.total - done, "last": parts[-1].month})
        cards = []
        for path, later in sorted(not_billed(s, current, today).items()):
            acc = find_account(s, path)
            owed = -account_balance(s, acc, today).get(acc.commodity_code or "TRY", Decimal(0))
            cards.append({"path": path, "owed": owed, "later": later, "rest": owed - later})
        return _page(
            request, "debts_installments.html", "installments", rows=rows, dues=sorted(dues.items()), cards=cards,
            current=current, months=months_,
        )


# --------------------------------------------------------------------------- paying off


@router.get("/debts/payoff")
def payoff_page(request: Request):
    q = request.query_params
    today = date.today()
    error = result = None
    with ledger_session(request) as s:
        report = debt_report(s, today)
        others = [line for line in report.lines if line.state is None]
        loans = [line for line in report.lines if line.state is not None]
        if optional(q.get("budget")):
            try:
                budget = amount_value(q.get("budget"), "budget")
                rates = {
                    line.account: percent_value(q.get(f"rate:{line.account}"), "rate")
                    for line in others if optional(q.get(f"rate:{line.account}"))
                }
                minimums = {
                    line.account: amount_value(q.get(f"min:{line.account}"), "least payment", positive=False)
                    for line in others if optional(q.get(f"min:{line.account}"))
                }
                debts = payoff_debts(s, today, rates, minimums)
                if not debts:
                    raise FinanceError("No debts to plan: add loans, or give the other debts a rate or a least payment.")
                least = sum((d.minimum for d in debts), Decimal(0))
                if budget < least:
                    raise FinanceError(f"The budget does not cover the least payments ({money(least)} a month).")
                result = {
                    "budget": budget, "least": least, "debts": debts,
                    "plans": [
                        (PLANS[0], simulate(debts, least, LEAST)),
                        (PLANS[1], simulate(debts, budget, AVALANCHE)),
                        (PLANS[2], simulate(debts, budget, SNOWBALL)),
                    ],
                }
                avalanche, snowball = result["plans"][1][1], result["plans"][2][1]
                result["saving"] = (
                    snowball.interest - avalanche.interest if avalanche.feasible and snowball.feasible else None
                )
            except (FinanceError, ValueError) as exc:
                error = str(exc)
        return _page(
            request, "debts_payoff.html", "payoff", 400 if error else 200, others=others, loans=loans, result=result,
            error=error, q=q,
        )


# --------------------------------------------------------------------------- calculators


def _offer(q) -> dict:
    """The annual cost rate of a credit offer, as ``finance apr`` works it out."""
    principal = amount_value(q.get("amount"), "credit amount")
    count = q.get("count", "").strip()
    if not count.isdigit() or int(count) < 1:
        raise FinanceError("The number of installments must be at least 1.")
    count = int(count)
    first_in = int(q.get("first_in") or 1)
    if first_in < 0:
        raise FinanceError("The months until the first installment cannot be negative.")
    if q.get("mode", "installment") == "installment":
        payments = [amount_value(q.get("installment"), "installment")] * count
        if optional(q.get("last")):
            payments[-1] = amount_value(q.get("last"), "last installment")
    else:
        method = q.get("method", "annuity")
        if method not in METHODS:
            raise FinanceError(f"Unknown method {method!r}.")
        try:
            terms = LoanTerms(
                principal, percent_value(q.get("rate"), "interest rate"), count, date(2001, 1, 1), METHODS[method],
                percent_value(q.get("kkdf") or "15", "KKDF"), percent_value(q.get("bsmv") or "15", "BSMV"),
            )
        except ValueError as exc:
            raise FinanceError(str(exc)) from exc
        payments = [i.payment for i in schedule(terms)]
    fees = amount_value(q.get("fees"), "fees", positive=False) if optional(q.get("fees")) else Decimal(0)
    try:
        rate = offer_cost_rate(principal, payments, fees, first_in)
    except ValueError as exc:
        raise FinanceError(str(exc)) from exc
    total = sum(payments, Decimal(0)) + fees
    monthly = (1 + rate) ** (1 / 12) - 1
    return {
        "principal": principal, "payments": payments, "fees": fees, "first_in": first_in, "total": total,
        "cost": total - principal, "rate": rate, "monthly": monthly, "simple": monthly * 12,
    }


def _cash(s, q) -> dict:
    """Cash or installments, as ``finance cash-or-installments`` compares them."""
    count = q.get("count", "").strip()
    if not count.isdigit() or int(count) < 1:
        raise FinanceError("The number of installments must be at least 1.")
    first_in = int(q.get("first_in") or 1)
    if optional(q.get("rate")) and optional(q.get("annual_rate")):
        raise FinanceError("Give what the money earns a month or a year, not both.")
    if optional(q.get("rate")):
        monthly, source = float(percent_value(q.get("rate"), "rate")), "given"
    elif optional(q.get("annual_rate")):
        monthly, source = monthly_from_annual(float(percent_value(q.get("annual_rate"), "rate"))), "given"
    else:
        expected = trailing_inflation(s)
        if expected is None:
            raise FinanceError("No CPI in the ledger: give what your money earns meanwhile.")
        monthly, source = monthly_from_annual(expected.rate), expected
    try:
        r = cash_or_installments(amount_value(q.get("cash"), "cash price"), amount_value(q.get("installment"), "installment"),
                                 int(count), monthly, first_in)
    except ValueError as exc:
        raise FinanceError(str(exc)) from exc
    return {"r": r, "monthly": monthly, "yearly": effective_annual(monthly), "source": source,
            "implied_year": effective_annual(r.implied_rate) if r.implied_rate > -1 else None,
            "gap": abs(Decimal(repr(r.advantage))).quantize(Decimal("0.01"))}


@router.get("/debts/calculators")
def calculators_page(request: Request):
    q = request.query_params
    offer = cash = None
    offer_error = cash_error = None
    with ledger_session(request) as s:
        if q.get("calc") == "offer":
            try:
                offer = _offer(q)
            except (FinanceError, ValueError) as exc:
                offer_error = str(exc)
        elif q.get("calc") == "cash":
            try:
                cash = _cash(s, q)
            except (FinanceError, ValueError) as exc:
                cash_error = str(exc)
            else:
                cash["source_text"] = inflation_source(cash["source"], translator(config.language()))
        return _page(
            request, "debts_calculators.html", "calculators", 400 if offer_error or cash_error else 200, q=q, offer=offer,
            cash=cash, offer_error=offer_error, cash_error=cash_error,
        )
