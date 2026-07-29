"""Display helpers shared by Discord messages and the web UI."""

from __future__ import annotations

import datetime as dt
from decimal import ROUND_HALF_UP, Decimal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

_SYMBOLS = {"USD": "$", "CAD": "$", "AUD": "$", "NZD": "$", "GBP": "£",
            "EUR": "€", "JPY": "¥", "INR": "₹"}


def money(amount_minor: int, currency: str = "USD") -> str:
    """Render integer minor units for humans: ``-4321, "USD"`` -> ``-$43.21``."""
    value = (Decimal(amount_minor) / Decimal(100)).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )
    symbol = _SYMBOLS.get(currency.upper(), "")
    sign = "-" if value < 0 else ""
    body = f"{abs(value):,.2f}"
    return f"{sign}{symbol}{body}" if symbol else f"{sign}{body} {currency.upper()}"


def money_plain(amount_minor: int) -> str:
    """Unsigned-symbol form for CSV export: ``-43.21``."""
    return str(
        (Decimal(amount_minor) / Decimal(100)).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )
    )


def get_zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("UTC")


def local(value: dt.datetime, tz_name: str = "UTC") -> dt.datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=dt.UTC)
    return value.astimezone(get_zone(tz_name))


def when(value: dt.datetime, tz_name: str = "UTC") -> str:
    return f"{local(value, tz_name):%b %-d, %-I:%M %p}"


def when_full(value: dt.datetime, tz_name: str = "UTC") -> str:
    return f"{local(value, tz_name):%Y-%m-%d %H:%M %Z}"


def iso_date(value: dt.datetime, tz_name: str = "UTC") -> str:
    return f"{local(value, tz_name):%Y-%m-%d}"
