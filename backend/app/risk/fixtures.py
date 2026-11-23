"""Deterministic daily climatology used by tests and when a live fetch fails.

This is not an observation record. Callers must label it as a stand-in.
"""

from __future__ import annotations

from datetime import date, timedelta

# month sets are 1-12. every=0 means the condition never fires.
PROFILES: dict[str, dict] = {
    "minneapolis": {"snow_months": {11, 12, 1, 2, 3}, "snow_every": 2, "freeze_months": {10, 11, 12, 1, 2, 3, 4}, "freeze_every": 1, "hot_months": set(), "hot_every": 0, "extreme_months": set(), "extreme_every": 0, "heavy_every": 40, "very_every": 120, "wind_months": {3, 4, 5, 6}, "wind_every": 15},
    "detroit": {"snow_months": {11, 12, 1, 2, 3}, "snow_every": 3, "freeze_months": {12, 1, 2, 3}, "freeze_every": 1, "hot_months": {7}, "hot_every": 12, "extreme_months": set(), "extreme_every": 0, "heavy_every": 36, "very_every": 110, "wind_months": {3, 4, 5, 6}, "wind_every": 14},
    "chicago": {"snow_months": {11, 12, 1, 2, 3}, "snow_every": 4, "freeze_months": {12, 1, 2}, "freeze_every": 1, "hot_months": {6, 7, 8}, "hot_every": 10, "extreme_months": set(), "extreme_every": 0, "heavy_every": 30, "very_every": 100, "wind_months": {3, 4, 5, 6}, "wind_every": 12},
    "denver": {"snow_months": {10, 11, 12, 1, 2, 3, 4}, "snow_every": 5, "freeze_months": {11, 12, 1, 2, 3}, "freeze_every": 1, "hot_months": {7}, "hot_every": 8, "extreme_months": set(), "extreme_every": 0, "heavy_every": 45, "very_every": 150, "wind_months": {3, 4, 5, 6}, "wind_every": 10},
    "dallas": {"snow_months": {1, 2}, "snow_every": 20, "freeze_months": {12, 1, 2}, "freeze_every": 5, "hot_months": {5, 6, 7, 8, 9}, "hot_every": 1, "extreme_months": {7, 8}, "extreme_every": 2, "heavy_every": 22, "very_every": 70, "wind_months": {3, 4, 5, 6}, "wind_every": 8},
    "houston": {"snow_months": set(), "snow_every": 0, "freeze_months": {1}, "freeze_every": 30, "hot_months": {6, 7, 8}, "hot_every": 1, "extreme_months": {7, 8}, "extreme_every": 6, "heavy_every": 12, "very_every": 36, "wind_months": {3, 4, 5, 6}, "wind_every": 14},
    "miami": {"snow_months": set(), "snow_every": 0, "freeze_months": set(), "freeze_every": 0, "hot_months": {6, 7, 8, 9}, "hot_every": 4, "extreme_months": set(), "extreme_every": 0, "heavy_every": 18, "very_every": 60, "wind_months": {6, 7, 8, 9, 10}, "wind_every": 16},
    "atlanta": {"snow_months": {1, 2}, "snow_every": 25, "freeze_months": {12, 1, 2}, "freeze_every": 4, "hot_months": {6, 7, 8}, "hot_every": 2, "extreme_months": {7}, "extreme_every": 10, "heavy_every": 16, "very_every": 50, "wind_months": {3, 4, 5, 6}, "wind_every": 12},
    "newyork": {"snow_months": {12, 1, 2, 3}, "snow_every": 4, "freeze_months": {12, 1, 2}, "freeze_every": 1, "hot_months": {7, 8}, "hot_every": 8, "extreme_months": set(), "extreme_every": 0, "heavy_every": 28, "very_every": 90, "wind_months": {1, 2, 3, 11, 12}, "wind_every": 8},
    "losangeles": {"snow_months": set(), "snow_every": 0, "freeze_months": set(), "freeze_every": 0, "hot_months": {8, 9, 10}, "hot_every": 3, "extreme_months": {9}, "extreme_every": 12, "heavy_every": 40, "very_every": 120, "wind_months": {1, 2, 11, 12}, "wind_every": 10},
}


def _iter_dates(start: date, end: date):
    cursor = start
    while cursor <= end:
        yield cursor
        cursor += timedelta(days=1)


def _hits(day: date, months: set[int], every: int) -> bool:
    if every <= 0 or day.month not in months:
        return False
    return day.toordinal() % every == 0


def build_fixture_days(hub_id: str, start: date, end: date) -> list[dict]:
    profile = PROFILES[hub_id]
    rows: list[dict] = []
    for day in _iter_dates(start, end):
        extreme = _hits(day, profile["extreme_months"], profile["extreme_every"])
        hot = extreme or _hits(day, profile["hot_months"], profile["hot_every"])
        freeze = _hits(day, profile["freeze_months"], profile["freeze_every"])
        snow = _hits(day, profile["snow_months"], profile["snow_every"])
        if day.toordinal() % profile["very_every"] == 0:
            precip = 2.4
        elif day.toordinal() % profile["heavy_every"] == 0:
            precip = 1.3
        else:
            precip = 0.08
        gust = 46.0 if _hits(day, profile["wind_months"], profile["wind_every"]) else 18.0
        if extreme:
            temp_max = 102.0
        elif hot:
            temp_max = 96.0
        elif day.month in (12, 1, 2):
            temp_max = 36.0
        elif day.month in (6, 7, 8):
            temp_max = 88.0
        else:
            temp_max = 64.0
        temp_min = 18.0 if freeze else (70.0 if day.month in (6, 7, 8) else 45.0)
        rows.append(
            {
                "date": day.isoformat(),
                "temp_max_f": temp_max,
                "temp_min_f": temp_min,
                "precip_in": precip,
                "snowfall_cm": 3.0 if snow else 0.0,
                "wind_gust_mph": gust,
            }
        )
    return rows
