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
    monkeypatch.setenv("CHAT_RATE_LIMIT_PER_MIN", "0")
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
    assert phoenix["answer"]["answer"] == (
        "I don't cover Phoenix. This assistant scores 10 regional hubs: "
        "Chicago, Minneapolis, Detroit, Denver, Dallas, Houston, Miami, Atlanta, New York/Newark, and Los Angeles."
    )
    assert "demo" not in phoenix["answer"]["answer"].lower()

    safer = _chat(client, "Which hub is safest?")
    assert safer["intent"]["needs_clarification"] is False
    assert safer["intent"]["intent"] == "rank_hubs"
    assert len(safer["evidence"]["rankings"]) == 10
    assert "riskier" in safer["answer"]["answer"].lower()
    assert "is lower" not in safer["answer"]["answer"].lower()
    assert "Scores:" in safer["answer"]["answer"]
    assert "disruption" in safer["answer"]["answer"].lower()


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
    assert text.startswith("Denver had")
    assert "hub (" not in text
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
    parsed = schema_retries(lambda _error: "not-json", IntentResult)
    assert parsed is None
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


def test_compare_ranks_by_the_requested_hazards(client):
    compared = client.get("/api/compare", params={"hub_ids": "miami,houston", "hazards": "hurricane,flood"})
    assert compared.status_code == 200
    body = compared.json()
    assert body["hazards"] == ["hurricane", "flood"]
    focus = []
    for hub in body["hubs"]:
        scores = {item["hazard"]: item["score"] for item in hub["hazard_scores"]}
        expected = round((scores["hurricane"] + scores["flood"]) / 2, 2)
        assert hub["focus_score"] == expected
        focus.append(hub["focus_score"])
    assert focus == sorted(focus, reverse=True)
    unknown = client.get("/api/compare", params={"hub_ids": "miami,houston", "hazards": "blizzard"})
    assert unknown.status_code == 400


def test_admin_refresh_requires_the_token(client, monkeypatch):
    monkeypatch.setenv("ADMIN_TOKEN", "")
    assert client.post("/api/admin/refresh-scores").status_code == 404
    monkeypatch.setenv("ADMIN_TOKEN", "secret")
    assert client.post("/api/admin/refresh-scores").status_code == 403
    assert client.post("/api/admin/refresh-scores", headers={"X-Admin-Token": "nope"}).status_code == 403
    refreshed = client.post("/api/admin/refresh-scores", headers={"X-Admin-Token": "secret"})
    assert refreshed.status_code == 200
    assert refreshed.json()["refreshed"] == 10


def test_chat_rejects_an_empty_or_huge_message_and_limits_repeats(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "limit.db"))
    monkeypatch.setenv("WEATHER_DATA_MODE", "fixture")
    monkeypatch.setenv("WEATHER_RISK_DISABLE_LLM", "1")
    monkeypatch.setenv("CHAT_RATE_LIMIT_PER_MIN", "0")
    with TestClient(create_app()) as open_client:
        assert open_client.post("/api/chat", json={"message": ""}).status_code == 422
        assert open_client.post("/api/chat", json={"message": "x" * 501}).status_code == 422
    monkeypatch.setenv("CHAT_RATE_LIMIT_PER_MIN", "2")
    with TestClient(create_app()) as limited:
        assert limited.post("/api/chat", json={"message": "hello"}).status_code == 200
        assert limited.post("/api/chat", json={"message": "hello"}).status_code == 200
        blocked = limited.post("/api/chat", json={"message": "hello"})
    assert blocked.status_code == 429
    assert blocked.headers["retry-after"]


def test_chicago_alerts_do_not_load_a_year_of_weather(client, monkeypatch):
    def fail_weather(*_args, **_kwargs):
        raise AssertionError("daily weather should not load for an alerts question")

    monkeypatch.setattr("app.risk.engine.load_daily", fail_weather)
    body = _chat(client, "alerts in Chicago")
    assert body["intent"]["intent"] == "alerts"
    assert body["intent"]["hub_ids"] == ["chicago"]
    assert "Chicago" in body["answer"]["answer"]
    assert "Scores:" not in body["answer"]["answer"]


def _names_in_order(text: str, names: list[str]) -> bool:
    position = -1
    for name in names:
        found = text.find(name)
        if found < position:
            return False
        position = found
    return True


def test_lowest_after_a_ranking_still_names_los_angeles(client):
    ranked = _chat(client, "Rank all hubs by weather risk")
    lowest = _chat(client, "Which hub has the lowest weather risk?", ranked["session_id"])
    _assert_full_safety_ranking(lowest)
    highest = _chat(client, "Which hub has the highest weather risk?")
    why = _chat(client, "why?", highest["session_id"])
    safest = _chat(client, "Which hub is the safest?", why["session_id"])
    _assert_full_safety_ranking(safest)


def _assert_full_safety_ranking(body: dict) -> None:
    assert body["intent"]["intent"] == "rank_hubs"
    assert body["intent"]["hub_ids"] == []
    assert body["evidence"]["safety_framing"] is True
    names = [row["name"] for row in body["evidence"]["rankings"]]
    assert len(names) == 10
    assert names[0] == "Los Angeles"
    text = body["answer"]["answer"]
    assert text.startswith("Los Angeles is the least exposed")
    assert "Detroit is the least exposed" not in text


