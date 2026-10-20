"""The web interface: a FastAPI application that renders pages from the ledger.

All the bookkeeping lives in the library; the views read and change the ledger
through it and render Jinja templates. Pages work without JavaScript; HTMX
updates parts of a page (filters, the category review, task progress), and
ECharts draws the net worth chart. Both are bundled, so nothing is loaded
from the internet.

The server works on one ledger at a time: the user's own, or the demo
household's, and can switch between them.
"""

from __future__ import annotations

import hmac
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from importlib import resources
from pathlib import Path

from fastapi import Depends, FastAPI, Request
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    PlainTextResponse,
    RedirectResponse,
)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.engine import Engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session
from starlette.exceptions import HTTPException

from financeproject import VERSION_LABEL, __version__, config
from financeproject.core.money import (
    DEFAULT_NUMBER_FORMAT,
    format_amount,
    set_number_format,
)
from financeproject.errors import FinanceError
from financeproject.ledger.db import (
    is_initialised,
    make_engine,
    open_ledger,
    session_scope,
)
from financeproject.ledger.history import (
    DELETE,
    EDIT,
    IMPORT,
    RECATEGORIZE,
    UNDO_IMPORT,
)
from financeproject.web.i18n import LANGUAGES, translator
from financeproject.web.jobs import Jobs
from financeproject.web.security import Guard, check_csrf, cookie_name, csrf_for

WEB = resources.files("financeproject.web")
templates = Jinja2Templates(directory=str(WEB / "templates"))
templates.env.filters["short"] = lambda path: short(path)
THEMES = ("auto", "light", "dark")
ACTION_LABELS = {
    EDIT: "Edited",
    RECATEGORIZE: "Recategorized",
    DELETE: "Deleted",
    IMPORT: "Imported",
    UNDO_IMPORT: "Import undone",
}
"""How the change history names each kind of change."""
NUMBER_FORMATS = ("1,234.56", "1.234,56")


class NeedsLedger(Exception):
    """The current ledger file does not exist yet."""


class LedgerFile:
    """The ledger the interface works on, with its database engine."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._engine: Engine | None = None

    @property
    def exists(self) -> bool:
        return self.path.exists()

    @property
    def engine(self) -> Engine:
        if not self.exists:
            raise NeedsLedger()
        if self._engine is None:
            engine = make_engine(self.path)
            if not is_initialised(engine):
                engine.dispose()
                raise FinanceError(f"{self.path} is not a financeproject ledger.")
            open_ledger(engine)
            self._engine = engine
        return self._engine

    def close(self) -> None:
        if self._engine is not None:
            self._engine.dispose()
            self._engine = None


@dataclass
class Flash:
    kind: str
    """``ok``, ``info``, ``warning`` or ``error``."""
    text: str


@contextmanager
def ledger_session(request: Request) -> Iterator[Session]:
    """A session on the current ledger; commits when the block ends without an error."""
    with session_scope(request.app.state.ledger.engine) as session:
        yield session


def flash(request: Request, kind: str, text: str) -> None:
    request.app.state.flashes.append(Flash(kind, text))


def is_demo(request: Request) -> bool:
    state = request.app.state
    return state.ledger.path.resolve() == state.demo_path.resolve()


def number_format() -> str:
    return config.number_format() or DEFAULT_NUMBER_FORMAT


MINUS = "\u2212"


def money(value: Decimal | float | None, decimals: int = 2) -> str:
    """An amount for reading, in the number format, with a real minus sign."""
    if value is None:
        return "—"
    text = format_amount(value, decimals)
    return MINUS + text[1:] if text.startswith("-") else text


def percent(value: Decimal | float | None, decimals: int = 0, sign: bool = False) -> str:
    """A share as a percentage: ``0.063`` → ``6.3%``, or ``%6,3`` in Turkish (the sign goes first)."""
    if value is None:
        return "—"
    number = Decimal(str(value)) * 100
    body = format_amount(abs(number), decimals)
    body = f"%{body}" if config.language() == "tr" else f"{body}%"
    if number < 0 and body.strip("%0.,"):
        return MINUS + body
    return ("+" + body) if sign and number > 0 else body


def signed(value: Decimal | float | None, decimals: int = 2) -> str:
    """An amount with its sign: ``+1,234`` or ``−56``."""
    if value is None:
        return "—"
    return ("+" if value > 0 else "") + money(value, decimals)


def price_info(book, code: str, at: date) -> dict | None:
    """The price of ``code`` in TRY on ``at``: its value, unit, date and sources (``None`` if there is none)."""
    from financeproject.reports.context import SOURCE_LABELS

    found = book.resolve(code, at)
    if found is None:
        return None
    return {
        "value": found.value,
        "decimals": 4 if code == "USD" else 2,
        "unit": "TRY/g" if code == "XAU_G" else "TRY",
        "as_of": found.as_of,
        "sources": list(dict.fromkeys(SOURCE_LABELS.get(q.source, q.source) for q in found.path)),
    }


def short(path: str) -> str:
    """An account without its root: ``Expenses:Food:Groceries`` → ``Food:Groceries``."""
    return path.split(":", 1)[1] if ":" in path else path


def route(row) -> list[str]:
    """The asset and liability accounts of a list row, money out first (``from → to``)."""
    names = [short(path) for path in row.accounts]
    if len(names) == 2 and row.amount > 0:
        names.reverse()
    return names


def row_amount(row) -> str:
    """A list row's amount: signed for income and spending, plain for a transfer."""
    unit = "" if row.commodity == "TRY" else f" {row.commodity}"
    if not row.categories:
        return money(abs(row.amount)) + unit
    return ("+" if row.amount > 0 else "") + money(row.amount) + unit


