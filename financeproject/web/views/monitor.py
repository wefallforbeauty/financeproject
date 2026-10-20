"""The wall monitor: one page that a tablet or a screen on the wall keeps open.

It shows the time, net worth and its change since the end of last month, this month's income, spending and
savings rate, the payments of the next two weeks and net worth over twelve months, and refreshes itself
every two minutes. In **guest mode** no amount and no payee is shown: the changes are given as
percentages, the payments by their kind, and the chart in percent of its first month. A monitor may turn
guest mode on, but only a phone or the computer can turn it off again."""

from __future__ import annotations

from datetime import date, datetime, timedelta

from fastapi import APIRouter, HTTPException, Request, Response

from financeproject import config
from financeproject.web.app import ledger_session, money, percent, redirect, render
from financeproject.web.i18n import translator
from financeproject.web.security import session_of
from financeproject.web.views.dashboard import coming_text, overview
from financeproject.web.views.devices import guests_on

router = APIRouter()
DAYS_AHEAD = 14
REFRESH_SECONDS = 120
CHECK_SECONDS = 5
"""How often the monitor asks whether guest mode changed (a cheap request; the panel itself is refreshed
every two minutes)."""
SHOWN = 6
"""Upcoming payments on the monitor: as many as fit on one screen."""


def guest_label(item, _) -> str:
    """What guest mode calls an upcoming payment instead of its payee."""
    if item.kind == "installments":
        return _("Card installments")
    if item.kind == "loan":
        return _("Loan installment")
    return _("Income") if item.direction > 0 else _("A payment")


def _panel(request: Request) -> dict:
    today = date.today()
    guests = guests_on()
    _ = translator(config.language())
    with ledger_session(request) as s:
        data = overview(s, today)
    this_month = data["this_month"]
    horizon = today + timedelta(days=DAYS_AHEAD)
    coming = []
    for item in [i for i in data["coming"] if i.on <= horizon][:SHOWN]:
        label, note = coming_text(item, _, this_month)
        coming.append((item, guest_label(item, _) if guests else label, note))
    dates, values = data["series"]["dates"], data["series"]["units"].get("nominal_try", [])
    first = next((v for v in values if v), None)
    chart = {"dates": dates, "values": values, "percent": False, "suffix": "TRY", "summary": ""}
    if guests and first:
        chart.update(values=[None if v is None else v / first - 1 for v in values], percent=True, suffix="")
    if values and first:
        last = next((v for v in reversed(values) if v is not None), first)
        chart["summary"] = _(
            "Net worth from {first} to {last}: {change}.", first=dates[0], last=dates[-1],
            change=percent(last / first - 1, 1, sign=True) if guests else f"{money(first, 0)} → {money(last, 0)} TRY",
        )
    session = session_of(request)
    return {
        "data": data, "net": data["sheet"].net_worth.net, "flows": data["flows"], "this_month": this_month,
        "coming": coming, "chart": chart, "guests": guests, "updated": datetime.now().strftime("%H:%M"),
        "refresh": REFRESH_SECONDS, "check": CHECK_SECONDS,
        "can_show": not (session.device is not None and session.device.is_monitor),
    }


@router.get("/monitor")
def monitor_page(request: Request):
    return render(request, "monitor.html", **_panel(request))


@router.get("/monitor/panel")
def monitor_panel(request: Request):
    return render(request, "_monitor_panel.html", **_panel(request))


@router.get("/monitor/check")
def check(guests: str = ""):
    """Nothing, or a request to load the page again when guest mode is no longer what the page shows (the
    whole page: its guest button changes too)."""
    response = Response(status_code=204)
    if (guests == "on") != guests_on():
        response.headers["HX-Refresh"] = "true"
    return response


@router.post("/monitor/guests")
async def set_guests(request: Request):
    form = await request.form()
    on = str(form.get("guests", "")) == "on"
    session = session_of(request)
    if not on and session.device is not None and session.device.is_monitor:
        raise HTTPException(status_code=403, detail="Guest mode is turned off from a phone or the computer.")
    config.save_setting("monitor", "guests", "on" if on else "off")
    target = str(form.get("next", "/monitor"))
    local = target.startswith("/") and not target.startswith("//") and "\\" not in target
    return redirect(target if local else "/monitor")
