"""Locks the published score formulas."""

from datetime import date

import pytest

from app.risk.hazards import get_baseline
from app.risk.hubs import HUB_BY_ID
from app.risk.scoring import clamp_0_100, metrics_from_days, normalize, score_hub
from app.risk.types import AlertBatch, DailyBatch, DailyObs, SourceRef, WeatherMetrics
from app.risk.windows import resolve_window


def _source(name: str) -> SourceRef:
    return SourceRef(name=name, source_type="live_api", retrieved_at="2026-11-23T00:00:00+00:00", uncertainty_note="test")


def _alerts(weights: dict[str, float] | None, available: bool = True) -> AlertBatch:
    base = {key: 0.0 for key in ("winter", "flood", "hurricane", "heat", "severe_storm")}
    if weights:
        base.update(weights)
    return AlertBatch(weighted=base, available=available, count=1, events=[], source=_source("National Weather Service active alerts"), fallback=False)


def _daily() -> DailyBatch:
    return DailyBatch(days=[], source=_source("Open-Meteo Historical Weather API"), fallback=False)


def _metrics(**overrides) -> WeatherMetrics:
    metrics = WeatherMetrics(
        snow_days_pct=0,
        snow_valid_days=365,
        freeze_days_pct=0,
        freeze_valid_days=365,
        heavy_rain_days_pct=0,
        very_heavy_rain_days_pct=0,
        precip_valid_days=365,
        heat_days_pct=0,
        extreme_heat_days_pct=0,
        temp_max_valid_days=365,
        high_wind_days_pct=0,
        wind_valid_days=365,
    )
    for key, value in overrides.items():
        setattr(metrics, key, value)
    return metrics


def _score(hub_id: str, metrics: WeatherMetrics, alerts: AlertBatch):
    hub = HUB_BY_ID[hub_id]
    return score_hub(
        hub,
        metrics,
        alerts,
        get_baseline(hub_id),
        _daily(),
        "calendar year 2025",
        "2025-01-01",
        "2025-12-31",
    )


def test_clamp_and_normalize():
    assert clamp_0_100(-4) == 0
    assert clamp_0_100(140) == 100
    assert normalize(35, 35) == 100
    assert normalize(70, 35) == 100
    assert normalize(0, 35) == 0


def test_published_weights_at_the_caps():
    metrics = _metrics(
        snow_days_pct=35,
        freeze_days_pct=60,
        heavy_rain_days_pct=15,
        very_heavy_rain_days_pct=5,
        heat_days_pct=30,
        extreme_heat_days_pct=10,
        high_wind_days_pct=10,
    )
    # Use a synthetic baseline by scoring Miami's real baseline only for the
    # structural path, then check the winter math on a hand-built call below.
    alerts = _alerts({"winter": 5, "flood": 0, "hurricane": 3, "heat": 0, "severe_storm": 5})
    report = _score("miami", metrics, alerts)
    # Miami winter baseline is 0. Caps on snow, freeze, and alerts:
    # 0.40*100 + 0.30*100 + 0.20*0 + 0.10*100 = 80
    assert report.hazards["winter"].score == pytest.approx(80)


def test_hand_example_matches_design_formula():
    """0.4/0.3/0.2/0.1 winter example: snow 35, freeze 60, baseline 50, alerts 5 -> 90."""
    from app.risk.types import HazardBaseline

    hub = HUB_BY_ID["miami"]
    baseline = HazardBaseline(
        hub_id="miami",
        county="Miami-Dade",
        state="FL",
        fips="12086",
        winter_weather=50,
        inland_flood=80,
        coastal_flood=0,
        hurricane=90,
        heat_wave=40,
        tornado=60,
        lightning=60,
        hail=60,
        strong_wind=60,
        coastal_flood_published=True,
        hurricane_published=True,
        retrieved_at="2026-11-23",
        source_note="test",
    )
    metrics = _metrics(
        snow_days_pct=35,
        freeze_days_pct=60,
        heavy_rain_days_pct=15,
        very_heavy_rain_days_pct=5,
        heat_days_pct=30,
        extreme_heat_days_pct=10,
        high_wind_days_pct=10,
    )
    alerts = _alerts({"winter": 5, "flood": 0, "hurricane": 3, "heat": 0, "severe_storm": 5})
    # Miami is surge exposed, so coastal modifier is 100, matching the hand example.
    report = score_hub(hub, metrics, alerts, baseline, _daily(), "calendar year 2025", "2025-01-01", "2025-12-31")
    assert report.hazards["winter"].score == pytest.approx(90)
    assert report.hazards["flood"].score == pytest.approx(78)
    assert report.hazards["hurricane"].score == pytest.approx(93)
    assert report.hazards["heat"].score == pytest.approx(70)
    assert report.hazards["severe_storm"].score == pytest.approx(80)
    assert report.overall_score == pytest.approx(84.1)
    winter_sum = sum(driver.contribution for driver in report.hazards["winter"].drivers)
    assert winter_sum == pytest.approx(90)


