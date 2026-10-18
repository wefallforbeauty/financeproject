"""The change history: what was edited, recategorized, deleted, imported or undone, and when.

Every change keeps a snapshot of the transaction before and after it, as
JSON, so that a deleted transaction can still be read. Imports are recorded
once per file, not per line.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from financeproject.ledger.models import Change, Transaction

EDIT, RECATEGORIZE, DELETE, IMPORT, UNDO_IMPORT = "edit", "recategorize", "delete", "import", "undo import"
ACTIONS = (EDIT, RECATEGORIZE, DELETE, IMPORT, UNDO_IMPORT)


def snapshot(tx: Transaction) -> dict:
    """The transaction as plain data."""
    return {
        "id": tx.id,
        "date": tx.date.isoformat(),
        "description": tx.description,
        "payee": tx.payee,
        "note": tx.note,
        "source": tx.source,
        "legs": [
            {"account": p.account.path, "commodity": p.commodity_code, "amount": format(p.amount, "f")}
            for p in tx.postings
        ],
    }


def log_change(
    session: Session,
    action: str,
    summary: str,
    *,
    transaction_id: int | None = None,
    import_id: int | None = None,
    before: dict | None = None,
    after: dict | None = None,
) -> Change:
    if action not in ACTIONS:
        raise ValueError(f"Unknown action {action!r}")
    change = Change(
        action=action,
        transaction_id=transaction_id,
        import_id=import_id,
        summary=summary[:300],
        before=json.dumps(before, ensure_ascii=False) if before is not None else None,
        after=json.dumps(after, ensure_ascii=False) if after is not None else None,
    )
    session.add(change)
    session.flush()
    return change


@dataclass(frozen=True)
class ChangeView:
    id: int
    at: datetime
    """Local time."""
    action: str
    summary: str
    transaction_id: int | None
    import_id: int | None
    before: dict | None
    after: dict | None


def _view(change: Change) -> ChangeView:
    at = change.at.replace(tzinfo=UTC).astimezone() if change.at else datetime.now().astimezone()
    return ChangeView(
        change.id,
        at,
        change.action,
        change.summary,
        change.transaction_id,
        change.import_id,
        json.loads(change.before) if change.before else None,
        json.loads(change.after) if change.after else None,
    )


def history_of(session: Session, transaction_id: int) -> list[ChangeView]:
    """The changes of one transaction, newest first."""
    changes = session.scalars(
        select(Change).where(Change.transaction_id == transaction_id).order_by(Change.id.desc())
    )
    return [_view(c) for c in changes]


def recent_changes(session: Session, limit: int = 100, offset: int = 0) -> list[ChangeView]:
    """All changes, newest first."""
    changes = session.scalars(select(Change).order_by(Change.id.desc()).limit(limit).offset(offset))
    return [_view(c) for c in changes]
