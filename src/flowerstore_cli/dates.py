"""Dates and delivery cutoffs, in Manila time like the storefront."""

from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

MANILA = ZoneInfo("Asia/Manila")
MAX_DAYS_AHEAD = 365


def now() -> dt.datetime:
    return dt.datetime.now(MANILA)


def parse_date(text: str, today: dt.date | None = None) -> dt.date:
    """YYYY-MM-DD, `today` or `tomorrow`, from today up to a year ahead (Manila)."""
    today = today or now().date()
    t = text.strip().lower()
    if t == "today":
        d = today
    elif t == "tomorrow":
        d = today + dt.timedelta(days=1)
    else:
        try:
            d = dt.date.fromisoformat(t)
        except ValueError:
            raise ValueError(f"'{text}' is not a date: use YYYY-MM-DD, today or tomorrow")
    if d < today:
        raise ValueError(f"{d} is in the past (Manila time)")
    if d > today + dt.timedelta(days=MAX_DAYS_AHEAD):
        raise ValueError(f"{d} is more than a year ahead")
    return d


def order_by(delivery: dt.date, closes_at: int | None, weekend_closes_at: int | None) -> str | None:
    """When orders for a slot on `delivery` close, as an ISO time in Manila.

    The CMS stores a cutoff hour per slot (a separate one on weekends). Hours of 24 or more
    reach back whole days: 30 means 6 AM the day before. The storefront picks the weekend
    hour by the delivery date's weekday, so this does too.
    """
    hours = weekend_closes_at if delivery.weekday() >= 5 else closes_at
    if hours is None:
        return None
    hours = int(hours)
    day = delivery - dt.timedelta(days=hours // 24)
    return dt.datetime.combine(day, dt.time(hours % 24), MANILA).isoformat()
