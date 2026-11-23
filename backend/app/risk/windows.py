"""Analysis windows. Scores default to the last complete calendar year."""

from __future__ import annotations

import calendar
from datetime import date, timedelta


def _feb_end(year: int) -> date:
    return date(year, 2, calendar.monthrange(year, 2)[1])


def last_winter_bounds(today: date) -> tuple[date, date]:
    """Most recent winter that has already finished (December through February)."""
    if today.month >= 3:
        return date(today.year - 1, 12, 1), _feb_end(today.year)
    return date(today.year - 2, 12, 1), _feb_end(today.year - 1)


def resolve_window(name: str | None, today: date | None = None) -> tuple[date, date, str, str]:
    """Return start, end, stable key, and a short label.

    Unknown keys fall back to the previous calendar year so a bad model value
    cannot invent a window.
    """
    today = today or date.today()
    key = (name or "last_calendar_year").strip()
    if key == "trailing_365":
        end = today - timedelta(days=7)
        start = end - timedelta(days=364)
        label = f"{start.isoformat()} to {end.isoformat()} (trailing 365 days, ending 7 days ago)"
        return start, end, key, label
    if key == "last_winter":
        start, end = last_winter_bounds(today)
        label = f"winter {start.isoformat()} to {end.isoformat()}"
        return start, end, key, label
    year = today.year - 1
    start, end = date(year, 1, 1), date(year, 12, 31)
    label = f"calendar year {year}"
    return start, end, "last_calendar_year", label


WINDOW_KEYS = ("last_calendar_year", "trailing_365", "last_winter")


def last_calendar_year(today: date | None = None) -> int:
    return (today or date.today()).year - 1


def window_years(today: date | None = None) -> set[int]:
    """Every year that appears in some supported window. Drafts may repeat these."""
    years: set[int] = set()
    for key in WINDOW_KEYS:
        start, end, _key, _label = resolve_window(key, today)
        years.update({start.year, end.year})
    return years