"""SQLite reads and writes. Each call opens its own connection under a lock."""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

from app.config import data_mode, database_path
from app.db.models import SCHEMA_SQL
from app.risk.hazards import load_baselines
from app.risk.hubs import HUBS
from app.risk.types import AlertBatch, DailyObs

_LOCK = threading.Lock()


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _connect() -> sqlite3.Connection:
    path = database_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def db():
    with _LOCK:
        conn = _connect()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


def init_db() -> None:
    with db() as conn:
        conn.executescript(SCHEMA_SQL)


def seed_reference_data() -> None:
    init_db()
    with db() as conn:
        for hub in HUBS:
            conn.execute(
                """
                INSERT INTO hubs (
                    hub_id, name, city, state, region, lat, lon, county, fips,
                    timezone, surge_exposed, business_importance_weight
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(hub_id) DO UPDATE SET
                    name=excluded.name,
                    city=excluded.city,
                    state=excluded.state,
                    region=excluded.region,
                    lat=excluded.lat,
                    lon=excluded.lon,
                    county=excluded.county,
                    fips=excluded.fips,
                    timezone=excluded.timezone,
                    surge_exposed=excluded.surge_exposed,
                    business_importance_weight=excluded.business_importance_weight
                """,
                (
                    hub.hub_id,
                    hub.name,
                    hub.city,
                    hub.state,
                    hub.region,
                    hub.lat,
                    hub.lon,
                    hub.county,
                    hub.fips,
                    hub.timezone,
                    1 if hub.surge_exposed else 0,
                    hub.business_importance_weight,
                ),
            )
        for baseline in load_baselines().values():
            conn.execute(
                """
                INSERT INTO hazard_baselines (
                    hub_id, county, fips, winter_weather, inland_flood, coastal_flood,
                    coastal_flood_published, hurricane, hurricane_published, heat_wave,
                    tornado, lightning, hail, strong_wind, retrieved_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(hub_id) DO UPDATE SET
                    county=excluded.county,
                    fips=excluded.fips,
                    winter_weather=excluded.winter_weather,
                    inland_flood=excluded.inland_flood,
                    coastal_flood=excluded.coastal_flood,
                    coastal_flood_published=excluded.coastal_flood_published,
                    hurricane=excluded.hurricane,
                    hurricane_published=excluded.hurricane_published,
                    heat_wave=excluded.heat_wave,
                    tornado=excluded.tornado,
                    lightning=excluded.lightning,
                    hail=excluded.hail,
                    strong_wind=excluded.strong_wind,
                    retrieved_at=excluded.retrieved_at
                """,
                (
                    baseline.hub_id,
                    baseline.county,
                    baseline.fips,
                    baseline.winter_weather,
                    baseline.inland_flood,
                    baseline.coastal_flood,
                    1 if baseline.coastal_flood_published else 0,
                    baseline.hurricane,
                    1 if baseline.hurricane_published else 0,
                    baseline.heat_wave,
                    baseline.tornado,
                    baseline.lightning,
                    baseline.hail,
                    baseline.strong_wind,
                    baseline.retrieved_at,
                ),
            )


def list_hubs() -> list[dict]:
    with db() as conn:
        rows = conn.execute("SELECT * FROM hubs ORDER BY name").fetchall()
    return [dict(row) for row in rows]


