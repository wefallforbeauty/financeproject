"""Investments: returns in four units, risk with the backtest of the value at risk, and concentration with the
deposit insurance check, for every investment account or the accounts chosen."""

from __future__ import annotations

from datetime import date
from typing import Annotated

from fastapi import APIRouter, Query, Request
from sqlalchemy import select

from financeproject import config
from financeproject.core.kinds import ASSET
from financeproject.errors import FinanceError
from financeproject.ledger.models import Account
from financeproject.reports.investments import (
    DEFAULT_KINDS,
    GOLD_UNIT,
    NO_INSTITUTION,
    REAL,
    UNITS,
    concentration_report,
    returns_report,
    risk_report,
)
from financeproject.web.app import ledger_session, money, percent, render
from financeproject.web.forms import amount_value, bounded, day_value, optional
from financeproject.web.i18n import translator

router = APIRouter()

TABS = (
    ("returns", "Returns", "/investments"),
    ("risk", "Risk", "/investments/risk"),
    ("concentration", "Concentration", "/investments/concentration"),
)
UNIT_LABELS = {"TRY": "TRY", REAL: "Real TRY", "USD": "USD", GOLD_UNIT: "Gold (g)"}
FLAGS = ("{share} in one holding: {name}", "{share} in one institution: {name}", "{share} in one currency: {name}")
VERDICTS = ("too many exceptions", "too few exceptions", "exceptions cluster", "fits")
TEXTS = (*(label for _, label, _url in TABS), *UNIT_LABELS.values(), *FLAGS, *VERDICTS)
"""Texts this module translates through a variable (the translation test reads them)."""


def _page(request: Request, template: str, tab: str, status: int = 200, **context):
    return render(request, template, status, section="investments", tabs=TABS, tab=tab, **context)


def _assets(s) -> list[Account]:
    return list(s.scalars(select(Account).where(Account.type == ASSET, Account.closed_on.is_(None)).order_by(Account.path)))


def _days(start: str, end: str) -> tuple[date | None, date | None]:
    first = day_value(start, "first day") if optional(start) else None
    last = day_value(end, "last day") if optional(end) else None
    if first and last and first >= last:
        raise FinanceError("The first day is not before the last one.")
    return first, last


def _common(s, account: list[str]) -> dict:
    return {"assets": _assets(s), "chosen": set(account), "default_kinds": DEFAULT_KINDS}


Accounts = Annotated[list[str] | None, Query()]
"""The accounts chosen (a repeated ``account`` parameter); none means every investment account."""


@router.get("/investments")
def returns_page(request: Request, start: str = "", end: str = "", account: Accounts = None):
    account = account or []
    first, last = _days(start, end)
    with ledger_session(request) as s:
        common = _common(s, account)
        try:
            report = returns_report(s, first, last, account or None)
        except FinanceError as exc:
            return _page(request, "investments_returns.html", "returns", report=None, error=str(exc), form={"start": start, "end": end}, **common)
        shown = {r.name for r in report.units}
        _ = translator(config.language())
        return _page(
            request, "investments_returns.html", "returns", report=report, error=None, unit_labels=UNIT_LABELS,
            missing=[_(UNIT_LABELS[u]) for u in UNITS if u not in shown], form={"start": start, "end": end}, **common,
        )


@router.get("/investments/risk")
def risk_page(request: Request, start: str = "", end: str = "", window: int | None = None, account: Accounts = None):
    account = account or []
    first, last = _days(start, end)
    window_ = bounded(window, 250, 20, 1000)
    with ledger_session(request) as s:
        common = _common(s, account)
        try:
            report = risk_report(s, first, last, account or None, window=window_)
        except FinanceError as exc:
            return _page(request, "investments_risk.html", "risk", report=None, error=str(exc), form={"start": start, "end": end}, window=window_, **common)
        return _page(
            request, "investments_risk.html", "risk", report=report, error=None, form={"start": start, "end": end},
            window=window_, enough=len(report.returns) >= 20, verdict=verdict,
            var_rows=list(zip(report.historical, report.normal, strict=False)), matrix=correlation_matrix(report.correlations),
            **common,
        )


def correlation_matrix(pairs: dict[tuple[str, str], float | None]) -> list[tuple[str, list[float | None]]]:
    """The pairs' correlations as rows of a symmetric table (1 on the diagonal)."""
    names = sorted({name for pair in pairs for name in pair})

    def value(a: str, b: str) -> float | None:
        return 1.0 if a == b else pairs.get((a, b), pairs.get((b, a)))

    return [(a, [value(a, b) for b in names]) for a in names]


def verdict(b) -> str:
    """The backtest's verdict (English; translated in the template)."""
    if b.kupiec_p < 0.05:
        return "too many exceptions" if b.exceptions > b.expected else "too few exceptions"
    if b.independence_p is not None and b.independence_p < 0.05:
        return "exceptions cluster"
    return "fits"


def concentration_flags(report, _) -> list[str]:
    """The report's warnings in the interface's language (``ConcentrationReport.flags`` words them in English)."""
    out = []
    investments = [sh for sh in report.by_holding if sh.name not in report.currencies]
    for text, shares in ((FLAGS[0], investments), (FLAGS[1], report.by_institution), (FLAGS[2], report.by_currency)):
        named = [sh for sh in shares if sh.name != NO_INSTITUTION]
        if named and named[0].share > report.threshold:
            out.append(_(text, share=percent(named[0].share), name=named[0].name))
    for d in report.deposits:
        if d.over > 0:
            out.append(_(
                "{owner} at {bank}: deposits of {value} TRY are {over} over the insured {limit}",
                owner=d.owner, bank=d.institution, value=money(d.value), over=money(d.over), limit=money(d.limit, 0),
            ))
    return out


@router.get("/investments/concentration")
def concentration_page(
    request: Request, on: str = Query("", alias="date"), threshold: int | None = None, insured_limit: str = "",
    account: Accounts = None,
):
    account = account or []
    at = day_value(on) if optional(on) else date.today()
    threshold_ = bounded(threshold, 50, 1, 99)
    limit = amount_value(insured_limit, "insured limit") if optional(insured_limit) else None
    _ = translator(config.language())
    with ledger_session(request) as s:
        common = _common(s, account)
        report = concentration_report(s, at, account or None, threshold=threshold_ / 100, limit=limit)
        return _page(
            request, "investments_concentration.html", "concentration", report=report, at=at, flags=concentration_flags(report, _),
            threshold=threshold_, form={"date": on, "insured_limit": insured_limit}, given_limit=limit is not None,
            no_institution=NO_INSTITUTION, **common,
        )
