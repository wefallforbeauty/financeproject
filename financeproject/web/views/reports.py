"""Reports: the balance sheet, income statement, cash flow, where the money went (Sankey), the change in
net worth, household ratios and recurring payments. Each page is the matching command of the command line."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from fastapi import APIRouter, Query, Request
from sqlalchemy import select

from financeproject import config
from financeproject.core.dates import add_months, month_end, month_start
from financeproject.core.kinds import TERMS, TIERS, debt_term
from financeproject.core.recurring import ACTIVE, LATE, STOPPED
from financeproject.errors import FinanceError
from financeproject.ledger.models import Commodity, Person
from financeproject.reports.balance_sheet import build_balance_sheet
from financeproject.reports.cashflow import LINES, cash_flow
from financeproject.reports.change import UNITS as CHANGE_UNITS
from financeproject.reports.change import networth_change
from financeproject.reports.context import load_market_data
from financeproject.reports.income import IncomeStatement, income_statement
from financeproject.reports.ratios import BELOW, DANGER, OK, household_ratios
from financeproject.reports.recurring import recurring, upcoming
from financeproject.reports.sankey import build as build_sankey
from financeproject.web.app import (
    ledger_session,
    money,
    percent,
    price_info,
    redirect,
    render,
    short,
)
from financeproject.web.forms import (
    bounded,
    day_value,
    month_range,
    month_value,
    optional,
)
from financeproject.web.i18n import translator
from financeproject.web.views.dashboard import coming_text

router = APIRouter()

TABS = (
    ("balance-sheet", "Balance sheet"),
    ("income", "Income statement"),
    ("cash-flow", "Cash flow"),
    ("sankey", "Where the money went"),
    ("change", "Change in net worth"),
    ("ratios", "Ratios"),
    ("recurring", "Recurring payments"),
)
CHANGE_LABELS = {"try": "TRY", "real": "Real TRY", "usd": "USD", "gold": "Gold (g)"}
CHANGE_LINES = (
    ("exchange", "Exchange results (buying and selling currency or gold)"),
    ("opening", "Opening balances (accounts added to the ledger)"),
    ("equity", "Other equity"),
    ("unvalued", "Moved to or from assets without a price"),
)
RATIO_TEXTS = {
    "emergency": ("Emergency fund", "≥ {n} months", "liquid assets / monthly expenses (incl. loan principal)"),
    "emergency_wide": ("Emergency fund, wider", "", "also counting gold, funds and stocks (sellable within a week)"),
    "savings": ("Savings rate", "≥ 10%", "(income − expenses) / income; below zero = spending more than income"),
    "nonmortgage": ("Non-mortgage debt payments", "≤ 15%; danger ≥ 20%", "loan payments and card installments / income"),
    "all_debt": ("All debt payments", "≤ 35%; danger ≥ 45%", "including mortgage payments"),
    "liquid_share": ("Liquid assets / net worth", "≥ 15%", ""),
    "investment_share": ("Investment assets / net worth", "≥ 50%", "gold, funds, stocks, private pension"),
    "inflation": ("Inflation protection", "≥ 2", "growth of net worth / CPI inflation"),
    "debt_assets": ("Debt / assets", "", "solvency: net worth below zero means insolvent (DeVaney & Lytton 1995)"),
}
INFLATION_REASONS = {
    "no_cpi": "no CPI",
    "short_cpi": "the CPI does not go back a year",
    "no_net_worth": "no positive net worth a year earlier",
    "no_inflation": "no inflation in the window",
}
STATUS = {OK: ("good", "ok"), BELOW: ("warning", "below"), DANGER: ("critical", "danger")}
SERIES_STATUS = {ACTIVE: "active", LATE: "late", STOPPED: "stopped?"}
BENCHMARK_FIELDS = {"emergency": {"n": Decimal("2.5")}}
"""Numbers in the benchmarks, written in the chosen number format."""
RATIO_INPUTS = (
    ("Income", "income"),
    ("Expenses", "expenses"),
    ("Loan principal repaid", "principal_repaid"),
    ("Monthly expenses (incl. principal)", "monthly_expenses"),
    ("Loan payments (non-mortgage)", "loan_payments"),
    ("Mortgage payments", "mortgage_payments"),
    ("Card installments due", "installments_due"),
    ("Liquid assets", "liquid"),
    ("Investment assets", "investments"),
    ("Net worth", "net_worth"),
)
TEXTS = (
    *(label for _, label in TABS), *CHANGE_LABELS.values(), *(label for _, label in CHANGE_LINES),
    *(text for texts in RATIO_TEXTS.values() for text in texts if text), *INFLATION_REASONS.values(),
    *(label for _, label in STATUS.values()), *SERIES_STATUS.values(), *(label for label, _ in RATIO_INPUTS),
    *(label for _, label, _parts in LINES), *TIERS.values(), *TERMS.values(), "Income", "Expenses",
)
"""Texts this module translates through a variable (the translation test reads them)."""


def _page(request: Request, template: str, tab: str, **context):
    return render(request, template, section="reports", tabs=TABS, tab=tab, **context)


@router.get("/reports")
def reports_home():
    return redirect("/reports/balance-sheet")


# --------------------------------------------------------------------------- balance sheet


@router.get("/reports/balance-sheet")
def balance_sheet_page(request: Request, on: str = Query("", alias="date"), owner: str = ""):
    at = day_value(on) if optional(on) else date.today()
    with ledger_session(request) as s:
        market = load_market_data(s)
        bs = build_balance_sheet(s, at, owner=owner or None, market=market)
        people = list(s.scalars(select(Person.name).order_by(Person.name)))
        decimals = {c.code: c.decimals for c in s.scalars(select(Commodity))}
        prices = {code: price_info(market.book, code, at) for code in ("USD", "XAU_G")}
        return _page(
            request, "reports_balance_sheet.html", "balance-sheet", bs=bs, at=at, owner=owner, people=people,
            decimals=decimals, prices=prices, base_month=market.base_month, tiers=TIERS, terms=TERMS,
            debt_term=debt_term,
        )


# --------------------------------------------------------------------------- income statement


def _table_rows(table: dict[str, dict[date, Decimal]], months: list[date]) -> list[dict]:
    rows = []
    for key, total in IncomeStatement.totals(table).items():
        rows.append({"label": short(key), "cells": [table[key].get(m) for m in months], "total": total})
    return rows


def _sum_rows(rows: list[dict], count: int) -> list[Decimal]:
    return [sum((r["cells"][i] or Decimal(0) for r in rows), Decimal(0)) for i in range(count)]


@router.get("/reports/income")
def income_page(
    request: Request, start: str = "", end: str = "", months: int | None = None, depth: int | None = None, real: str = ""
):
    today = date.today()
    first, last = month_range(start, end, bounded(months, 6, 1, 36), today)
    depth_ = bounded(depth, 2, 1, 4)
    _ = translator(config.language())
    with ledger_session(request) as s:
        market = load_market_data(s)
        st = income_statement(s, first, last, depth_, "real" if real else "try", market, end=today)
    n = len(st.months)
    income, expenses = _table_rows(st.income, st.months), _table_rows(st.expenses, st.months)
    income_totals, expense_totals = _sum_rows(income, n), _sum_rows(expenses, n)
    chart = {
        "categories": [m.strftime("%Y-%m") for m in st.months],
        "series": [
            {"key": "income", "name": _("Income"), "values": [float(v) for v in income_totals]},
            {"key": "expenses", "name": _("Expenses"), "values": [float(v) for v in expense_totals]},
        ],
        "suffix": "TRY",
        "decimals": 0,
        "summary": _(
            "Income and expenses by month from {first} to {last}.",
            first=st.months[0].strftime("%Y-%m") if st.months else "", last=st.months[-1].strftime("%Y-%m") if st.months else "",
        ),
    }
    partial = bool(st.months) and st.end < month_end(st.months[-1])
    return _page(
        request, "reports_income.html", "income", st=st, months=st.months, income=income, expenses=expenses,
        income_totals=income_totals, expense_totals=expense_totals, net=[st.net(m) for m in st.months],
        rates=[st.savings_rate(m) for m in st.months], count=n, partial=partial, chart=chart, depth=depth_,
        real=bool(real), base_month=market.base_month, form={"start": start, "end": end, "months": bounded(months, 6, 1, 36)},
    )


# --------------------------------------------------------------------------- cash flow


@router.get("/reports/cash-flow")
def cash_flow_page(request: Request, start: str = "", end: str = "", months: int | None = None, real: str = ""):
    today = date.today()
    first, last = month_range(start, end, bounded(months, 6, 1, 36), today)
    with ledger_session(request) as s:
        market = load_market_data(s)
        flows = cash_flow(s, first, last, "real" if real else "try", market, end=today)
    rows = []
    for key, label, _parts in LINES:
        values = [f.line(key) for f in flows]
        always = key in ("income", "expenses", "operating", "investing", "cards", "loans")
        if always or any(abs(v) >= Decimal("0.5") for v in values):
            rows.append({"key": key, "label": label, "values": values, "total": sum(values, Decimal(0))})
    revaluation = [f.revaluation for f in flows]
    unvalued = sorted({item for f in flows for item in f.decomposition.unvalued})
    return _page(
        request, "reports_cash_flow.html", "cash-flow", flows=flows, rows=rows, revaluation=revaluation,
        revaluation_total=sum(revaluation, Decimal(0)), unvalued=unvalued, real=bool(real), base_month=market.base_month,
        form={"start": start, "end": end, "months": bounded(months, 6, 1, 36)},
    )


# --------------------------------------------------------------------------- Sankey


@router.get("/reports/sankey")
def sankey_page(request: Request, month: str = "", start: str = "", end: str = "", depth: int | None = None):
    today = date.today()
    if optional(month):
        first = last = month_value(month)
    elif optional(start) or optional(end):
        first, last = month_range(start, end, 1, today)
    else:
        first = last = add_months(month_start(today), -1)
    depth_ = bounded(depth, 2, 1, 4)
    _ = translator(config.language())
    with ledger_session(request) as s:
        st = income_statement(s, first, last, depth_, "try", load_market_data(s), end=today)
    data = build_sankey(IncomeStatement.totals(st.income), IncomeStatement.totals(st.expenses), min_share=Decimal("0.02"))
    names = {"Saved": _("Saved"), "From savings": _("From savings"), "Other": _("Other")}

    def nodes(items, side: str) -> list[dict]:
        out = []
        for k, node in enumerate(items):
            special = node.label in ("Saved", "From savings")
            share = float(node.value / data.total_income) if data.total_income else None
            out.append({
                "id": f"{side}{k}", "label": names.get(node.label, node.label), "value": float(node.value),
                "role": "savings" if special else ("income" if side == "in" else "expenses"), "share": share,
            })
        return out

    left, right = nodes(data.left, "in"), nodes(data.right, "out")
    chart = {
        "left": left, "right": right, "budget": {"id": "budget", "label": _("Budget"), "value": float(data.budget)},
        "suffix": "TRY", "share_text": _("{share} of income"),
        "summary": _(
            "Where the money went from {start} to {end}: income {income}, expenses {expenses}.",
            start=st.start, end=st.end, income=money(data.total_income, 0), expenses=money(data.total_expenses, 0),
        ),
    }
    return _page(
        request, "reports_sankey.html", "sankey", st=st, data=data, left=left, right=right, chart=chart,
        empty=not data.total_income and not data.total_expenses, depth=depth_,
        form={"month": month or (first.strftime("%Y-%m") if first == last else ""), "start": start, "end": end},
    )


# --------------------------------------------------------------------------- change in net worth


@router.get("/reports/change")
def change_page(
    request: Request, month: str = "", start: str = "", end: str = "", unit: str = "try", top: int | None = None
):
    if unit not in CHANGE_UNITS:
        raise FinanceError(f"Unknown unit {unit!r}.")
    today = date.today()
    if optional(month):
        m = month_value(month)
        a, b = month_start(m) - timedelta(days=1), min(month_end(m), today)
    else:
        a = day_value(start, "start date") if optional(start) else month_start(today) - timedelta(days=1)
        b = day_value(end, "end date") if optional(end) else today
    if a >= b:
        raise FinanceError("The start is not before the end.")
    top_ = bounded(top, 5, 1, 30)
    with ledger_session(request) as s:
        market = load_market_data(s)
        d = networth_change(s, a, b, unit, market)
    decimals = 2 if unit in ("usd", "gold") else 0
    groups = []
    for key, label in (("income", "Income"), ("expenses", "Expenses")):
        detail = sorted(d.details.get(key, {}).items(), key=lambda kv: -abs(kv[1]))
        rest = detail[top_:]
        groups.append({
            "label": label, "value": d.flows.get(key, Decimal(0)),
            "detail": [(short(path), v) for path, v in detail[:top_]],
            "rest": (len(rest), sum((v for _, v in rest), Decimal(0))) if rest else None,
        })
    shown = Decimal(1).scaleb(-decimals) / 2
    others = [(label, d.flows[key]) for key, label in CHANGE_LINES if abs(d.flows.get(key, Decimal(0))) >= shown]
    revaluation = sorted(d.revaluation.items(), key=lambda kv: -abs(kv[1]))
    return _page(
        request, "reports_change.html", "change", d=d, a=a, b=b, unit=unit, units=CHANGE_LABELS, decimals=decimals,
        suffix={"try": "TRY", "real": "TRY", "usd": "USD", "gold": "g"}[unit], groups=groups, others=others,
        saving=d.flows.get("income", Decimal(0)) + d.flows.get("expenses", Decimal(0)), revaluation=revaluation,
        base_month=market.base_month, form={"month": month, "start": start, "end": end}, top=top_,
    )


# --------------------------------------------------------------------------- ratios


def ratio_texts(ratio, _) -> dict:
    """A ratio's name, value, benchmark and note in the interface's language."""
    name, benchmark, note = RATIO_TEXTS.get(ratio.key, (ratio.name, ratio.benchmark, ratio.note))
    if ratio.key == "inflation":
        detail = ratio.detail
        if "reason" in detail:
            extra = _(INFLATION_REASONS.get(detail["reason"], detail["reason"]))
        else:
            extra = _(
                "net worth {growth}, CPI {inflation} ({start} – {end})", growth=percent(detail["growth"] / 100, 1, sign=True),
                inflation=percent(detail["inflation"] / 100, 1, sign=True), start=detail["start"], end=detail["end"],
            )
        note = f"{_(note)}; {extra}"
    else:
        note = _(note) if note else ""
    if ratio.value is None:
        value = "—"
    elif ratio.unit == "months":
        value = _("{n} months", n=money(ratio.value, 1))
    elif ratio.unit == "%":
        value = percent(ratio.value / 100, 1)
    else:
        value = f"{money(ratio.value, 2)} ×"
    tone, label = STATUS.get(ratio.status, ("", ""))
    fields = {k: money(v, 1) for k, v in BENCHMARK_FIELDS.get(ratio.key, {}).items()}
    return {"name": _(name), "value": value, "benchmark": _(benchmark, **fields) if benchmark else "", "note": note,
            "tone": tone, "status": _(label) if label else ""}


@router.get("/reports/ratios")
def ratios_page(request: Request, on: str = Query("", alias="date"), months: int | None = None):
    at = day_value(on) if optional(on) else date.today()
    months_ = bounded(months, 12, 1, 36)
    _ = translator(config.language())
    with ledger_session(request) as s:
        report = household_ratios(s, at, months_, load_market_data(s))
    inputs = [(label, getattr(report, attr)) for label, attr in RATIO_INPUTS]
    return _page(
        request, "reports_ratios.html", "ratios", report=report, ratios=[ratio_texts(r, _) for r in report.ratios],
        inputs=inputs, at=at, months=months_, first=report.start + timedelta(days=1),
    )


# --------------------------------------------------------------------------- recurring payments


@router.get("/reports/recurring")
def recurring_page(request: Request, show_all: str = Query("", alias="all")):
    today = date.today()
    _ = translator(config.language())
    with ledger_session(request) as s:
        series = recurring(s)
        coming = upcoming(s, today, 60, series)
    shown = [x for x in series if show_all or x.status(today) != STOPPED]
    monthly: dict[str, Decimal] = {}
    for x in shown:
        if x.direction < 0 and x.status(today) != STOPPED:
            monthly[x.commodity] = monthly.get(x.commodity, Decimal(0)) + x.typical * Decimal(30.44 / x.period.days)
    rows = [
        {"series": x, "status": x.status(today), "status_label": _(SERIES_STATUS[x.status(today)])} for x in shown
    ]
    this_month = month_start(today)
    return _page(
        request, "reports_recurring.html", "recurring", rows=rows, show_all=bool(show_all),
        monthly=" + ".join(f"{money(v, 0)} {c}" for c, v in sorted(monthly.items())),
        coming=[(item, *coming_text(item, _, this_month)) for item in coming], today=today,
        hidden=sum(1 for x in series if x.status(today) == STOPPED) if not show_all else 0,
    )
