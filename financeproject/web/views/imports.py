"""Importing: upload a statement, card list or CSV file, review it, then write it; undo an import.

The uploaded file waits in the data directory's ``uploads`` folder (readable
only by the user) between the preview and the confirmation, and is deleted
after either. Leftovers older than a day are removed when the server starts.
"""

from __future__ import annotations

import re
import secrets
import shutil
import time
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request, UploadFile

from financeproject import config
from financeproject.errors import FinanceError
from financeproject.ingest.banks import STATEMENT_SUFFIXES, TEMPLATES, read_statement
from financeproject.ingest.csvfile import CsvFormat, import_csv, parse_column_map
from financeproject.ingest.importer import (
    HOW_FILE,
    HOW_INSTALLMENT,
    HOW_LOAN,
    HOW_PAYMENT,
    HOW_TRANSFER,
    HOW_UNCATEGORIZED,
    HOW_USER,
    import_statement,
    import_views,
    undo_import,
)
from financeproject.ledger.db import auto_backup
from financeproject.web.app import flash, ledger_session, percent, redirect, render
from financeproject.web.forms import choices
from financeproject.web.i18n import translator

router = APIRouter()
UPLOAD_ID = re.compile(r"^[0-9a-f]{16}$")
KEEP_SECONDS = 24 * 3600


def uploads_dir() -> Path:
    path = config.data_dir() / "uploads"
    path.mkdir(parents=True, exist_ok=True)
    path.chmod(0o700)
    return path


def clean_uploads(now: float | None = None) -> int:
    """Remove uploads older than a day; returns how many."""
    now = now or time.time()
    removed = 0
    for folder in uploads_dir().iterdir():
        if folder.is_dir() and now - folder.stat().st_mtime > KEEP_SECONDS:
            shutil.rmtree(folder, ignore_errors=True)
            removed += 1
    return removed


def _upload_path(upload_id: str) -> Path:
    if not UPLOAD_ID.match(upload_id or ""):
        raise HTTPException(status_code=400, detail="Unknown upload.")
    folder = uploads_dir() / upload_id
    files = [p for p in folder.iterdir() if p.is_file()] if folder.is_dir() else []
    if not files:
        raise HTTPException(status_code=400, detail="The uploaded file is gone; upload it again.")
    return files[0]


def _discard(upload_id: str) -> None:
    if UPLOAD_ID.match(upload_id or ""):
        shutil.rmtree(uploads_dir() / upload_id, ignore_errors=True)


def safe_name(filename: str | None) -> str:
    """The uploaded file's own name, kept to letters, digits and a few signs (its suffix tells the format)."""
    name = Path((filename or "").replace("\\", "/")).name
    name = re.sub(r"[^\w.() -]+", "_", name).strip(" .")
    if len(name) > 120:
        stem, dot, suffix = name.rpartition(".")
        name = (stem[: 110] + dot + suffix[:9]) if dot else name[:120]
    return name or "statement"


async def _save(upload: UploadFile) -> tuple[str, Path]:
    name = safe_name(upload.filename)
    upload_id = secrets.token_hex(8)
    folder = uploads_dir() / upload_id
    folder.mkdir(mode=0o700)
    target = folder / name
    with target.open("wb") as fh:
        while chunk := await upload.read(1 << 20):
            fh.write(chunk)
    target.chmod(0o600)
    return upload_id, target


def _options(data: dict) -> dict:
    """The import options of a form, as strings (they travel from the preview to the confirmation)."""
    keys = ("account", "payments_from", "opening", "learn", "bank", "delimiter", "decimal", "date_format", "columns", "skip", "invert_sign", "encoding")
    return {k: str(data.get(k, "") or "") for k in keys}


def _run(s, path: Path, options: dict, *, dry_run: bool, overrides: dict[int, str] | None = None):
    """Import ``path`` with the options; returns the result and the statement (``None`` for CSV)."""
    use_model = options["learn"] != "0"
    if path.suffix.lower() in STATEMENT_SUFFIXES or (options["bank"] and options["bank"] != "auto"):
        bank = options["bank"] if options["bank"] in TEMPLATES else None
        statement = read_statement(path, bank)
        if not options["account"]:
            raise FinanceError("Choose the account the statement belongs to.")
        result = import_statement(
            s,
            statement,
            path,
            options["account"],
            dry_run=dry_run,
            add_opening=options["opening"] == "1",
            use_model=use_model,
            payments_from=options["payments_from"] or None,
            overrides=overrides,
        )
        return result, statement
    fmt = CsvFormat(
        delimiter={"": None, "auto": None, "tab": "\t"}.get(options["delimiter"], options["delimiter"]),
        decimal_sep=options["decimal"] if options["decimal"] in (",", ".") else None,
        date_format=options["date_format"] or None,
        encoding=options["encoding"] or None,
        columns=parse_column_map(options["columns"]) if options["columns"] else {},
        skip_rows=int(options["skip"]) if options["skip"].isdigit() else 0,
        invert_sign=options["invert_sign"] == "1",
    )
    result = import_csv(
        s, path, fmt, account=options["account"] or None, dry_run=dry_run, use_model=use_model, overrides=overrides
    )
    return result, None


