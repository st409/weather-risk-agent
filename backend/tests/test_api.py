"""Chat and score endpoints against the offline climatology."""

import re

import pytest
from fastapi.testclient import TestClient

from pydantic import ValidationError

from app.agent.composer import apply_model_prose
from app.agent.llm import answer_user_message, intent_user_message, schema_retries
from app.agent.schemas import AnswerDraft, AnswerResult, IntentResult
from app.main import create_app


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


def test_health_and_hub_list(client):
    health = client.get("/api/health")
    assert health.status_code == 200
    assert health.json()["hub_count"] == 10
    hubs = client.get("/api/hubs")
    assert hubs.status_code == 200
    assert len(hubs.json()["hubs"]) == 10


def test_demo_conversation(client):
    first = _chat(client, "Which hubs in the Midwest are most exposed to winter disruption?")
    session_id = first["session_id"]
    names = [row["name"] for row in first["evidence"]["rankings"]]
    assert names[0] == "Minneapolis"
    assert "Detroit" in names and "Chicago" in names
    assert "Miami" not in first["answer"]["answer"]
    assert first["intent"]["hazards"] == ["winter"]
    assert first["answer"]["assumptions"]
    assert first["answer"]["sources"]
    assert first["answer"]["uncertainties"]
    winter_scores = [item for item in first["answer"]["scores"] if item["hazard"] == "winter"]
    assert winter_scores and all(item["score"] is not None for item in winter_scores)

    why = _chat(client, "Why is the top one risky?", session_id)
    assert why["intent"]["hub_ids"] == ["minneapolis"]
    assert "Minneapolis" in why["answer"]["answer"]
    assert why["evidence"]["rankings"][0]["drivers"]

    compared = _chat(client, "Compare Miami and Houston in terms of hurricane and flood exposure.")
    assert "Miami" in compared["answer"]["answer"]
    assert "Houston" in compared["answer"]["answer"]
    hazards = {item["hazard"] for item in compared["answer"]["scores"]}
    assert "hurricane" in hazards and "flood" in hazards
    session_id = compared["session_id"]

    floods = _chat(client, "What about floods only?", session_id)
    assert floods["intent"]["hub_ids"] == ["miami", "houston"]
    assert floods["intent"]["hazards"] == ["flood"]
    assert "hurricane" not in {item["hazard"] for item in floods["answer"]["scores"]}
    assert "Miami" in floods["answer"]["answer"] and "Houston" in floods["answer"]["answer"]

    denver = _chat(client, "What percentage of days in Denver last year had snowfall?")
    assert denver["evidence"]["metric"]["value"] is not None
    assert re.search(r"\d+(\.\d+)?%", denver["answer"]["answer"])
    assert denver["answer"]["sources"]

    dallas = _chat(client, "Why is the Dallas hub's weather disruption risk high?")
    assert "Dallas" in dallas["answer"]["answer"]
    assert dallas["answer"]["assumptions"]
    assert dallas["answer"]["uncertainties"]
    assert dallas["evidence"]["rankings"][0]["drivers"]

    phoenix = _chat(client, "How exposed is Phoenix to heat?")
    assert phoenix["intent"]["needs_clarification"] is True
    assert "Phoenix" in phoenix["answer"]["answer"]

    safer = _chat(client, "Which is safer?")
    assert safer["intent"]["needs_clarification"] is True
    assert "lower" in safer["answer"]["answer"]
    assert "disruption" in safer["answer"]["answer"]


def test_direct_score_endpoints(client):
    risk = client.get("/api/hubs/dallas/risk")
    assert risk.status_code == 200
    body = risk.json()
    assert body["hub_id"] == "dallas"
    assert body["overall_score"] is not None
    assert body["sources"]

    missing = client.get("/api/hubs/phoenix/risk")
    assert missing.status_code == 404

    compared = client.get("/api/compare", params={"hub_ids": "miami,houston", "hazards": "hurricane,flood"})
    assert compared.status_code == 200
    ids = {hub["hub_id"] for hub in compared.json()["hubs"]}
    assert ids == {"miami", "houston"}


