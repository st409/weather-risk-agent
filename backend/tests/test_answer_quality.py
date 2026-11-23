"""Answer wording, one-decimal evidence, and stale snow-day cache."""

from datetime import date

import pytest
from fastapi.testclient import TestClient

from app.agent.composer import _draft_is_usable
from app.agent.llm import ANSWER_SYSTEM
from app.agent.schemas import AnswerResult, IntentResult
from app.db.repository import get_daily_window, latest_score, save_daily_window, save_score_snapshot, seed_reference_data
from app.main import create_app
from app.risk.engine import recompute_stale_scores
from app.risk.hubs import HUB_BY_ID
from app.risk.types import DailyObs
from app.risk.weather import load_daily


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("WEATHER_DATA_MODE", "fixture")
    monkeypatch.setenv("WEATHER_RISK_DISABLE_LLM", "1")
    with TestClient(create_app()) as test_client:
        yield test_client


def _chat(client: TestClient, message: str, session_id: str | None = None) -> dict:
    response = client.post("/api/chat", json={"session_id": session_id, "message": message})
    assert response.status_code == 200, response.text
    body = response.json()
    AnswerResult.model_validate(body["answer"])
    IntentResult.model_validate(body["intent"])
    return body


def test_prompt_keeps_every_contributor():
    assert "two or three" not in ANSWER_SYSTEM
    assert "every contributor" in ANSWER_SYSTEM
    assert ">= 1 cm" in ANSWER_SYSTEM


def test_word_cap_accepts_a_full_contributor_draft():
    long_enough = " ".join(["briefing"] * 180)
    assert _draft_is_usable(long_enough, {}, [], "")
    assert not _draft_is_usable(" ".join(["briefing"] * 230), {}, [], "")
    skeleton = "Contributors, largest first: heat (4.0) and winter (10.0). These points sum to 14.0."
    assert _draft_is_usable("heat (4.0) and winter (10.0) stay in the briefing.", {}, [], skeleton)
    assert not _draft_is_usable("Only winter (10.0) is mentioned in this briefing.", {}, [], skeleton)


def test_snow_answer_leads_with_count_threshold_and_source(client):
    body = _chat(client, "How many days did it snow in Denver last year?")
    text = body["answer"]["answer"]
    assert text.startswith("Denver hub (Mountain region) had")
    assert ">= 1 cm" in text
    assert "Open-Meteo" in text
    assert "/100" not in text
    assert "risk score" not in text.lower()
    joined = " ".join(body["answer"]["uncertainties"]).lower()
    assert "hurricane" not in joined
    assert "one year" in joined
    for question in body["answer"]["follow_up_questions"]:
        low = question.lower()
        assert "miami" not in low and "houston" not in low
        assert "how many days did it snow" not in low


def test_explain_lists_heat_and_skips_unpublished_hurricane(client):
    body = _chat(client, "Why is the Denver hub's weather disruption risk high?")
    text = body["answer"]["answer"]
    assert "Denver hub (Mountain region)" in text
    assert "heat" in text.lower()
    assert "hurricane" not in text.lower()
    assert "Contributors, largest first" in text
    row = body["evidence"]["rankings"][0]
    assert row["overall_score"] == round(row["overall_score"], 1)
    for driver in row["drivers"]:
        assert driver["contribution"] == round(driver["contribution"], 1)
    for item in body["answer"]["scores"]:
        assert item["score"] == round(item["score"], 1)
    notes = " ".join(body["answer"]["uncertainties"]).lower()
    assert "hurricane" not in notes
    assert "coastal" not in notes
    assert "single year" in notes or "one year" in notes
    followups = " ".join(body["answer"]["follow_up_questions"]).lower()
    assert "miami" not in followups and "houston" not in followups
    assert "why is denver" not in followups


def test_rank_explains_second_and_third(client):
    body = _chat(client, "Which hubs in the Midwest are most exposed to winter disruption?")
    text = body["answer"]["answer"]
    assert "Minneapolis hub (Midwest region)" in text
    assert text.count("is lower") >= 2
    assert "Chicago hub (Midwest region)" in text
    assert "Detroit hub (Midwest region)" in text


def test_compare_ties_and_keeps_each_baseline(client):
    body = _chat(client, "Compare Miami and Houston in terms of hurricane and flood exposure.")
    text = body["answer"]["answer"]
    assert "Miami hub (South region)" in text
    assert "Houston hub (South region)" in text
    assert "practically tied" in text
    assert "does not separate" in text
    assert "both" not in text.lower()
    scores = [item for item in body["answer"]["scores"] if item["hazard"] == "hurricane"]
    assert abs(scores[0]["score"] - scores[1]["score"]) <= 1.0


def test_stale_cache_note_and_score_use_one_centimeter(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "cache.db"))
    monkeypatch.setenv("WEATHER_DATA_MODE", "live")
    monkeypatch.setattr(
        "app.risk.weather._fetch_nws",
        lambda _hub: (
            {"winter": 0.0, "flood": 0.0, "hurricane": 0.0, "heat": 0.0, "severe_storm": 0.0},
            [],
            "2026-11-23T00:00:00+00:00",
        ),
    )
    seed_reference_data()
    hub = HUB_BY_ID["denver"]
    start, end = "2025-01-01", "2025-01-02"
    save_daily_window(
        hub.hub_id,
        start,
        end,
        [
            DailyObs(start, 40.0, 20.0, 0.0, 0.4, 10.0),
            DailyObs(end, 41.0, 21.0, 0.0, 1.5, 12.0),
        ],
        "Open-Meteo Historical Weather API",
        "2026-11-23T00:00:00+00:00",
        "A snow day is any daily snowfall above 0 cm.",
        False,
    )
    save_score_snapshot(
        {
            "hub_id": "denver",
            "window_start": start,
            "window_end": end,
            "window_label": "two stale days",
            "overall_score": 10,
            "hazard_scores": [{"hazard": "winter", "score": 10}],
            "metrics": {"snow_days": 2},
            "sources": [{"uncertainty_note": "snowfall above 0 cm"}],
        }
    )
    batch = load_daily(hub, date.fromisoformat(start), date.fromisoformat(end))
    assert "above 0" not in batch.source.uncertainty_note.lower()
    assert ">= 1 cm" in batch.source.uncertainty_note
    stored = get_daily_window("denver", start, end)
    assert stored is not None
    assert ">= 1 cm" in stored["uncertainty_note"]
    assert latest_score("denver") is None

    save_score_snapshot(
        {
            "hub_id": "denver",
            "window_start": start,
            "window_end": end,
            "window_label": "two stale days",
            "overall_score": 10,
            "hazard_scores": [{"hazard": "winter", "score": 10}],
            "metrics": {"snow_days": 2},
        }
    )
    refreshed = recompute_stale_scores()
    assert refreshed == 1
    latest = latest_score("denver")
    assert latest is not None
    assert latest["snow_day_rule"] == "gte_1cm"
    assert latest["metrics"]["snow_days"] == 1