def how_label(row, _) -> tuple[str, str]:
    """The preview's "how" column as a translated label and a badge tone."""
    how = row.how
    main, _sep, extra = how.partition("; ")
    if row.duplicate:
        if main.startswith(HOW_INSTALLMENT):
            return _("counted with its purchase"), "accent"
        if main.startswith(HOW_TRANSFER):
            return _("already in as #{n}", n=main.rsplit("#", 1)[-1]), "accent"
        return _("already in"), ""
    words = main.split()
    if main == HOW_UNCATEGORIZED:
        label, tone = _("no category"), "warning"
    elif main == HOW_USER:
        label, tone = _("chosen by you"), "accent"
    elif main == HOW_FILE:
        label, tone = _("from the file"), ""
    elif main == HOW_PAYMENT:
        label, tone = _("card payment"), "accent"
    elif main.startswith("rule #"):
        label, tone = _("rule #{n}", n=main[len("rule #"):]), ""
    elif words[:1] == ["model"] and len(words) == 2:
        label, tone = _("learned, {p}", p=percent(float(words[1]))), ""
    elif main.startswith(HOW_LOAN):
        label, tone = _("loan installment"), "accent"
    else:
        label, tone = main, ""
    if extra:
        count = extra.split()[0]
        label += " · " + _("{n} installments", n=count)
    return label, tone


def _editable(row) -> bool:
    """Rows whose category can be chosen in the preview."""
    return (
        not row.duplicate
        and not row.how.startswith((HOW_LOAN, HOW_TRANSFER))
        and row.how != HOW_PAYMENT
        and row.counter_account.startswith(("Expenses:", "Income:"))
    )


@router.get("/import")
def import_form(request: Request):
    with ledger_session(request) as s:
        options = choices(s)
    return render(request, "import.html", section="import", options=options, data={}, error=None)


@router.post("/import/preview")
async def preview(request: Request):
    form = await request.form()
    upload = form.get("file")
    data = {k: v for k, v in form.items() if isinstance(v, str)}
    options = _options(data)
    if upload is None or isinstance(upload, str) or not upload.filename:
        with ledger_session(request) as s:
            choices_ = choices(s)
        _ = translator(config.language())
        return render(request, "import.html", 400, section="import", options=choices_, data=data, error=_("Choose a file."))
    clean_uploads()
    upload_id, path = await _save(upload)
    try:
        with ledger_session(request) as s:
            result, statement = _run(s, path, options, dry_run=True)
            s.rollback()
            choices_ = choices(s)
    except (FinanceError, ValueError) as exc:
        _discard(upload_id)
        with ledger_session(request) as s:
            choices_ = choices(s)
        return render(request, "import.html", 400, section="import", options=choices_, data=data, error=str(exc))
    return render(
        request,
        "import_preview.html",
        section="import",
        result=result,
        statement=statement,
        upload_id=upload_id,
        file_name=path.name,
        options=options,
        choices=choices_,
        editable=_editable,
        how=lambda row: how_label(row, translator(config.language())),
    )


@router.post("/import/confirm")
async def confirm(request: Request):
    form = await request.form()
    data = {k: v for k, v in form.items() if isinstance(v, str)}
    upload_id = data.get("upload_id", "")
    path = _upload_path(upload_id)
    options = _options(data)
    overrides = {}
    for key, value in data.items():
        if key.startswith("cat_") and key[4:].isdigit() and value and value != data.get(f"was_{key[4:]}"):
            overrides[int(key[4:])] = value
    auto_backup(request.app.state.ledger.path, config.backup_dir())
    try:
        with ledger_session(request) as s:
            result, statement = _run(s, path, options, dry_run=False, overrides=overrides)
            if result.errors:
                s.rollback()
    except (FinanceError, ValueError) as exc:
        _discard(upload_id)
        with ledger_session(request) as s:
            choices_ = choices(s)
        return render(request, "import.html", 400, section="import", options=choices_, data=data, error=str(exc))
    _discard(upload_id)
    return render(
        request, "import_result.html", section="import", result=result, statement=statement, file_name=path.name,
        upload_id=upload_id, options=options,
    )


@router.post("/import/cancel")
async def cancel(request: Request):
    form = await request.form()
    _discard(str(form.get("upload_id", "")))
    return redirect("/import")


@router.get("/imports")
def import_list(request: Request):
    with ledger_session(request) as s:
        views = import_views(s, 200)
    return render(request, "imports.html", section="import", views=views)


@router.post("/imports/{import_id}/undo")
def undo(request: Request, import_id: int):
    _ = translator(config.language())
    auto_backup(request.app.state.ledger.path, config.backup_dir())
    with ledger_session(request) as s:
        count = undo_import(s, import_id)
    flash(request, "ok", _.n(
            "Import #{n} undone: {count} transaction deleted. A backup was made first.",
            "Import #{n} undone: {count} transactions deleted. A backup was made first.",
            count,
            n=import_id,
            count=count,
        ))
    return redirect("/imports")

