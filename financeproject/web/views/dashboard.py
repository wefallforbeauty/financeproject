"""The start page: net worth, this month, what is coming, what needs attention."""

from __future__ import annotations

import math
from datetime import date

from fastapi import APIRouter, Request

from financeproject import config
from financeproject.core.dates import add_months, month_start
from financeproject.ledger.queries import find_transactions, transaction_date_range
from financeproject.reports.alerts import alerts
from financeproject.reports.balance_sheet import build_balance_sheet
from financeproject.reports.context import load_market_data
from financeproject.reports.income import income_statement
from financeproject.reports.networth import networth_series
from financeproject.reports.recurring import upcoming
from financeproject.web.app import ledger_session, money, render
from financeproject.web.i18n import translator

router = APIRouter()

UNITS = (("nominal_try", "TRY"), ("real_try", "Real TRY"), ("usd", "USD"), ("gold_g", "Gold (g)"))


def coming_text(item, _, this_month: date) -> tuple[str, str]:
    """The label and note of an upcoming payment, in the interface's language."""
    if item.kind == "installments":
        label = _("Card installments ({card})", card=item.account)
        if item.on == this_month:
            return label, _("due this month")
        return label, _("due in {month}", month=item.on.strftime("%Y-%m"))
    if item.kind == "loan":
        label = _("{loan}: installment {n}", loan=item.loan, n=item.number)
        return label, _("overdue") if item.late else _("{n} of {count}", n=item.number, count=item.count)
    note = _("{period}, {amount}", period=_(item.period), amount=_("fixed amount") if item.fixed else _("about"))
    return item.label, (_("late") + "; " + note) if item.late else note


def _number(value) -> float | None:
    """A chart value: a float, or ``None`` for a missing one (None or NaN)."""
    if value is None:
        return None
    number = float(value)
    return None if math.isnan(number) else round(number, 4)


@router.get("/")
def dashboard(request: Request):
    today = date.today()
    this_month = month_start(today)
    last_month = add_months(this_month, -1)
    with ledger_session(request) as s:
        market = load_market_data(s)
        sheet = build_balance_sheet(s, today, market=market)
        first = transaction_date_range(s)[0]
        series = {"dates": [], "units": {}}
        if first is not None:
            start = max(add_months(this_month, -12), month_start(first))
            frame = networth_series(s, start, today, "month", market=market)
            series = {
                "dates": [d.isoformat() for d in frame["date"]],
                "units": {key: [_number(v) for v in frame[key]] for key, _ in UNITS},
            }
        months = income_statement(s, last_month, this_month, depth=1, market=market, end=today)
        flows = {
            m: {
                "income": months.total_income(m),
                "expenses": months.total_expenses(m),
                "net": months.net(m),
                "rate": months.savings_rate(m),
            }
            for m in (last_month, this_month)
        }
        coming = upcoming(s, today, 30)[:10] if first is not None else []
        recent = find_transactions(s, limit=8)[0]
        notes = alerts(s, sheet.net_worth, today)
    previous = None
    if len(series["dates"]) >= 2 and series["units"]["nominal_try"][-2] is not None:
        previous = series["units"]["nominal_try"][-2]
    _ = translator(config.language())
    coming = [(item, *coming_text(item, _, this_month)) for item in coming]
    chart = {
        **series,
        "unit": "nominal_try",
        "labels": {key: _(label) for key, label in UNITS},
        "decimals": {"nominal_try": 0, "real_try": 0, "usd": 0, "gold_g": 1},
        "suffixes": {"nominal_try": "TRY", "real_try": "TRY", "usd": "USD", "gold_g": "g"},
        "summaries": {},
    }
    for key, label in UNITS:
        values = [v for v in series["units"].get(key, []) if v is not None]
        if values:
            chart["summaries"][key] = _(
                "Net worth in {unit} from {first} to {last}: from {start} to {end}.",
                unit=_(label), first=series["dates"][0], last=series["dates"][-1],
                start=money(values[0], chart["decimals"][key]), end=money(values[-1], chart["decimals"][key]),
            )
    return render(
        request,
        "dashboard.html",
        section="dashboard",
        sheet=sheet,
        net=sheet.net_worth.net,
        base_month=market.base_month,
        previous=previous,
        series=series,
        chart=chart,
        units=UNITS,
        flows=flows,
        this_month=this_month,
        last_month=last_month,
        coming=coming,
        recent=recent,
        alerts=notes,
    )
