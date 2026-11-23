"""SQLite schema for hubs, cached weather, scores, and chat memory."""

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS hubs (
    hub_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    city TEXT NOT NULL,
    state TEXT NOT NULL,
    region TEXT NOT NULL,
    lat REAL NOT NULL,
    lon REAL NOT NULL,
    county TEXT NOT NULL,
    fips TEXT NOT NULL,
    timezone TEXT NOT NULL,
    surge_exposed INTEGER NOT NULL,
    business_importance_weight REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS hazard_baselines (
    hub_id TEXT PRIMARY KEY,
    county TEXT NOT NULL,
    fips TEXT NOT NULL,
    winter_weather REAL NOT NULL,
    inland_flood REAL NOT NULL,
    coastal_flood REAL NOT NULL,
    coastal_flood_published INTEGER NOT NULL,
    hurricane REAL NOT NULL,
    hurricane_published INTEGER NOT NULL,
    heat_wave REAL NOT NULL,
    tornado REAL NOT NULL,
    lightning REAL NOT NULL,
    hail REAL NOT NULL,
    strong_wind REAL NOT NULL,
    retrieved_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS daily_observations (
    hub_id TEXT NOT NULL,
    obs_date TEXT NOT NULL,
    temp_max_f REAL,
    temp_min_f REAL,
    precip_in REAL,
    snowfall_cm REAL,
    wind_gust_mph REAL,
    PRIMARY KEY (hub_id, obs_date)
);

CREATE TABLE IF NOT EXISTS observation_windows (
    hub_id TEXT NOT NULL,
    window_start TEXT NOT NULL,
    window_end TEXT NOT NULL,
    source_name TEXT NOT NULL,
    retrieved_at TEXT NOT NULL,
    uncertainty_note TEXT NOT NULL,
    fallback INTEGER NOT NULL,
    PRIMARY KEY (hub_id, window_start, window_end)
);

CREATE TABLE IF NOT EXISTS alert_cache (
    hub_id TEXT PRIMARY KEY,
    retrieved_at TEXT NOT NULL,
    available INTEGER NOT NULL,
    alert_count INTEGER NOT NULL,
    weighted_json TEXT NOT NULL,
    events_json TEXT NOT NULL,
    source_name TEXT NOT NULL,
    uncertainty_note TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS risk_scores (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    hub_id TEXT NOT NULL,
    computed_at TEXT NOT NULL,
    window_start TEXT NOT NULL,
    window_end TEXT NOT NULL,
    window_label TEXT NOT NULL,
    overall_score REAL NOT NULL,
    winter_score REAL NOT NULL,
    flood_score REAL NOT NULL,
    hurricane_score REAL NOT NULL,
    heat_score REAL NOT NULL,
    severe_storm_score REAL NOT NULL,
    payload_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS chat_sessions (
    session_id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS chat_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    payload_json TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS session_context (
    session_id TEXT PRIMARY KEY,
    context_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""
