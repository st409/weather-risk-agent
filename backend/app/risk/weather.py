"""Open-Meteo historical daily weather and National Weather Service active alerts."""

from __future__ import annotations

import logging
from datetime import date, datetime, timezone

import httpx

from app.config import alert_cache_minutes, data_mode, nws_user_agent
from app.db import repository
from app.risk.fixtures import build_fixture_days
from app.risk.hubs import Hub
from app.risk.types import AlertBatch, DailyBatch, DailyObs, SourceRef

logger = logging.getLogger(__name__)

OPEN_METEO_URL = "https://archive-api.open-meteo.com/v1/archive"
NWS_ALERTS_URL = "https://api.weather.gov/alerts/active"

SEVERITY_WEIGHTS = {
    "Extreme": 3.0,
    "Severe": 2.0,
    "Moderate": 1.0,
    "Minor": 0.5,
    "Unknown": 0.25,
}

_CLIENT = httpx.Client(
    timeout=httpx.Timeout(30.0, connect=10.0),
    headers={"User-Agent": "weather-risk-agent/0.1"},
    follow_redirects=True,
)


class WeatherUnavailable(RuntimeError):
    """Live daily weather could not be loaded for a hub. Nothing is substituted for it."""

    def __init__(self, hub_id: str):
        super().__init__(f"live weather unavailable for {hub_id}")
        self.hub_id = hub_id


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _obs_from_dict(row: dict) -> DailyObs:
    return DailyObs(
        date=row["date"],
        temp_max_f=row.get("temp_max_f"),
        temp_min_f=row.get("temp_min_f"),
        precip_in=row.get("precip_in"),
        snowfall_cm=row.get("snowfall_cm"),
        wind_gust_mph=row.get("wind_gust_mph"),
    )


def _fixture_batch(hub: Hub, start: date, end: date, reason: str) -> DailyBatch:
    days = [_obs_from_dict(row) for row in build_fixture_days(hub.hub_id, start, end)]
    source = SourceRef(
        name="Offline climatology stand-in",
        source_type="static_public_dataset",
        retrieved_at=_now(),
        uncertainty_note=reason,
    )
    return DailyBatch(days=days, source=source, fallback=True)


def open_meteo_note() -> str:
    """Current snow-day wording. Cached windows must not keep an older unit or definition."""
    return (
        "Open-Meteo archive reanalysis (ERA5 / IFS blend). Snowfall is stored in centimeters. "
        "A snow day is daily snowfall_sum >= 1 cm. This is not a station log."
    )


def load_daily(hub: Hub, start: date, end: date, force: bool = False) -> DailyBatch:
    if data_mode() == "fixture":
        return _fixture_batch(
            hub,
            start,
            end,
            "WEATHER_DATA_MODE=fixture, so these daily values are a deterministic stand-in, not an Open-Meteo pull.",
        )
    if not force:
        cached = repository.get_daily_window(hub.hub_id, start.isoformat(), end.isoformat())
        if cached and not cached["fallback"]:
            if cached["uncertainty_note"] == open_meteo_note():
                source = SourceRef(
                    name=cached["source_name"],
                    source_type="cached_api",
                    retrieved_at=cached["retrieved_at"],
                    uncertainty_note=cached["uncertainty_note"],
                )
                return DailyBatch(days=[_obs_from_dict(row) for row in cached["days"]], source=source, fallback=False)
            repository.delete_scores_for_window(hub.hub_id, start.isoformat(), end.isoformat())
    try:
        days, retrieved_at = _fetch_open_meteo(hub, start, end)
    except Exception as exc:
        logger.warning("Open-Meteo failed for %s: %s", hub.hub_id, exc)
        raise WeatherUnavailable(hub.hub_id) from exc
    note = open_meteo_note()
    source = SourceRef(
        name="Open-Meteo Historical Weather API",
        source_type="live_api",
        retrieved_at=retrieved_at,
        uncertainty_note=note,
    )
    repository.save_daily_window(
        hub.hub_id,
        start.isoformat(),
        end.isoformat(),
        days,
        source.name,
        retrieved_at,
        note,
        fallback=False,
    )
    return DailyBatch(days=days, source=source, fallback=False)


def load_alerts(hub: Hub, force: bool = False) -> AlertBatch:
    if data_mode() == "fixture":
        return _empty_alerts(
            "Fixture mode holds active alert counts at zero and does not call the National Weather Service.",
            fallback=True,
            source_type="static_public_dataset",
            name="National Weather Service active alerts (fixture)",
        )
    if not force:
        cached = repository.get_alert_cache(hub.hub_id, max_age_minutes=alert_cache_minutes())
        if cached:
            source = SourceRef(
                name=cached["source_name"],
                source_type="cached_api",
                retrieved_at=cached["retrieved_at"],
                uncertainty_note=cached["uncertainty_note"],
            )
            return AlertBatch(
                weighted=cached["weighted"],
                available=bool(cached["available"]),
                count=int(cached["count"]),
                events=cached["events"],
                source=source,
                fallback=False,
            )
    try:
        weighted, events, retrieved_at = _fetch_nws(hub)
    except Exception as exc:
        logger.warning("NWS alerts failed for %s: %s", hub.hub_id, exc)
        return _empty_alerts(
            f"National Weather Service alerts could not be retrieved ({exc.__class__.__name__}). Alert scores were omitted.",
            fallback=True,
            source_type="live_api",
            name="National Weather Service active alerts",
            available=False,
        )
    note = (
        "Active NWS point alerts only. Severity weights are Extreme 3, Severe 2, Moderate 1, Minor 0.5, Unknown 0.25. "
        "A zero means nothing is in effect at this point right now, not that the season is quiet."
    )
    source = SourceRef(
        name="National Weather Service active alerts",
        source_type="live_api",
        retrieved_at=retrieved_at,
        uncertainty_note=note,
    )
    batch = AlertBatch(
        weighted=weighted,
        available=True,
        count=len(events),
        events=events,
        source=source,
        fallback=False,
    )
    repository.save_alert_cache(hub.hub_id, batch)
    return batch