def save_daily_window(
    hub_id: str,
    window_start: str,
    window_end: str,
    days: list[DailyObs],
    source_name: str,
    retrieved_at: str,
    uncertainty_note: str,
    fallback: bool,
) -> None:
    with db() as conn:
        conn.execute("DELETE FROM daily_observations WHERE hub_id = ? AND obs_date >= ? AND obs_date <= ?", (hub_id, window_start, window_end))
        conn.executemany(
            """
            INSERT INTO daily_observations (
                hub_id, obs_date, temp_max_f, temp_min_f, precip_in, snowfall_cm, wind_gust_mph
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (hub_id, day.date, day.temp_max_f, day.temp_min_f, day.precip_in, day.snowfall_cm, day.wind_gust_mph)
                for day in days
            ],
        )
        conn.execute(
            """
            INSERT INTO observation_windows (
                hub_id, window_start, window_end, source_name, retrieved_at, uncertainty_note, fallback
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(hub_id, window_start, window_end) DO UPDATE SET
                source_name=excluded.source_name,
                retrieved_at=excluded.retrieved_at,
                uncertainty_note=excluded.uncertainty_note,
                fallback=excluded.fallback
            """,
            (hub_id, window_start, window_end, source_name, retrieved_at, uncertainty_note, 1 if fallback else 0),
        )


def get_daily_window(hub_id: str, window_start: str, window_end: str) -> dict | None:
    with db() as conn:
        header = conn.execute(
            """
            SELECT * FROM observation_windows
            WHERE hub_id = ? AND window_start = ? AND window_end = ?
            """,
            (hub_id, window_start, window_end),
        ).fetchone()
        if header is None:
            return None
        rows = conn.execute(
            """
            SELECT obs_date, temp_max_f, temp_min_f, precip_in, snowfall_cm, wind_gust_mph
            FROM daily_observations
            WHERE hub_id = ? AND obs_date >= ? AND obs_date <= ?
            ORDER BY obs_date
            """,
            (hub_id, window_start, window_end),
        ).fetchall()
    return {
        "source_name": header["source_name"],
        "retrieved_at": header["retrieved_at"],
        "uncertainty_note": header["uncertainty_note"],
        "fallback": bool(header["fallback"]),
        "days": [
            {
                "date": row["obs_date"],
                "temp_max_f": row["temp_max_f"],
                "temp_min_f": row["temp_min_f"],
                "precip_in": row["precip_in"],
                "snowfall_cm": row["snowfall_cm"],
                "wind_gust_mph": row["wind_gust_mph"],
            }
            for row in rows
        ],
    }


def delete_scores_for_window(hub_id: str, window_start: str, window_end: str) -> None:
    with db() as conn:
        conn.execute(
            "DELETE FROM risk_scores WHERE hub_id = ? AND window_start = ? AND window_end = ?",
            (hub_id, window_start, window_end),
        )


def save_alert_cache(hub_id: str, batch: AlertBatch) -> None:
    with db() as conn:
        conn.execute(
            """
            INSERT INTO alert_cache (
                hub_id, retrieved_at, available, alert_count, weighted_json, events_json, source_name, uncertainty_note
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(hub_id) DO UPDATE SET
                retrieved_at=excluded.retrieved_at,
                available=excluded.available,
                alert_count=excluded.alert_count,
                weighted_json=excluded.weighted_json,
                events_json=excluded.events_json,
                source_name=excluded.source_name,
                uncertainty_note=excluded.uncertainty_note
            """,
            (
                hub_id,
                batch.source.retrieved_at,
                1 if batch.available else 0,
                batch.count,
                json.dumps(batch.weighted),
                json.dumps(batch.events),
                batch.source.name,
                batch.source.uncertainty_note,
            ),
        )


def get_alert_cache(hub_id: str, max_age_minutes: int) -> dict | None:
    with db() as conn:
        row = conn.execute("SELECT * FROM alert_cache WHERE hub_id = ?", (hub_id,)).fetchone()
    if row is None:
        return None
    retrieved = datetime.fromisoformat(row["retrieved_at"])
    if retrieved.tzinfo is None:
        retrieved = retrieved.replace(tzinfo=timezone.utc)
    age = datetime.now(timezone.utc) - retrieved
    if age > timedelta(minutes=max_age_minutes):
        return None
    return {
        "retrieved_at": row["retrieved_at"],
        "available": bool(row["available"]),
        "count": row["alert_count"],
        "weighted": json.loads(row["weighted_json"]),
        "events": json.loads(row["events_json"]),
        "source_name": row["source_name"],
        "uncertainty_note": row["uncertainty_note"],
    }


def save_score_snapshot(report_public: dict) -> None:
    if report_public.get("weather_fallback") and data_mode() == "live":
        return  # a stand-in is never stored as a live score
    hazards = {item["hazard"]: item["score"] for item in report_public.get("hazard_scores", [])}
    with db() as conn:
        conn.execute(
            """
            INSERT INTO risk_scores (
                hub_id, computed_at, window_start, window_end, window_label, overall_score,
                winter_score, flood_score, hurricane_score, heat_score, severe_storm_score, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                report_public["hub_id"],
                _now(),
                report_public["window_start"],
                report_public["window_end"],
                report_public["window_label"],
                report_public["overall_score"],
                hazards.get("winter", 0),
                hazards.get("flood", 0),
                hazards.get("hurricane", 0),
                hazards.get("heat", 0),
                hazards.get("severe_storm", 0),
                json.dumps(report_public),
            ),
        )


def ensure_session(session_id: str) -> None:
    stamp = _now()
    with db() as conn:
        conn.execute(
            """
            INSERT INTO chat_sessions (session_id, created_at, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(session_id) DO UPDATE SET updated_at=excluded.updated_at
            """,
            (session_id, stamp, stamp),
        )


def add_message(session_id: str, role: str, content: str, payload: dict | None) -> None:
    with db() as conn:
        conn.execute(
            """
            INSERT INTO chat_messages (session_id, role, content, payload_json, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (session_id, role, content, None if payload is None else json.dumps(payload), _now()),
        )
        conn.execute("UPDATE chat_sessions SET updated_at = ? WHERE session_id = ?", (_now(), session_id))


def get_context(session_id: str) -> dict:
    with db() as conn:
        row = conn.execute("SELECT context_json FROM session_context WHERE session_id = ?", (session_id,)).fetchone()
    if row is None:
        return {}
    return json.loads(row["context_json"])


def save_context(session_id: str, context: dict) -> None:
    with db() as conn:
        conn.execute(
            """
            INSERT INTO session_context (session_id, context_json, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(session_id) DO UPDATE SET
                context_json=excluded.context_json,
                updated_at=excluded.updated_at
            """,
            (session_id, json.dumps(context), _now()),
        )
