"""Settings: display, prices, the ledger in use (yours or the demo), backups, the EVDS key, the shortcut, stopping."""

from __future__ import annotations

import sys
from datetime import date

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse
from sqlalchemy import func, select
from starlette.background import BackgroundTask

from financeproject import config
from financeproject.core.dates import add_months, month_start
from financeproject.core.money import set_number_format
from financeproject.ledger.db import backup, make_engine, session_scope
from financeproject.ledger.models import Price
from financeproject.web.app import (
    NUMBER_FORMATS,
    THEMES,
    LedgerFile,
    flash,
    is_demo,
    redirect,
    render,
)
from financeproject.web.devices import MONITOR
from financeproject.web.i18n import LANGUAGES, translator
from financeproject.web.views.devices import TABS, guests_on, seen_text

router = APIRouter()


def _backups() -> list:
    folder = config.backup_dir()
    if not folder.is_dir():
        return []
    files = sorted(folder.glob("*.sqlite"), key=lambda p: p.stat().st_mtime, reverse=True)
    return files[:30]


def _price_dates(request: Request) -> list[tuple[str, date]]:
    """The newest price of every source."""
    ledger = request.app.state.ledger
    if not ledger.exists:
        return []
    with session_scope(ledger.engine) as s:
        rows = s.execute(select(Price.source, func.max(Price.date)).group_by(Price.source).order_by(Price.source)).all()
    return [(source, last) for source, last in rows]


@router.get("/settings")
def settings_page(request: Request):
    state = request.app.state
    return render(
        request,
        "settings.html",
        section="settings",
        tabs=TABS,
        tab="general",
        seen_text=seen_text,
        guests=guests_on(),
        monitors=any(d.kind == MONITOR for d in state.devices.devices),
        languages=LANGUAGES,
        themes=THEMES,
        number_formats=NUMBER_FORMATS,
        price_dates=_price_dates(request),
        evds=config.evds_api_key() is not None,
        backups=_backups(),
        own_path=state.own_path,
        demo_path=state.demo_path,
        demo_exists=state.demo_path.exists(),
        job=state.jobs.current,
        data_dir=config.data_dir(),
        python=sys.executable,
    )


@router.post("/settings/display")
async def display(request: Request):
    form = await request.form()
    language = str(form.get("language", "en"))
    theme = str(form.get("theme", "auto"))
    numbers = str(form.get("number_format", "1,234.56"))
    if language in LANGUAGES:
        config.save_setting("display", "language", language)
    if theme in THEMES:
        config.save_setting("display", "theme", theme)
    if numbers in NUMBER_FORMATS:
        config.save_setting("display", "number_format", numbers)
        set_number_format(numbers)
    _ = translator(config.language())
    flash(request, "ok", _("Saved."))
    return redirect("/settings")


@router.post("/settings/evds")
async def evds(request: Request):
    form = await request.form()
    key = str(form.get("key", "")).strip()
    config.save_evds_api_key(key)
    _ = translator(config.language())
    flash(request, "ok", _("EVDS key saved.") if key else _("EVDS key removed."))
    return redirect("/settings#evds")


@router.post("/settings/backup")
def make_backup(request: Request):
    _ = translator(config.language())
    target = backup(request.app.state.ledger.path, config.backup_dir())
    flash(request, "ok", _("Backup made: {name}", name=target.name))
    return redirect("/settings#backups")


@router.get("/settings/backups/{name}")
def download_backup(request: Request, name: str):
    match = next((p for p in _backups() if p.name == name), None)
    if match is None:
        raise HTTPException(status_code=404, detail="No such backup.")
    return FileResponse(match, filename=match.name, media_type="application/vnd.sqlite3")


def _update_prices(path):
    def task(progress) -> str:
        from financeproject.prices.http import make_client
        from financeproject.prices.update import update_all, update_range

        today = date.today()
        engine = make_engine(path)
        try:
            with session_scope(engine) as s, make_client() as client:
                start, end = update_range(s, None, None, today)
                report = update_all(s, client, start, end, api_key=config.evds_api_key(), today=today, progress=progress)
        finally:
            engine.dispose()
        lines = [f"{report.added} new, {report.updated} updated records.", *report.messages, *report.warnings[:10]]
        return "\n".join(lines)

    return task


@router.post("/settings/prices")
def update_prices(request: Request):
    _ = translator(config.language())
    state = request.app.state
    if not state.ledger.exists:
        return redirect("/welcome")
    state.jobs.start(_("Updating prices"), _update_prices(state.ledger.path))
    return redirect("/settings#prices")


@router.get("/jobs/current")
def job_state(request: Request):
    job = request.app.state.jobs.current
    response = render(request, "_job.html", job=job)
    if request.headers.get("hx-request") and (job is None or not job.running):
        # the page asked while the task ran; now that it is over, the whole page may have changed
        # (a new demo ledger, new prices), so the browser loads it again
        response.headers["HX-Refresh"] = "true"
    return response


def switch_ledger(request: Request, path) -> None:
    state = request.app.state
    state.ledger.close()
    state.ledger = LedgerFile(path)


@router.post("/settings/ledger")
async def choose_ledger(request: Request):
    form = await request.form()
    state = request.app.state
    _ = translator(config.language())
    if str(form.get("which")) == "demo":
        if not state.demo_path.exists():
            raise HTTPException(status_code=400, detail="The demo ledger is not built yet.")
        switch_ledger(request, state.demo_path)
        flash(request, "info", _("You are looking at the demo household now."))
    else:
        switch_ledger(request, state.own_path)
        flash(request, "info", _("You are looking at your own ledger now."))
    return redirect("/")


@router.post("/settings/demo")
async def build_demo(request: Request):
    form = await request.form()
    state = request.app.state
    synthetic = str(form.get("prices")) == "offline"
    _ = translator(config.language())
    path = state.demo_path
    if is_demo(request):
        switch_ledger(request, state.own_path)

    def task(progress) -> str:
        from financeproject.demo import create_demo_ledger

        today = date.today()
        made = create_demo_ledger(
            path, add_months(month_start(today), -12), today, synthetic=synthetic, api_key=config.evds_api_key(),
            overwrite=True, progress=progress,
        )
        lines = [
            f"{made.summary.transactions} transactions, {made.summary.start} – {made.summary.end}, prices: {made.prices}.",
            "The interface now shows the demo household; switch back under Settings → Ledger.",
        ]
        if made.warning:
            lines.append(made.warning)
        return "\n".join(lines)

    def after() -> None:
        state.ledger.close()
        state.ledger = LedgerFile(path)

    state.jobs.start(_("Building the demo household"), task, after)
    return redirect("/settings#ledger")


@router.post("/settings/shortcut")
def make_shortcut(request: Request):
    from financeproject.web.shortcut import create_shortcut

    _ = translator(config.language())
    state = request.app.state
    db = None if state.own_path == config.default_db_path() else state.own_path
    made = create_shortcut(db)
    flash(request, "ok", _("Shortcut created: {where}", where=", ".join(str(p) for p in made)))
    return redirect("/settings#shortcut")


@router.post("/quit")
def quit_server(request: Request):
    server = request.app.state.server

    def stop() -> None:
        if server is not None:
            server.should_exit = True

    response = render(request, "stopped.html")
    response.background = BackgroundTask(stop)
    return response