def test_safer_after_why_stays_on_the_compared_pair(client):
    compared = _chat(client, "Compare Dallas and Atlanta")
    why = _chat(client, "why?", compared["session_id"])
    safer = _chat(client, "which is safer?", why["session_id"])
    assert safer["intent"]["intent"] == "compare_hubs"
    assert safer["intent"]["hub_ids"] == ["dallas", "atlanta"]
    assert safer["evidence"]["safety_framing"] is True
    text = safer["answer"]["answer"]
    assert "Atlanta is less exposed" in text
    assert "47.1" in text and "42.4" in text
    assert "Los Angeles is the least exposed" not in text
    fresh = _chat(client, "Compare Dallas and Atlanta")
    explained = _chat(client, "why?", fresh["session_id"])
    safest = _chat(client, "Which hub is the safest?", explained["session_id"])
    _assert_full_safety_ranking(safest)


def test_trailing_365_keeps_the_snow_day_count(client):
    first = _chat(client, "How many snow days did Denver have last year?")
    follow = _chat(client, "What about the trailing 365 days?", first["session_id"])
    assert follow["intent"]["intent"] == "metric_lookup"
    assert follow["intent"]["hub_ids"] == ["denver"]
    assert follow["intent"]["metric"] == "snowfall_day_pct"
    assert follow["intent"]["time_window"] == "trailing_365"
    text = follow["answer"]["answer"]
    assert "Denver" in text and "snow days" in text
    assert "trailing 365 days" in text
    assert "winter score" not in text.lower()
    assert "of the year" not in text


def test_one_hazard_scores_line_names_the_hazard(client):
    winter = _chat(client, "Which hubs in the Midwest are most exposed to winter disruption?")
    assert "Scores (winter): Minneapolis 72.3, Detroit 51.2, Chicago 44.5" in winter["answer"]["answer"]
    heat = _chat(client, "Compare Dallas and Atlanta on heat")
    assert "Scores (heat):" in heat["answer"]["answer"]
    overall = _chat(client, "Compare Dallas and Atlanta")
    assert "Scores: Dallas 47.1, Atlanta 42.4" in overall["answer"]["answer"]
    assert "Scores (" not in overall["answer"]["answer"]


def test_new_regions_do_not_keep_the_previous_hazard(client):
    winter = _chat(client, "Which hubs in the Midwest are most exposed to winter disruption?")
    west = _chat(client, "Which West and Mountain hubs have the highest risk?", winter["session_id"])
    assert west["intent"]["intent"] == "rank_hubs"
    assert west["intent"]["hazards"] == []
    assert west["intent"]["hub_ids"] == ["denver", "losangeles"]
    text = west["answer"]["answer"]
    assert "weather disruption" in text
    assert "Scores: Denver 36.1, Los Angeles 26.8" in text
    assert "Scores (winter)" not in text
    assert "most exposed to winter" not in text


def test_active_alerts_are_listed_and_affect_the_score():
    from app.agent.composer import alerts_answer
    from app.risk.types import AlertBatch, HubAlerts, SourceRef

    source = SourceRef(
        name="National Weather Service active alerts",
        source_type="live_api",
        retrieved_at="2026-11-23",
        uncertainty_note="Active now.",
    )
    miami = HubAlerts(
        hub_id="miami",
        name="Miami",
        region="South",
        batch=AlertBatch(
            weighted={"flood": 2.0},
            available=True,
            count=2,
            events=["Flood Watch (Severe)", "Coastal Flood Advisory (Minor)"],
            source=source,
            fallback=False,
        ),
    )
    answer = alerts_answer([miami])
    text = answer.answer
    assert "Flood Watch" in text
    assert "Coastal Flood Advisory" in text
    assert "current snapshot" in text
    assert "do affect the current score" in text
    assert "not reflected in the annual score" not in text
    assert "not part of the annual score" not in text
    assert all("not part of the annual score" not in item for item in answer.assumptions)


def test_lowest_and_safest_name_los_angeles(client):
    for question in ("Which hub has the lowest weather risk?", "Which hub is the safest?"):
        body = _chat(client, question)
        assert body["evidence"]["safety_framing"] is True
        assert body["intent"]["intent"] == "rank_hubs"
        text = body["answer"]["answer"]
        assert text.startswith("Los Angeles is the least exposed")
        assert "New York/Newark" not in text
        assert "is the most exposed" not in text


def test_methodology_questions_are_not_unknown(client):
    for question in (
        "How do you calculate the scores?",
        "How are the scores calculated?",
        "How does the scoring work?",
    ):
        body = _chat(client, question)
        assert body["intent"]["intent"] == "methodology"
        assert body["intent"]["needs_clarification"] is False
        assert "What should I look up?" not in body["answer"]["answer"]
        assert "Python computes the scores" in body["answer"]["answer"]


def test_rank_all_and_south_list_every_hub_in_order(client):
    ranked = _chat(client, "Rank all hubs by weather risk")
    names = [row["name"] for row in ranked["evidence"]["rankings"]]
    assert names == [
        "Houston",
        "New York/Newark",
        "Dallas",
        "Detroit",
        "Chicago",
        "Minneapolis",
        "Atlanta",
        "Miami",
        "Denver",
        "Los Angeles",
    ]
    text = ranked["answer"]["answer"]
    opening, scores = text.split("Scores:", 1)
    assert _names_in_order(opening, names)
    assert _names_in_order(scores, names)
    south = _chat(client, "Rank the South hubs")
    south_names = [row["name"] for row in south["evidence"]["rankings"]]
    assert south_names == ["Houston", "Dallas", "Atlanta", "Miami"]
    south_opening, south_scores = south["answer"]["answer"].split("Scores:", 1)
    assert _names_in_order(south_opening, south_names)
    assert _names_in_order(south_scores, south_names)
    assert "Chicago" not in south["answer"]["answer"]
