"""The change history: edits, recategorizations, deletions, imports and undone imports."""

from __future__ import annotations

from fastapi import APIRouter, Request

from financeproject.ledger.history import recent_changes
from financeproject.web.app import ledger_session, render

router = APIRouter()
PAGE = 100


@router.get("/history")
def history_page(request: Request, page: int = 1):
    page = max(1, page)
    with ledger_session(request) as s:
        changes = recent_changes(s, PAGE + 1, (page - 1) * PAGE)
    return render(
        request, "history.html", section="history", changes=changes[:PAGE], page=page, more=len(changes) > PAGE
    )
