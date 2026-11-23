"""Shared records passed between weather, scoring, and the API."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class DailyObs:
    date: str
    temp_max_f: float | None
    temp_min_f: float | None
    precip_in: float | None
    snowfall_cm: float | None
    wind_gust_mph: float | None


@dataclass
class SourceRef:
    name: str
    source_type: str
    retrieved_at: str
    uncertainty_note: str

    def public(self) -> dict:
        return {
            "name": self.name,
            "source_type": self.source_type,
            "retrieved_at": self.retrieved_at,
            "uncertainty_note": self.uncertainty_note,
        }


@dataclass
class DailyBatch:
    days: list[DailyObs]
    source: SourceRef
    fallback: bool


@dataclass
class AlertBatch:
    weighted: dict[str, float]
    available: bool
    count: int
    events: list[str]
    source: SourceRef
    fallback: bool


@dataclass
class HubAlerts:
    """Active alerts for one hub. Used by alert questions, which load no weather history."""

    hub_id: str
    name: str
    region: str
    batch: AlertBatch


@dataclass
class Driver:
    metric_name: str
    value: float | None
    weight: float
    contribution: float
    source: str
    freshness: str
    confidence: str

    def public(self) -> dict:
        return {
            "metric_name": self.metric_name,
            "value": None if self.value is None else round(self.value, 2),
            "weight": round(self.weight, 4),
            "contribution": round(self.contribution, 2),
            "source": self.source,
            "freshness": self.freshness,
            "confidence": self.confidence,
        }


@dataclass
class HazardScore:
    hazard: str
    score: float
    drivers: list[Driver]
    rescaled: bool


@dataclass
class WeatherMetrics:
    snow_days: int = 0
    snow_valid_days: int = 0
    snow_days_pct: float | None = None
    freeze_days: int = 0
    freeze_valid_days: int = 0
    freeze_days_pct: float | None = None
    heavy_rain_days: int = 0
    very_heavy_rain_days: int = 0
    precip_valid_days: int = 0
    heavy_rain_days_pct: float | None = None
    very_heavy_rain_days_pct: float | None = None
    heat_days: int = 0
    extreme_heat_days: int = 0
    temp_max_valid_days: int = 0
    heat_days_pct: float | None = None
    extreme_heat_days_pct: float | None = None
    high_wind_days: int = 0
    wind_valid_days: int = 0
    high_wind_days_pct: float | None = None


@dataclass
class HazardBaseline:
    hub_id: str
    county: str
    state: str
    fips: str
    winter_weather: float
    inland_flood: float
    coastal_flood: float
    hurricane: float
    heat_wave: float
    tornado: float
    lightning: float
    hail: float
    strong_wind: float
    coastal_flood_published: bool
    hurricane_published: bool
    retrieved_at: str
    source_note: str

    @property
    def flood(self) -> float:
        return max(self.inland_flood, self.coastal_flood)

    @property
    def severe(self) -> float:
        return (self.tornado + self.lightning + self.hail + self.strong_wind) / 4.0


@dataclass
class HubReport:
    hub_id: str
    name: str
    region: str
    overall_score: float
    hazards: dict[str, HazardScore]
    metrics: WeatherMetrics
    sources: list[SourceRef]
    uncertainties: list[str] = field(default_factory=list)
    window_label: str = ""
    window_start: str = ""
    window_end: str = ""
    weather_fallback: bool = False
    alerts_fallback: bool = False
    hurricane_published: bool = True
    coastal_flood_published: bool = True
    active_events: list[str] = field(default_factory=list)

    def hazard_value(self, hazard: str) -> float:
        return self.hazards[hazard].score

    def focus_score(self, hazards: list[str]) -> float:
        if not hazards:
            return self.overall_score
        return sum(self.hazard_value(hazard) for hazard in hazards) / len(hazards)
