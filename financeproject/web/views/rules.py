"""Categorization rules: the list in the order they are tried, adding, deleting, applying them to the
transactions without a category."""

from __future__ import annotations

from fastapi import APIRouter, Request
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from financeproject import config
from financeproject.errors import FinanceError
from financeproject.ingest.rules import (
    add_rule,
    apply_rules_to_uncategorized,
    delete_rule,
)
from financeproject.ledger.models import Rule
from financeproject.web.app import flash, ledger_session, redirect, render
from financeproject.web.forms import choices
from financeproject.web.i18n import translator

router = APIRouter()
TABS = (("review", "Review", "/categorize"), ("rules", "Rules", "/rules"))
TEXTS = tuple(label for _, label, _url in TABS)
"""Texts this module translates through a variable (the translation test reads them)."""


def _rules(s) -> list[Rule]:
    return list(s.scalars(select(Rule).options(selectinload(Rule.account)).order_by(Rule.priority.desc(), Rule.id)))


def _page(request: Request, status: int = 200, form: dict | None = None, error: str | None = None):
    with ledger_session(request) as s:
        return render(
            request, "rules.html", status, section="categorize", tabs=TABS, tab="rules", rules=_rules(s),
            options=choices(s), form=form or {}, error=error,
        )


@router.get("/rules")
def rules_page(request: Request):
    return _page(request)


@router.post("/rules")
async def add(request: Request):
    form = await request.form()
    data = {k: v for k, v in form.items() if isinstance(v, str)}
    _ = translator(config.language())
    try:
        priority = int(data.get("priority") or 0)
    except ValueError:
        return _page(request, 400, data, _("The priority is a whole number."))
    try:
        with ledger_session(request) as s:
            rule = add_rule(s, data.get("pattern", ""), data.get("account", ""), data.get("regex") == "1", priority)
            number, pattern, target = rule.id, rule.pattern, rule.account.path
    except FinanceError as exc:
        return _page(request, 400, data, str(exc))
    flash(request, "ok", _("Rule #{n}: '{pattern}' → {account}", n=number, pattern=pattern, account=target))
    return redirect("/rules")


@router.post("/rules/{rule_id}/delete")
def delete(request: Request, rule_id: int):
    _ = translator(config.language())
    with ledger_session(request) as s:
        delete_rule(s, rule_id)
    flash(request, "ok", _("Rule #{n} deleted.", n=rule_id))
    return redirect("/rules")


@router.post("/rules/apply")
def apply(request: Request):
    _ = translator(config.language())
    with ledger_session(request) as s:
        moved = len(apply_rules_to_uncategorized(s))
    flash(request, "ok", _.n("{n} transaction categorized by the rules.", "{n} transactions categorized by the rules.", moved))
    return redirect("/rules")