def test_missing_alert_rescales_remaining_weights():
    from app.risk.types import HazardBaseline

    baseline = get_baseline("chicago")
    # Replace only by passing alerts unavailable and metrics at the snow/freeze caps.
    metrics = _metrics(snow_days_pct=35, freeze_days_pct=60)
    report = score_hub(
        HUB_BY_ID["chicago"],
        metrics,
        _alerts(None, available=False),
        baseline,
        _daily(),
        "calendar year 2025",
        "2025-01-01",
        "2025-12-31",
    )
    # Chicago winter baseline is 100. Alert dropped.
    # (0.40*100 + 0.30*100 + 0.20*100) / 0.90 = 100
    assert report.hazards["winter"].rescaled is True
    assert report.hazards["winter"].score == pytest.approx(100)


def test_metrics_from_three_days():
    days = [
        DailyObs("2025-01-01", 100, 30, 2.5, 1.0, 45),
        DailyObs("2025-01-02", 70, 40, 0.2, 0.4, 10),
        DailyObs("2025-01-03", None, None, None, None, None),
    ]
    metrics = metrics_from_days(days)
    assert metrics.snow_days == 1
    assert metrics.snow_valid_days == 2
    assert metrics.snow_days_pct == 50
    assert metrics.freeze_days == 1
    assert metrics.heavy_rain_days == 1
    assert metrics.very_heavy_rain_days == 1
    assert metrics.heat_days == 1
    assert metrics.extreme_heat_days == 1
    assert metrics.high_wind_days == 1
    assert metrics.temp_max_valid_days == 2


def test_fema_snapshot_uses_published_scores():
    miami = get_baseline("miami")
    denver = get_baseline("denver")
    assert miami.hurricane == 99.96
    assert miami.hurricane_published is True
    assert denver.hurricane == 0
    assert denver.hurricane_published is False
    assert miami.flood == 99.71 or miami.flood == 99.60 or miami.flood >= 99


def test_inch_snowfall_is_stored_as_centimeters(monkeypatch):
    from app.risk.weather import _fetch_open_meteo, _to_cm

    assert _to_cm(1.0, "inch") == pytest.approx(2.54)
    assert _to_cm(0.4, "inch") == pytest.approx(1.016)
    assert _to_cm(12.0, "cm") == 12.0
    payload = {
        "daily_units": {
            "snowfall_sum": "inch",
            "temperature_2m_max": "°F",
            "temperature_2m_min": "°F",
            "precipitation_sum": "inch",
            "wind_gusts_10m_max": "mp/h",
        },
        "daily": {
            "time": ["2025-01-01", "2025-01-02", "2025-01-03"],
            "snowfall_sum": [1.0, 0.4, 0.2],
            "temperature_2m_max": [40.0, 40.0, 40.0],
            "temperature_2m_min": [20.0, 20.0, 20.0],
            "precipitation_sum": [0.1, 0.1, 0.1],
            "wind_gusts_10m_max": [10.0, 10.0, 10.0],
        },
    }
    seen = {}

    def fake_get(url, params=None, headers=None):
        seen["params"] = params
        return payload

    monkeypatch.setattr("app.risk.weather._get_json", fake_get)
    rows, _retrieved = _fetch_open_meteo(HUB_BY_ID["denver"], date(2025, 1, 1), date(2025, 12, 31))
    assert seen["params"]["timezone"] == "America/Denver"
    assert seen["params"]["precipitation_unit"] == "inch"
    assert seen["params"]["start_date"] == "2025-01-01"
    assert seen["params"]["end_date"] == "2025-12-31"
    assert [row.snowfall_cm for row in rows] == pytest.approx([2.54, 1.016, 0.508])
    metrics = metrics_from_days(rows)
    assert metrics.snow_days == 2
    assert metrics.snow_valid_days == 3


def test_default_window_is_previous_calendar_year():
    start, end, key, label = resolve_window(None, today=date(2026, 11, 23))
    assert key == "last_calendar_year"
    assert start == date(2025, 1, 1)
    assert end == date(2025, 12, 31)
    assert "2025" in label
