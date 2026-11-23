"""Environment and filesystem paths. Values are read when called, not frozen at import."""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND_ROOT = REPO_ROOT / "backend"

load_dotenv(REPO_ROOT / ".env", override=False)


def data_mode() -> str:
    mode = os.getenv("WEATHER_DATA_MODE", "live").strip().lower()
    return mode or "live"


def llm_disabled() -> bool:
    return os.getenv("WEATHER_RISK_DISABLE_LLM", "").strip() == "1"


def openai_api_key() -> str:
    return os.getenv("OPENAI_API_KEY", "").strip()


def openai_model() -> str:
    return os.getenv("OPENAI_MODEL", "").strip() or "gpt-4o-mini"


def database_path() -> Path:
    raw = os.getenv("DATABASE_PATH", "").strip()
    if not raw:
        return BACKEND_ROOT / "data" / "weather_risk.db"
    path = Path(raw)
    if not path.is_absolute():
        path = REPO_ROOT / path
    return path


def nws_user_agent() -> str:
    return os.getenv("NWS_USER_AGENT", "").strip() or "weather-risk-agent/0.1 (local demo)"


def alert_cache_minutes() -> int:
    raw = os.getenv("ALERT_CACHE_MINUTES", "30").strip()
    try:
        return max(1, int(raw))
    except ValueError:
        return 30
