"""Gold price (XAU/USD) from the Frankfurter v2 API (no key needed).

Frankfurter (MIT licensed, self-hostable) collects the reference rates that
central banks publish. About fourteen of them publish gold (XAU): the central
banks of Russia, Poland, Ukraine, Romania and others. Without ``providers`` the
API returns the blend of their latest values, which smooths out the
publishing lags of single banks. Daily since 1999.

The ledger stores gold per gram in USD (``XAU_G``/``USD``), and the TRY gram
price follows from TCMB's USD/TRY rate. ``XAU_G`` is 24-carat (fine) gold.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation

import httpx

from financeproject.core.prices import TROY_OUNCE_GRAMS
from financeproject.errors import SourceError

BASE_URL = "https://api.frankfurter.dev/v2"
GRAM_DECIMALS = Decimal("0.00000001")


def parse_rates(payload: object) -> list[tuple[date, str, str, Decimal]]:
    """``[{"date": "2026-10-08", "base": "XAU", "quote": "USD", "rate": 4125.58}, …]``."""
    if isinstance(payload, dict) and "message" in payload:
        raise SourceError(f"Frankfurter error: {payload.get('message')}")
    if not isinstance(payload, list):
        raise SourceError("Unexpected answer from Frankfurter (not a list).")
    out: list[tuple[date, str, str, Decimal]] = []
    for row in payload:
        try:
            out.append(
                (
                    date.fromisoformat(row["date"]),
                    str(row["base"]).upper(),
                    str(row["quote"]).upper(),
                    Decimal(str(row["rate"])),
                )
            )
        except (KeyError, TypeError, ValueError, InvalidOperation) as exc:
            raise SourceError(f"Cannot read a Frankfurter row: {row!r}") from exc
    return out


def ounce_to_gram(price_per_ounce: Decimal) -> Decimal:
    return (price_per_ounce / TROY_OUNCE_GRAMS).quantize(GRAM_DECIMALS)


def fetch_gold_usd_per_gram(
    client: httpx.Client, start: date, end: date, providers: str | None = None
) -> list[tuple[date, Decimal]]:
    """Daily USD price of one gram of gold between ``start`` and ``end``."""
    params = {"base": "XAU", "quotes": "USD", "from": start.isoformat(), "to": end.isoformat()}
    if providers:
        params["providers"] = providers
    response = client.get(f"{BASE_URL}/rates", params=params)
    if response.status_code != 200:
        raise SourceError(f"Frankfurter returned {response.status_code}: {response.text[:200]}")
    try:
        payload = response.json()
    except ValueError as exc:
        raise SourceError("The Frankfurter answer is not JSON.") from exc
    return [
        (day, ounce_to_gram(rate))
        for day, base, quote, rate in parse_rates(payload)
        if base == "XAU" and quote == "USD"
    ]
