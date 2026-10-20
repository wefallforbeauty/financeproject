"""Pairing a phone or a wall monitor on the home network, and the certificate a phone may install.

The computer shows a QR code with a one-time code (Settings → Phones and monitor). The device opens it,
gives the household's password and a name, and gets the cookie that lets it in from then on. The cookie is
``SameSite=Lax``, so that opening the interface from a home-screen icon or another app carries it;
changes still need the CSRF token and this server's origin."""

from __future__ import annotations

from fastapi import APIRouter, Request
from starlette.concurrency import run_in_threadpool

from financeproject import config
from financeproject.web.app import Flash, redirect, render
from financeproject.web.devices import MONITOR, PAIRING_MESSAGES, PairingError
from financeproject.web.i18n import translator
from financeproject.web.security import DEVICE, DEVICE_COOKIE, LOCAL, session_of
from financeproject.web.views.devices import certificate_response

router = APIRouter()
COOKIE_DAYS = 400
"""How long a paired device's cookie lasts (the most browsers allow); removing the device ends it sooner."""
TEXTS = PAIRING_MESSAGES
"""Texts this module translates through a variable (the translation test reads them)."""


def _start_page(device) -> str:
    return "/monitor" if device.kind == MONITOR else "/"


def _form(request: Request, code: str, status: int = 200, error: str | None = None, name: str = ""):
    kind = request.app.state.pairings.kind_of(code) if code else None
    return render(request, "pair.html", status, code=code, kind=kind, error=error, name=name)


@router.get("/pair")
def pair_page(request: Request, code: str = ""):
    session = session_of(request)
    if session.kind == LOCAL:
        return redirect("/settings/devices#pair")
    if session.kind == DEVICE:
        return redirect(_start_page(session.device))
    return _form(request, code)


@router.post("/pair")
async def pair(request: Request):
    session = session_of(request)
    if session.kind != "visitor":
        return redirect("/")
    form = await request.form()
    code, name, password = str(form.get("code", "")), str(form.get("name", "")), str(form.get("password", ""))
    _ = translator(config.language())
    state = request.app.state
    kind = state.pairings.kind_of(code)
    default = _("Wall monitor") if kind == MONITOR else _("Phone")
    try:
        # scrypt takes a fraction of a second: in a worker thread, so the server keeps answering meanwhile
        device, token = await run_in_threadpool(state.pairings.redeem, code, password, name, state.devices, default)
    except PairingError as exc:
        return _form(request, code, 400, _(str(exc)), name)
    response = redirect(_start_page(device))
    response.set_cookie(DEVICE_COOKIE, token, max_age=COOKIE_DAYS * 86400, httponly=True, secure=True, samesite="lax", path="/")
    # the welcome waits under the device's own key, which its next request has
    state.flashes.setdefault(device.id, []).append(Flash("ok", _("{name} is paired. Welcome!", name=device.name)))
    return response


@router.get("/ca.crt")
def certificate():
    return certificate_response()