def local_time(moment: datetime | None) -> str:
    """A moment in this computer's time zone (the ledger keeps times in UTC)."""
    if moment is None:
        return ""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC).astimezone()
    return moment.strftime("%Y-%m-%d %H:%M")


def _uncategorized(state) -> int:
    from financeproject.ledger.queries import uncategorized_count

    if not state.ledger.exists:
        return 0
    try:
        with session_scope(state.ledger.engine) as s:
            return uncategorized_count(s)
    except FinanceError:
        return 0


def render(request: Request, template: str, status: int = 200, **context) -> HTMLResponse:
    state = request.app.state
    language = config.language()
    _ = translator(language)
    flashes, state.flashes = state.flashes, []
    base = {
        "_": _,
        "action_label": lambda action: _(ACTION_LABELS.get(action, action)),
        "lang": language,
        "languages": LANGUAGES,
        "theme": config.theme(),
        "number_format": number_format(),
        "csrf": state.csrf,
        "flashes": flashes,
        "version": VERSION_LABEL,
        "package_version": __version__,
        "ledger_path": state.ledger.path,
        "demo": is_demo(request),
        "money": money,
        "percent": percent,
        "signed": signed,
        "short": short,
        "route": route,
        "row_amount": row_amount,
        "local_time": local_time,
        "today": date.today(),
        "nav_uncategorized": _uncategorized(state),
        "section": "",
    }
    return templates.TemplateResponse(request, template, {**base, **context}, status_code=status)


def redirect(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


def create_app(
    db_path: Path,
    *,
    token: str,
    port: int | None = None,
    demo_path: Path | None = None,
    extra_hosts: set[str] | None = None,
) -> FastAPI:
    """The application for one session of ``finance ui``.

    ``db_path`` is the user's ledger, ``demo_path`` the demo household's.
    ``extra_hosts`` admits other ``Host`` headers (tests).
    """
    from financeproject.web.views import (
        accounts,
        categorize,
        dashboard,
        debts,
        history,
        imports,
        investments,
        reports,
        rules,
        settings,
        transactions,
        welcome,
    )

    set_number_format(number_format())
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, dependencies=[Depends(check_csrf)])
    app.state.own_path = Path(db_path)
    app.state.demo_path = Path(demo_path or config.demo_db_path())
    app.state.ledger = LedgerFile(db_path)
    app.state.token = token
    app.state.csrf = csrf_for(token)
    app.state.port = port
    app.state.cookie = cookie_name(port)
    app.state.jobs = Jobs()
    app.state.flashes = []
    app.state.server = None
    app.add_middleware(Guard, token=token, port=port, extra_hosts=extra_hosts)
    app.mount("/static", StaticFiles(directory=str(WEB / "static")), name="static")

    @app.get("/login")
    def login(request: Request, token: str = "", next: str = "/"):
        if not hmac.compare_digest(token, request.app.state.token):
            return HTMLResponse("Wrong or expired link; open the interface with: finance ui", status_code=401)
        local = next.startswith("/") and not next.startswith("//") and "\\" not in next
        target = next if local else "/"
        response = redirect(target)
        response.set_cookie(request.app.state.cookie, token, httponly=True, samesite="strict", path="/")
        return response

    @app.get("/ping")
    def ping():
        return PlainTextResponse("financeproject")

    @app.get("/favicon.ico")
    def favicon():
        return FileResponse(str(WEB / "static" / "img" / "icon.svg"), media_type="image/svg+xml")

    @app.exception_handler(NeedsLedger)
    async def needs_ledger(request: Request, exc: NeedsLedger):
        return redirect("/welcome")

    @app.exception_handler(HTTPException)  # Starlette's: also the 404 of an address no page has
    async def http_error(request: Request, exc: HTTPException):
        _ = translator(config.language())
        titles = {403: _("Not allowed"), 404: _("Not found")}
        return render(request, "error.html", exc.status_code, title=titles.get(exc.status_code, _("Error")), message=str(exc.detail))

    @app.exception_handler(FinanceError)
    async def finance_error(request: Request, exc: FinanceError):
        _ = translator(config.language())
        return render(request, "error.html", 400, title=_("That did not work"), message=str(exc))

    @app.exception_handler(OperationalError)
    async def database_error(request: Request, exc: OperationalError):
        if "locked" not in str(exc.orig):
            raise exc
        _ = translator(config.language())
        message = _("The ledger is busy: a task in the background is writing to it. Try again in a moment.")
        return render(request, "error.html", 503, title=_("That did not work"), message=message)

    for module in (dashboard, transactions, imports, categorize, rules, accounts, reports, debts, investments, settings, history, welcome):
        app.include_router(module.router)
    return app
