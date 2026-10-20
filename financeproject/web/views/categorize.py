"""Categories to review: the uncategorized transactions with a suggestion each, rules made from them."""

from __future__ import annotations

from fastapi import APIRouter, Request

from financeproject import config
from financeproject.errors import FinanceError
from financeproject.ingest.review import accept_confident, categorize, review
from financeproject.ingest.rules import add_rule, apply_rules_to_uncategorized
from financeproject.ledger.accounts import find_account
from financeproject.ledger.models import Transaction
from financeproject.web.app import flash, ledger_session, redirect, render
from financeproject.web.forms import choices
from financeproject.web.i18n import translator
from financeproject.web.views.rules import TABS

router = APIRouter()
SHOWN = 200


def suggested_pattern(tx: Transaction) -> str:
    """A rule pattern to start from: the payee, or the description's first words."""
    if tx.payee:
        return tx.payee
    return " ".join(tx.description.split()[:3])


@router.get("/categorize")
def review_page(request: Request):
    with ledger_session(request) as s:
        items = review(s)
        options = choices(s)
        confident = sum(1 for item in items if item.confident)
        return render(
            request,
            "categorize.html",
            section="categorize",
            items=items[:SHOWN],
            total=len(items),
            confident=confident,
            options=options,
            pattern=suggested_pattern,
            tabs=TABS,
            tab="review",
        )


@router.post("/categorize/accept")
def accept(request: Request):
    _ = translator(config.language())
    with ledger_session(request) as s:
        count = accept_confident(s)
    flash(request, "ok", _.n("{n} transaction categorized.", "{n} transactions categorized.", count))
    return redirect("/categorize")


@router.post("/categorize/rule")
async def make_rule(request: Request):
    form = await request.form()
    pattern = str(form.get("pattern", "")).strip()
    account = str(form.get("account", ""))
    _ = translator(config.language())
    if not pattern or not account:
        raise FinanceError("A rule needs a text to look for and a category.")
    with ledger_session(request) as s:
        rule = add_rule(s, pattern, account)
        moved = apply_rules_to_uncategorized(s)
        rule_id = rule.id
    flash(request, "ok", _.n(
            "Rule #{n} added; {count} transaction categorized by the rules.",
            "Rule #{n} added; {count} transactions categorized by the rules.",
            len(moved),
            n=rule_id,
            count=len(moved),
        ))
    return redirect("/categorize")


@router.post("/categorize/{transaction_id}")
async def choose(request: Request, transaction_id: int):
    form = await request.form()
    account = str(form.get("account", ""))
    with ledger_session(request) as s:
        tx = categorize(s, transaction_id, account)
        label = tx.payee or tx.description
        category = find_account(s, account).path
    if request.headers.get("hx-request"):
        return render(request, "_review_done.html", label=label, category=category, transaction_id=transaction_id)
    return redirect("/categorize")
