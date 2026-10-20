"""Settings → Phones and monitor (on this computer only): the household's password, opening the interface to
the home network, pairing phones and wall monitors and removing them, the certificate phones may install,
and the monitor's guest mode."""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response
from starlette.concurrency import run_in_threadpool

from financeproject import config
from financeproject.errors import FinanceError
from financeproject.web import lan, tls
from financeproject.web.app import flash, local_time, redirect, render
from financeproject.web.devices import KINDS, MIN_PASSWORD, MONITOR, PHONE, show_code
from financeproject.web.i18n import translator
from financeproject.web.server import close_lan, lan_port, open_lan

router = APIRouter()

TABS = (("general", "General", "/settings"), ("devices", "Phones and monitor", "/settings/devices"))
KIND_LABELS = {PHONE: "Phone", MONITOR: "Wall monitor"}
TEXTS = (*(label for _, label, _url in TABS), *KIND_LABELS.values())
"""Texts this module translates through a variable (the translation test reads them)."""


def qr_svg(text: str) -> str:
    """An inline SVG QR code of ``text``, dark on white whatever the theme (scanners need the contrast)."""
    import segno

    code = segno.make(text, error="m")
    return code.svg_inline(scale=4, border=3, dark="#0b0b0b", light="#ffffff", omitsize=True, svgclass="qr", lineclass=None)


def guests_on() -> bool:
    return config.get_setting("monitor", "guests") == "on"


def seen_text(moment: str | None) -> str:
    if not moment:
        return "—"
    return local_time(datetime.fromisoformat(moment))


def _pairing_context(request: Request) -> dict:
    state = request.app.state
    pairing = state.pairings.current
    home = state.lan
    context = {"pairing": pairing, "home": home, "qr": None, "pair_url": None, "code": None, "seconds": 0}
    if pairing is not None and pairing.device is None and home is not None:
        url = f"{home.url}/pair?code={pairing.code}"
        context.update(qr=qr_svg(url), pair_url=url, code=show_code(pairing.code), seconds=state.pairings.seconds_left(pairing))
    return context


@router.get("/settings/devices")
def devices_page(request: Request):
    state = request.app.state
    return render(
        request, "settings_devices.html", section="settings", tabs=TABS, tab="devices",
        has_password=state.devices.has_password, devices=state.devices.devices, kinds=KIND_LABELS,
        addresses=[str(a) for a in lan.addresses()], chosen=config.get_setting("network", "address"),
        open_at_start=config.get_setting("network", "open") == "on", fingerprint=tls.fingerprint(),
        server_expiry=tls.server_expiry(), guests=guests_on(), seen_text=seen_text, lan_port=lan_port(),
        **_pairing_context(request),
    )


@router.get("/settings/devices/pairing")
def pairing_part(request: Request):
    """The pairing box, which the page asks for again every few seconds while a code is waiting."""
    context = _pairing_context(request)
    response = render(request, "_pairing.html", kinds=KIND_LABELS, **context)
    if request.headers.get("hx-request") and context["qr"] is None:
        # the code was used or ran out: the list of devices on the page is out of date
        response.headers["HX-Refresh"] = "true"
    return response


@router.post("/settings/devices/password")
async def set_password(request: Request):
    form = await request.form()
    _ = translator(config.language())
    password, repeat = str(form.get("password", "")), str(form.get("repeat", ""))
    if password != repeat:
        flash(request, "error", _("The two passwords differ."))
        return redirect("/settings/devices#password")
    try:
        await run_in_threadpool(request.app.state.devices.set_password, password)
    except FinanceError:
        flash(request, "error", _("The password needs at least {n} characters.", n=MIN_PASSWORD))
        return redirect("/settings/devices#password")
    flash(request, "ok", _("Password saved. Paired devices stay paired."))
    return redirect("/settings/devices")


@router.post("/settings/devices/open")
async def open_home(request: Request):
    form = await request.form()
    _ = translator(config.language())
    address = str(form.get("address", "")) or None
    if address:
        config.save_setting("network", "address", address)
    try:
        # it waits until the new server answers: in a worker thread, so this one keeps answering meanwhile
        home = await run_in_threadpool(open_lan, request.app, address=address)
    except FinanceError as exc:
        flash(request, "error", str(exc))
    else:
        flash(request, "ok", _("Open to the home network at {url}.", url=home.url))
    return redirect("/settings/devices")


@router.post("/settings/devices/close")
def close_home(request: Request):
    _ = translator(config.language())
    close_lan(request.app)
    request.app.state.pairings.cancel()
    flash(request, "ok", _("Closed to the home network: only this computer can open the interface."))
    return redirect("/settings/devices")


@router.post("/settings/devices/start")
async def open_at_start(request: Request):
    form = await request.form()
    _ = translator(config.language())
    on = str(form.get("open", "")) == "on"
    config.save_setting("network", "open", "on" if on else "off")
    flash(request, "ok", _("Saved."))
    return redirect("/settings/devices#network")


@router.post("/settings/devices/pair")
async def pair(request: Request):
    form = await request.form()
    kind = str(form.get("kind", PHONE))
    if kind not in KINDS:
        raise HTTPException(status_code=400, detail="Unknown kind of device.")
    if request.app.state.lan is None:
        _ = translator(config.language())
        flash(request, "error", _("Open the interface to the home network first."))
        return redirect("/settings/devices#network")
    request.app.state.pairings.start(kind)
    return redirect("/settings/devices#pair")


@router.post("/settings/devices/pair/cancel")
def cancel_pairing(request: Request):
    request.app.state.pairings.cancel()
    return redirect("/settings/devices#pair")


@router.post("/settings/devices/{device_id}/remove")
def remove_device(request: Request, device_id: str):
    _ = translator(config.language())
    removed = request.app.state.devices.remove(device_id)
    if removed is None:
        raise HTTPException(status_code=404, detail="No such device.")
    flash(request, "ok", _("{name} was removed; it cannot open the interface any more.", name=removed.name))
    return redirect("/settings/devices#devices")


@router.get("/settings/devices/certificate")
def certificate(request: Request):
    return certificate_response()


def certificate_response() -> Response:
    data = tls.authority_der()
    if data is None:
        raise HTTPException(status_code=404, detail="The certificate is made when the interface is first opened to the home network.")
    return Response(
        data, media_type="application/x-x509-ca-cert",
        headers={"Content-Disposition": 'attachment; filename="financeproject.crt"'},
    )