def _empty_alerts(note: str, fallback: bool, source_type: str, name: str, available: bool = True) -> AlertBatch:
    # Fixture mode is "available" so a real zero is scored. A failed request is unavailable
    # and the alert weight is dropped instead of treated as a quiet day.
    return AlertBatch(
        weighted={key: 0.0 for key in ("winter", "flood", "hurricane", "heat", "severe_storm")},
        available=available,
        count=0,
        events=[],
        source=SourceRef(name=name, source_type=source_type, retrieved_at=_now(), uncertainty_note=note),
        fallback=fallback,
    )


def _fetch_open_meteo(hub: Hub, start: date, end: date) -> tuple[list[DailyObs], str]:
    params = {
        "latitude": hub.lat,
        "longitude": hub.lon,
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "daily": "temperature_2m_max,temperature_2m_min,precipitation_sum,snowfall_sum,wind_gusts_10m_max",
        "temperature_unit": "fahrenheit",
        "precipitation_unit": "inch",
        "wind_speed_unit": "mph",
        "timezone": hub.timezone,
    }
    payload = _get_json(OPEN_METEO_URL, params=params, headers=None)
    daily = payload.get("daily") or {}
    units = payload.get("daily_units") or {}
    dates = daily.get("time") or []
    if not dates:
        raise RuntimeError("Open-Meteo returned no daily dates")
    rows: list[DailyObs] = []
    for index, day in enumerate(dates):
        rows.append(
            DailyObs(
                date=day,
                temp_max_f=_to_f(_at(daily, "temperature_2m_max", index), units.get("temperature_2m_max", "")),
                temp_min_f=_to_f(_at(daily, "temperature_2m_min", index), units.get("temperature_2m_min", "")),
                precip_in=_to_inches(_at(daily, "precipitation_sum", index), units.get("precipitation_sum", "")),
                snowfall_cm=_to_cm(_at(daily, "snowfall_sum", index), units.get("snowfall_sum", "")),
                wind_gust_mph=_to_mph(_at(daily, "wind_gusts_10m_max", index), units.get("wind_gusts_10m_max", "")),
            )
        )
    return rows, _now()


def _fetch_nws(hub: Hub) -> tuple[dict[str, float], list[str], str]:
    headers = {"User-Agent": nws_user_agent(), "Accept": "application/geo+json"}
    payload = _get_json(NWS_ALERTS_URL, params={"point": f"{hub.lat},{hub.lon}"}, headers=headers)
    weighted = {key: 0.0 for key in ("winter", "flood", "hurricane", "heat", "severe_storm")}
    events: list[str] = []
    for feature in payload.get("features") or []:
        props = feature.get("properties") or {}
        event = str(props.get("event") or "")
        if not event or "test" in event.lower():
            continue
        hazard = _classify_event(event)
        if hazard is None:
            continue
        severity = str(props.get("severity") or "Unknown")
        weighted[hazard] += SEVERITY_WEIGHTS.get(severity, 0.25)
        if len(events) < 8:
            events.append(f"{event} ({severity})")
    return weighted, events, _now()


def _classify_event(event: str) -> str | None:
    text = event.lower()
    if any(token in text for token in ("hurricane", "tropical storm", "tropical depression", "storm surge")):
        return "hurricane"
    if "flood" in text:
        return "flood"
    if "heat" in text:
        return "heat"
    if any(token in text for token in ("wind chill", "winter", "snow", "blizzard", "ice storm", "freeze", "frost", "lake effect")):
        return "winter"
    if any(token in text for token in ("tornado", "thunderstorm", "hail", "high wind")):
        return "severe_storm"
    return None


def _get_json(url: str, params: dict | None, headers: dict | None) -> dict:
    last_error: Exception | None = None
    for _ in range(2):
        try:
            response = _CLIENT.get(url, params=params, headers=headers)
            response.raise_for_status()
            body = response.json()
            if isinstance(body, list):
                if not body:
                    raise RuntimeError("empty list response")
                body = body[0]
            if not isinstance(body, dict):
                raise RuntimeError("unexpected response shape")
            return body
        except Exception as exc:
            last_error = exc
    assert last_error is not None
    raise last_error


def _at(daily: dict, key: str, index: int) -> float | None:
    values = daily.get(key) or []
    if index >= len(values):
        return None
    value = values[index]
    if value is None:
        return None
    return float(value)


def _to_f(value: float | None, unit: str) -> float | None:
    if value is None:
        return None
    text = unit.lower()
    if "c" in text and "f" not in text:
        return value * 9.0 / 5.0 + 32.0
    return value


def _to_cm(value: float | None, unit: str) -> float | None:
    """Open-Meteo returns snowfall in inches when precipitation_unit is inch."""
    if value is None:
        return None
    text = (unit or "").lower()
    if "inch" in text or text.strip() == "in":
        return value * 2.54
    if "mm" in text:
        return value / 10.0
    return value


def _to_inches(value: float | None, unit: str) -> float | None:
    if value is None:
        return None
    if "mm" in unit.lower():
        return value / 25.4
    return value


def _to_mph(value: float | None, unit: str) -> float | None:
    if value is None:
        return None
    text = unit.lower()
    if "km" in text:
        return value / 1.609344
    if "m/s" in text or text.strip() in {"ms", "m/s"}:
        return value * 2.23694
    return value