def test_normal_answers_name_scope_without_a_long_dump(client):
    denver = _chat(client, "How many days did it snow in Denver last year?")
    text = denver["answer"]["answer"]
    assert "Denver hub (Mountain region)" in text
    assert "1 cm" in text
    assert "Open-Meteo" in text or "stand-in" in text
    assert "risk score" not in text.lower()
    assert "/100" not in text
    assert "hurricane" not in text.lower()
    assert re.search(r"\d+(\.\d+)?%", text)
    asked = "how many days did it snow"
    assert all(asked not in item.lower() for item in denver["answer"]["follow_up_questions"])
    detail = _chat(client, "Show the sources", denver["session_id"])
    assert detail["evidence"]["detail"] == "sources"
    assert detail["answer"]["sources"]


def test_schema_failure_falls_back_without_inventing_a_score():
    parsed, attempts = schema_retries(lambda _error: "not-json", IntentResult)
    assert parsed is None
    assert attempts == 2
    # A deterministic answer is still a valid AnswerResult, which is what the
    # orchestrator returns when the model cannot.
    sample = AnswerResult(
        answer="Minneapolis winter score is 70.0. Sources: offline stand-in.",
        key_findings=["Minneapolis winter score: 70.0"],
        scores=[{"hub_id": "minneapolis", "hub_name": "Minneapolis", "hazard": "winter", "score": 70.0, "unit": "index"}],
        assumptions=["Scores are computed in Python."],
        uncertainties=["Model output failed schema validation twice."],
        sources=[
            {
                "name": "Offline climatology stand-in",
                "source_type": "static_public_dataset",
                "retrieved_at": "2026-11-23T00:00:00+00:00",
                "uncertainty_note": "Stand-in data.",
            }
        ],
        follow_up_questions=["What about floods only?"],
    )
    assert AnswerResult.model_validate(sample.model_dump()).scores[0].score == 70.0


def test_second_call_cannot_replace_computed_scores():
    with pytest.raises(ValidationError):
        AnswerDraft.model_validate(
            {
                "answer": "Miami flood score is 12.",
                "follow_up_questions": [],
                "scores": [{"hub_id": "miami", "score": 12}],
            }
        )
    base = AnswerResult(
        answer="Miami flood score is 78.0 and Houston flood score is 80.0.",
        key_findings=["Miami flood: 78.0", "Houston flood: 80.0"],
        scores=[
            {"hub_id": "miami", "hub_name": "Miami", "hazard": "flood", "score": 78.0, "unit": "index"},
            {"hub_id": "houston", "hub_name": "Houston", "hazard": "flood", "score": 80.0, "unit": "index"},
        ],
        assumptions=["Scores are computed in Python."],
        uncertainties=["County baselines are saturated."],
        sources=[
            {
                "name": "FEMA National Risk Index",
                "source_type": "static_public_dataset",
                "retrieved_at": "2026-11-23",
                "uncertainty_note": "Snapshot.",
            }
        ],
        follow_up_questions=["What about heat only?"],
    )
    drafted = AnswerDraft(
        answer="The briefing stays with the hubs from the previous turn and talks about flood exposure only.",
        follow_up_questions=["What will the weather be next year?"],
    )
    shown = apply_model_prose(base, drafted, None)
    assert [item.score for item in shown.scores] == [78.0, 80.0]
    assert shown.key_findings == ["Miami flood: 78.0", "Houston flood: 80.0"]
    assert shown.follow_up_questions == ["What about heat only?"]
    brief = answer_user_message(
        "What about floods only?",
        [item.model_dump() for item in base.scores],
        {"rankings": []},
        "Houston flood is 80.0.",
        {"previous_user_message": "Compare Miami and Houston", "hub_ids": ["miami", "houston"]},
    )
    assert "read_only_scores" in brief
    assert "factual_skeleton" in brief
    assert "previous_turn" in brief
    assert "78.0" in brief
    prompt = intent_user_message(
        "ומה לגבי שיטפונות בלבד?",
        '{"previous_user_message": "Compare Miami and Houston", "hub_ids": ["miami", "houston"]}',
        "miami: Miami, South",
    )
    assert "previous_turn" in prompt
    assert "miami" in prompt
    assert "ומה לגבי שיטפונות בלבד?" in prompt
