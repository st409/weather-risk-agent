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


def allowed_origins() -> list[str]:
    """Local dev origins, plus any comma-separated ALLOWED_ORIGINS from the host."""
    origins = ["http://127.0.0.1:5173", "http://localhost:5173"]
    raw = os.getenv("ALLOWED_ORIGINS", "")
    for part in raw.split(","):
        origin = part.strip().rstrip("/")
        if origin and origin not in origins:
            origins.append(origin)
    return origins


def admin_token() -> str:
    """Secret for POST /api/admin/refresh-scores. Empty means the route does not exist."""
    return os.getenv("ADMIN_TOKEN", "").strip()


def chat_rate_limit_per_minute() -> int:
    """Per-IP cap on POST /api/chat. 0 turns the limit off (tests and evals)."""
    raw = os.getenv("CHAT_RATE_LIMIT_PER_MIN", "30").strip()
    try:
        return max(0, int(raw))
    except ValueError:
        return 30


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
