"""Deterministic 0-100 disruption scores.

Formulas match DESIGN.md. The model never supplies these numbers.
Weights are rescaled only when an input is missing. A real zero is kept.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.risk.types import (
    AlertBatch,
    DailyBatch,
    DailyObs,
    Driver,
    HazardBaseline,
    HazardScore,
    HubReport,
    SourceRef,
    WeatherMetrics,
)
from app.risk.hubs import Hub

WINTER_SNOW_HIGH = 35.0
WINTER_FREEZE_HIGH = 60.0
WINTER_ALERT_HIGH = 5.0
FLOOD_HEAVY_HIGH = 15.0
FLOOD_VERY_HEAVY_HIGH = 5.0
FLOOD_ALERT_HIGH = 5.0
HURRICANE_ALERT_HIGH = 3.0
HEAT_DAYS_HIGH = 30.0
HEAT_EXTREME_HIGH = 10.0
HEAT_ALERT_HIGH = 5.0
SEVERE_ALERT_HIGH = 5.0
SEVERE_WIND_HIGH = 10.0

OVERALL_WEIGHTS = {
    "winter": 0.30,
    "flood": 0.25,
    "hurricane": 0.20,
    "severe_storm": 0.15,
    "heat": 0.10,
}

HAZARD_ORDER = ("winter", "flood", "hurricane", "severe_storm", "heat")


def clamp_0_100(value: float) -> float:
    return min(100.0, max(0.0, value))


def normalize(value: float, high: float) -> float:
    if high == 0:
        return 0.0
    return clamp_0_100((value / high) * 100.0)


def _pct(hits: int, valid: int) -> float | None:
    if valid <= 0:
        return None
    return hits / valid * 100.0


def _confidence(valid_days: int) -> str:
    if valid_days >= 330:
        return "high"
    if valid_days >= 180:
        return "medium"
    return "low"


def metrics_from_days(days: list[DailyObs]) -> WeatherMetrics:
    metrics = WeatherMetrics()
    for day in days:
        if day.snowfall_cm is not None:
            metrics.snow_valid_days += 1
            if day.snowfall_cm >= 1:
                metrics.snow_days += 1
        if day.temp_min_f is not None:
            metrics.freeze_valid_days += 1
            if day.temp_min_f <= 32:
                metrics.freeze_days += 1
        if day.precip_in is not None:
            metrics.precip_valid_days += 1
            if day.precip_in >= 1:
                metrics.heavy_rain_days += 1
            if day.precip_in >= 2:
                metrics.very_heavy_rain_days += 1
        if day.temp_max_f is not None:
            metrics.temp_max_valid_days += 1
            if day.temp_max_f >= 95:
                metrics.heat_days += 1
            if day.temp_max_f >= 100:
                metrics.extreme_heat_days += 1
        if day.wind_gust_mph is not None:
            metrics.wind_valid_days += 1
            if day.wind_gust_mph >= 40:
                metrics.high_wind_days += 1
    metrics.snow_days_pct = _pct(metrics.snow_days, metrics.snow_valid_days)
    metrics.freeze_days_pct = _pct(metrics.freeze_days, metrics.freeze_valid_days)
    metrics.heavy_rain_days_pct = _pct(metrics.heavy_rain_days, metrics.precip_valid_days)
    metrics.very_heavy_rain_days_pct = _pct(metrics.very_heavy_rain_days, metrics.precip_valid_days)
    metrics.heat_days_pct = _pct(metrics.heat_days, metrics.temp_max_valid_days)
    metrics.extreme_heat_days_pct = _pct(metrics.extreme_heat_days, metrics.temp_max_valid_days)
    metrics.high_wind_days_pct = _pct(metrics.high_wind_days, metrics.wind_valid_days)
    return metrics


@dataclass
class _Part:
    metric_name: str
    raw_value: float | None
    normalized: float | None
    nominal_weight: float
    source: str
    freshness: str
    confidence: str


def _combine(parts: list[_Part]) -> tuple[float, list[Driver], bool]:
    usable = [part for part in parts if part.normalized is not None]
    rescaled = len(usable) != len(parts)
    denom = sum(part.nominal_weight for part in usable)
    if denom <= 0 or not usable:
        return 0.0, [], True
    drivers: list[Driver] = []
    total = 0.0
    for part in usable:
        effective = part.nominal_weight / denom
        contribution = effective * part.normalized
        total += contribution
        drivers.append(
            Driver(
                metric_name=part.metric_name,
                value=part.raw_value,
                weight=effective,
                contribution=contribution,
                source=part.source,
                freshness=part.freshness,
                confidence=part.confidence,
            )
        )
    return total, drivers, rescaled


def _weather_part(
    metric_name: str,
    raw: float | None,
    high: float,
    weight: float,
    source: SourceRef,
    freshness: str,
    valid_days: int,
) -> _Part:
    normalized = None if raw is None else normalize(raw, high)
    return _Part(
        metric_name=metric_name,
        raw_value=raw,
        normalized=normalized,
        nominal_weight=weight,
        source=source.name,
        freshness=freshness,
        confidence=_confidence(valid_days) if raw is not None else "low",
    )


def _direct_part(
    metric_name: str,
    raw: float | None,
    weight: float,
    source_name: str,
    freshness: str,
    confidence: str,
    already_normalized: bool = True,
    high: float | None = None,
) -> _Part:
    if raw is None:
        normalized = None
    elif already_normalized:
        normalized = clamp_0_100(raw)
    else:
        normalized = normalize(raw, high or 1.0)
    return _Part(
        metric_name=metric_name,
        raw_value=raw,
        normalized=normalized,
        nominal_weight=weight,
        source=source_name,
        freshness=freshness,
        confidence=confidence,
    )


def score_hub(
    hub: Hub,
    metrics: WeatherMetrics,
    alerts: AlertBatch,
    baseline: HazardBaseline,
    daily: DailyBatch,
    window_label: str,
    window_start: str,
    window_end: str,
) -> HubReport:
    freshness = f"{window_start} to {window_end}"
    weather_source = daily.source
    alert_source = alerts.source.name
    alert_fresh = f"active alerts at {alerts.source.retrieved_at}"
    alert_conf = "low" if (not alerts.available or alerts.fallback) else "medium"
    base_name = "FEMA National Risk Index county snapshot"
    base_fresh = f"snapshot {baseline.retrieved_at}"
    base_conf = "medium"

    def alert_value(hazard: str) -> float | None:
        if not alerts.available:
            return None
        return float(alerts.weighted.get(hazard, 0.0))

    winter, winter_rescaled = _finish(
        [
            _weather_part("snow_days_pct", metrics.snow_days_pct, WINTER_SNOW_HIGH, 0.40, weather_source, freshness, metrics.snow_valid_days),
            _weather_part("freeze_days_pct", metrics.freeze_days_pct, WINTER_FREEZE_HIGH, 0.30, weather_source, freshness, metrics.freeze_valid_days),
            _direct_part("winter_baseline_score", baseline.winter_weather, 0.20, base_name, base_fresh, base_conf),
            _direct_part("winter_alert_score", alert_value("winter"), 0.10, alert_source, alert_fresh, alert_conf, already_normalized=False, high=WINTER_ALERT_HIGH),
        ]
    )
    flood, flood_rescaled = _finish(
        [
            _direct_part("flood_baseline_score", baseline.flood, 0.35, base_name, base_fresh, base_conf),
            _weather_part("heavy_rain_days_pct", metrics.heavy_rain_days_pct, FLOOD_HEAVY_HIGH, 0.30, weather_source, freshness, metrics.precip_valid_days),
            _weather_part("very_heavy_rain_days_pct", metrics.very_heavy_rain_days_pct, FLOOD_VERY_HEAVY_HIGH, 0.20, weather_source, freshness, metrics.precip_valid_days),
            _direct_part("flood_alert_score", alert_value("flood"), 0.15, alert_source, alert_fresh, alert_conf, already_normalized=False, high=FLOOD_ALERT_HIGH),
        ]
    )
    coastal = 100.0 if hub.surge_exposed else 0.0
    hurricane, hurricane_rescaled = _finish(
        [
            _direct_part("hurricane_baseline_score", baseline.hurricane, 0.70, base_name, base_fresh, base_conf),
            _direct_part("tropical_alert_score", alert_value("hurricane"), 0.20, alert_source, alert_fresh, alert_conf, already_normalized=False, high=HURRICANE_ALERT_HIGH),
            _direct_part("coastal_modifier", coastal, 0.10, "Hub surge-exposure flag", "fixed hub metadata", "high"),
        ]
    )
    heat, heat_rescaled = _finish(
        [
            _weather_part("heat_days_pct", metrics.heat_days_pct, HEAT_DAYS_HIGH, 0.35, weather_source, freshness, metrics.temp_max_valid_days),
            _weather_part("extreme_heat_days_pct", metrics.extreme_heat_days_pct, HEAT_EXTREME_HIGH, 0.25, weather_source, freshness, metrics.temp_max_valid_days),
            _direct_part("heat_baseline_score", baseline.heat_wave, 0.25, base_name, base_fresh, base_conf),
            _direct_part("heat_alert_score", alert_value("heat"), 0.15, alert_source, alert_fresh, alert_conf, already_normalized=False, high=HEAT_ALERT_HIGH),
        ]
    )
    severe, severe_rescaled = _finish(
        [
            _direct_part("severe_baseline_score", baseline.severe, 0.50, base_name, base_fresh, base_conf),
            _direct_part("severe_alert_score", alert_value("severe_storm"), 0.30, alert_source, alert_fresh, alert_conf, already_normalized=False, high=SEVERE_ALERT_HIGH),
            _weather_part("high_wind_days_pct", metrics.high_wind_days_pct, SEVERE_WIND_HIGH, 0.20, weather_source, freshness, metrics.wind_valid_days),
        ]
    )
    hazards = {
        "winter": HazardScore("winter", winter[0], winter[1], winter_rescaled),
        "flood": HazardScore("flood", flood[0], flood[1], flood_rescaled),
        "hurricane": HazardScore("hurricane", hurricane[0], hurricane[1], hurricane_rescaled),
        "heat": HazardScore("heat", heat[0], heat[1], heat_rescaled),
        "severe_storm": HazardScore("severe_storm", severe[0], severe[1], severe_rescaled),
    }
    overall_parts = [
        _direct_part(f"{name}_score", hazards[name].score, OVERALL_WEIGHTS[name], "Deterministic scorer", freshness, "high")
        for name in HAZARD_ORDER
    ]
    overall, overall_drivers, overall_rescaled = _combine(overall_parts)
    hazards["overall"] = HazardScore("overall", overall, overall_drivers, overall_rescaled)

    uncertainties: list[str] = []
    if any(item.rescaled for item in hazards.values()):
        uncertainties.append("A missing input was dropped and the remaining weights for that score were rescaled to 1.")
    if metrics.snow_valid_days and metrics.snow_valid_days < 300:
        uncertainties.append(f"Only {metrics.snow_valid_days} days had a snowfall value in this window.")
    if not baseline.hurricane_published:
        uncertainties.append(f"FEMA publishes no hurricane score for {baseline.county} County. The hurricane baseline is 0.")
    if not baseline.coastal_flood_published:
        uncertainties.append(
            f"FEMA publishes no coastal-flood score for {baseline.county} County. The flood baseline uses inland flood only."
        )
    if alerts.fallback or not alerts.available:
        uncertainties.append(alerts.source.uncertainty_note)
    if daily.fallback:
        uncertainties.append(daily.source.uncertainty_note)

    sources = [daily.source, alerts.source, _baseline_source(baseline)]
    return HubReport(
        hub_id=hub.hub_id,
        name=hub.name,
        region=hub.region,
        overall_score=overall,
        hazards=hazards,
        metrics=metrics,
        sources=sources,
        uncertainties=uncertainties,
        window_label=window_label,
        window_start=window_start,
        window_end=window_end,
        weather_fallback=daily.fallback,
        alerts_fallback=alerts.fallback or not alerts.available,
        hurricane_published=baseline.hurricane_published,
        coastal_flood_published=baseline.coastal_flood_published,
        active_events=list(alerts.events),
    )


def _finish(parts: list[_Part]) -> tuple[tuple[float, list[Driver]], bool]:
    score, drivers, rescaled = _combine(parts)
    return (score, drivers), rescaled


def _baseline_source(baseline: HazardBaseline) -> SourceRef:
    return SourceRef(
        name="FEMA National Risk Index county snapshot",
        source_type="static_public_dataset",
        retrieved_at=baseline.retrieved_at,
        uncertainty_note=baseline.source_note,
    )


def metric_payload(report: HubReport, metric: str) -> dict | None:
    metrics = report.metrics
    catalog = {
        "snowfall_day_pct": ("Days with snowfall", metrics.snow_days_pct, metrics.snow_days, metrics.snow_valid_days),
        "freeze_day_pct": ("Days at or below 32 F", metrics.freeze_days_pct, metrics.freeze_days, metrics.freeze_valid_days),
        "heat_day_pct": ("Days at or above 95 F", metrics.heat_days_pct, metrics.heat_days, metrics.temp_max_valid_days),
        "extreme_heat_day_pct": ("Days at or above 100 F", metrics.extreme_heat_days_pct, metrics.extreme_heat_days, metrics.temp_max_valid_days),
        "heavy_rain_day_pct": ("Days with at least 1 inch of precipitation", metrics.heavy_rain_days_pct, metrics.heavy_rain_days, metrics.precip_valid_days),
        "very_heavy_rain_day_pct": ("Days with at least 2 inches of precipitation", metrics.very_heavy_rain_days_pct, metrics.very_heavy_rain_days, metrics.precip_valid_days),
    }
    if metric not in catalog:
        return None
    label, value, numerator, denominator = catalog[metric]
    return {
        "name": metric,
        "label": label,
        "hub_id": report.hub_id,
        "hub_name": report.name,
        "value": None if value is None else round(value, 2),
        "numerator": numerator,
        "denominator": denominator,
        "unit": "percent",
    }
