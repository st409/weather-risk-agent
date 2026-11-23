"""Fetch inputs and score hubs. Scoring itself stays in scoring.py."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import date

from app.db import repository
from app.risk.hazards import get_baseline
from app.risk.hubs import HUB_BY_ID, Hub, hubs_in_region
from app.risk.scoring import HAZARD_ORDER, metrics_from_days, score_hub
from app.risk.types import HubReport
from app.risk.weather import load_alerts, load_daily
from app.risk.windows import resolve_window


def select_hubs(hub_ids: list[str] | None, region: str | None) -> list[Hub]:
    if hub_ids:
        return [HUB_BY_ID[hub_id] for hub_id in hub_ids if hub_id in HUB_BY_ID]
    return hubs_in_region(region)


def build_report(hub: Hub, start: date, end: date, label: str, force: bool = False) -> HubReport:
    daily = load_daily(hub, start, end, force=force)
    alerts = load_alerts(hub, force=force)
    metrics = metrics_from_days(daily.days)
    baseline = get_baseline(hub.hub_id)
    return score_hub(
        hub,
        metrics,
        alerts,
        baseline,
        daily,
        window_label=label,
        window_start=start.isoformat(),
        window_end=end.isoformat(),
    )


def build_reports(hubs: list[Hub], start: date, end: date, label: str, force: bool = False) -> list[HubReport]:
    if len(hubs) <= 1:
        return [build_report(hub, start, end, label, force=force) for hub in hubs]
    workers = min(6, len(hubs))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(lambda hub: build_report(hub, start, end, label, force), hubs))


def recompute_stale_scores() -> int:
    """Drop snapshots scored with snowfall > 0 cm and score those windows again."""
    refreshed = 0
    for hub_id, start, end, label in repository.stale_score_windows():
        hub = HUB_BY_ID.get(hub_id)
        if hub is None:
            repository.delete_scores_for_window(hub_id, start, end)
            continue
        report = build_report(hub, date.fromisoformat(start), date.fromisoformat(end), label)
        repository.delete_scores_for_window(hub_id, start, end)
        repository.save_score_snapshot(public_report(report))
        refreshed += 1
    return refreshed


def score_selection(
    hub_ids: list[str] | None,
    region: str | None,
    window_key: str | None,
    force: bool = False,
) -> tuple[list[HubReport], str, str, str]:
    start, end, key, label = resolve_window(window_key)
    hubs = select_hubs(hub_ids, region)
    reports = build_reports(hubs, start, end, label, force=force)
    return reports, key, label, f"{start.isoformat()} to {end.isoformat()}"


def public_report(report: HubReport) -> dict:
    return {
        "hub_id": report.hub_id,
        "name": report.name,
        "region": report.region,
        "overall_score": round(report.overall_score, 2),
        "window_label": report.window_label,
        "window_start": report.window_start,
        "window_end": report.window_end,
        "hazard_scores": [
            {
                "hazard": hazard,
                "score": round(report.hazards[hazard].score, 2),
                "drivers": [driver.public() for driver in report.hazards[hazard].drivers],
            }
            for hazard in (*HAZARD_ORDER, "overall")
        ],
        "metrics": {
            "snow_days_pct": _round(report.metrics.snow_days_pct),
            "snow_days": report.metrics.snow_days,
            "snow_valid_days": report.metrics.snow_valid_days,
            "freeze_days_pct": _round(report.metrics.freeze_days_pct),
            "heavy_rain_days_pct": _round(report.metrics.heavy_rain_days_pct),
            "very_heavy_rain_days_pct": _round(report.metrics.very_heavy_rain_days_pct),
            "heat_days_pct": _round(report.metrics.heat_days_pct),
            "extreme_heat_days_pct": _round(report.metrics.extreme_heat_days_pct),
            "high_wind_days_pct": _round(report.metrics.high_wind_days_pct),
        },
        "active_events": report.active_events,
        "sources": [source.public() for source in _unique_sources(report)],
        "uncertainties": report.uncertainties,
        "weather_fallback": report.weather_fallback,
        "snow_day_rule": "gte_1cm",
    }


def _round(value: float | None) -> float | None:
    if value is None:
        return None
    return round(value, 2)


def _unique_sources(report: HubReport) -> list:
    seen = set()
    unique = []
    for source in report.sources:
        marker = (source.name, source.retrieved_at, source.source_type)
        if marker in seen:
            continue
        seen.add(marker)
        unique.append(source)
    return unique
