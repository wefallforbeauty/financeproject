"""The first page when the ledger does not exist yet: create it, or look at the demo household first."""

from __future__ import annotations

from fastapi import APIRouter, Request

from financeproject import config
from financeproject.ledger.db import create_ledger
from financeproject.web.app import LedgerFile, flash, redirect, render
from financeproject.web.i18n import translator

router = APIRouter()


@router.get("/welcome")
def welcome(request: Request):
    state = request.app.state
    if state.ledger.exists:
        return redirect("/")
    return render(
        request,
        "welcome.html",
        section="welcome",
        own_path=state.own_path,
        demo_exists=state.demo_path.exists(),
        job=state.jobs.current,
    )


@router.post("/welcome/create")
def create(request: Request):
    state = request.app.state
    create_ledger(state.own_path)
    state.ledger.close()
    state.ledger = LedgerFile(state.own_path)
    _ = translator(config.language())
    flash(request, "ok", _("Your ledger is ready. Start by adding your accounts."))
    return redirect("/accounts")
