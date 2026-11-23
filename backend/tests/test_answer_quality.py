"""Answer wording, one-decimal evidence, and stale snow-day cache."""

import re
from datetime import date

import pytest
from fastapi.testclient import TestClient

from app.agent.composer import WINDOW_NOTICE, _draft_is_usable, _fact_numbers, apply_model_prose
from app.agent.llm import ANSWER_SYSTEM
from app.agent.schemas import AnswerDraft, AnswerResult, IntentResult
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


def test_prompt_keeps_short_answers():
    assert "2 or 3" in ANSWER_SYSTEM
    assert "every hub on the Scores line" in ANSWER_SYSTEM
    assert "not fourteen" in ANSWER_SYSTEM
    assert "not ninety-nine" in ANSWER_SYSTEM
    assert "137 vs 120" in ANSWER_SYSTEM
    assert "not a probability" in ANSWER_SYSTEM
    assert ">= 1 cm" in ANSWER_SYSTEM
    assert "is riskier" in ANSWER_SYSTEM
    assert "higher = more exposed" in ANSWER_SYSTEM


def test_draft_accepts_short_answers_and_checks_numbers():
    assert _draft_is_usable("Denver is highest at 44.8. Based on 2025 weather data.", {"rankings": [{"focus_score": 44.8}]}, [], "")
    assert _draft_is_usable(" ".join(["briefing"] * 200), {"rankings": [{"hub_id": "miami"}]}, [], "")
    assert _draft_is_usable("The gap is 44.9.", {"rankings": [{"hub_id": "miami", "focus_score": 44.8}]}, [], "")
    assert not _draft_is_usable("The gap is 12.0.", {"rankings": [{"hub_id": "miami", "focus_score": 44.8}]}, [], "")
    evidence = {"required_notice": WINDOW_NOTICE, "unsupported_window": True, "rankings": [{"focus_score": 4.0}]}
    assert not _draft_is_usable("Denver had 4 snow days in January.", evidence, [], "")
    assert not _draft_is_usable(f"{WINDOW_NOTICE} Denver had 4 snow days in January.", evidence, [], "")
    skeleton = f"{WINDOW_NOTICE} Denver had 4 snow days in calendar year 2025."
    assert _draft_is_usable(skeleton, evidence, [], "")
    rejected = apply_model_prose(
        AnswerResult(answer=skeleton),
        AnswerDraft(answer="Denver had 4 snow days in January."),
        None,
        evidence,
    )
    assert rejected.answer == skeleton
    phrase = "mainly flood (12.4) and hurricane (10.3) of 42.9"
    why_evidence = {
        "required_driver_phrase": phrase,
        "driver_score_traps": [{"name": "winter", "score": 37.8, "points": 11.3}],
        "rankings": [{"focus_score": 42.9, "drivers": [{"contribution": 12.4}, {"contribution": 10.3}]}],
    }
    why_skeleton = f"The Dallas score is 42.9, {phrase}."
    assert not _draft_is_usable("Dallas is high.", why_evidence, [], "")
    assert not _draft_is_usable(f"{phrase}. Winter is 37.8.", why_evidence, [], "")
    assert _draft_is_usable(why_skeleton, why_evidence, [], "")
    kept = apply_model_prose(AnswerResult(answer=why_skeleton), AnswerDraft(answer="Dallas is high because winter is 37.8."), None, why_evidence)
    assert kept.answer == why_skeleton
    scored = {
        "required_driver_phrase": phrase,
        "breakdown": "Contributors, largest first: flood 49.6 → 12.4 of the 42.9 points and hurricane 51.5 → 10.3 of the 42.9 points.",
        "rankings": [{"focus_score": 42.9, "hazard_scores": {"flood": 49.6, "hurricane": 51.5}, "drivers": [{"contribution": 12.4}, {"contribution": 10.3}]}],
    }
    assert not _draft_is_usable("Dallas is high, mainly flood (49.6) and hurricane (51.5) of 42.9.", scored, [], "")
    assert not _draft_is_usable("Dallas is high, mainly winter (37.8) and flood (8.0) of 80.0.", {"breakdown": "Contributors, largest first: winter 37.8 → 11.3 of the 80.0 points and flood 32.0 → 8.0 of the 80.0 points.", "rankings": [{"hazard_scores": {"winter": 37.8, "flood": 32.0}}]}, [], "")
    assert _draft_is_usable(why_skeleton, scored, [], "")
    replaced = apply_model_prose(AnswerResult(answer=why_skeleton), AnswerDraft(answer="Dallas is high, mainly flood (49.6) and hurricane (51.5) of 42.9."), None, scored)
    assert replaced.answer == why_skeleton
    assert not _draft_is_usable(f"{WINDOW_NOTICE} Denver is highest at 44.8.", {"rankings": [{"focus_score": 44.8}]}, [], "")


def test_draft_keeps_scores_in_one_final_line_and_rejects_overclaims():
    skeleton = (
        "Dallas, Houston and Atlanta are the most exposed to heat disruption, with Dallas on top. "
        "Dallas had 153 days reaching 95 F in 2025. "
        "Scores: Dallas 81.2, Houston 61.9, Atlanta 39.9 (higher = more exposed)."
    )
    assert _draft_is_usable(skeleton, {}, [], skeleton)
    woven = skeleton.replace("Dallas had 153", "Dallas at 81.2 had 153")
    assert _draft_is_usable(woven, {}, [], skeleton)
    kept_scores = apply_model_prose(AnswerResult(answer=skeleton), AnswerDraft(answer=woven), None, {})
    assert kept_scores.fallback is False
    assert "Dallas at 81.2 had 153" in kept_scores.answer
    assert "score of." not in kept_scores.answer.lower()
    assert _draft_is_usable(skeleton.replace(" (higher = more exposed)", ""), {}, [], skeleton)
    assert not _draft_is_usable(
        "Dallas had 153 days reaching 95 F in 2025. Scores: (higher = more exposed).",
        {},
        [],
        skeleton,
    )
    counted = "Denver hub (Mountain region) had 151 freezing days (41.4% of the year)."
    kept_count = apply_model_prose(
        AnswerResult(answer=counted),
        AnswerDraft(answer=counted + " Scores: (higher = more exposed)."),
        None,
        {},
    )
    assert "Scores:" not in kept_count.answer
    assert kept_count.fallback is False
    assert not _draft_is_usable("Denver had a significant portion of the year.", {}, [], counted)
    assert not _draft_is_usable("Denver is the most exposed hub.", {}, [], counted)


def test_score_clauses_are_removed_whole():
    rank = (
        "New York/Newark, Houston and Dallas are the most exposed, with New York/Newark slightly ahead. "
        "FEMA rates New York/Newark a high historical hurricane-risk area. "
        "Scores: New York/Newark 50.6, Houston 51.2, Dallas 47.1 (higher = more exposed)."
    )
    rank_draft = (
        "New York/Newark, Houston and Dallas are the most exposed, with New York/Newark slightly ahead. "
        "New York/Newark has a score of 50.6 while Houston follows closely with a score of 51.2. "
        "FEMA rates New York/Newark a high historical hurricane-risk area. "
        "Scores: New York/Newark 50.6, Houston 51.2, Dallas 47.1 (higher = more exposed)."
    )
    safest = (
        "Los Angeles is the least exposed hub to weather disruption. Denver and Miami are riskier. "
        "Scores: Los Angeles 26.8 (higher = more exposed)."
    )
    safest_draft = (
        "Los Angeles is the least exposed hub to weather disruption with a score of 26.8. "
        "Denver and Miami are riskier. "
        "Scores: Los Angeles 26.8 (higher = more exposed)."
    )
    excluded = (
        "Houston is the most exposed hub after removing winter. "
        "The remaining drivers are hurricane and flood. FEMA rates it a high historical hurricane-risk area. "
        "Scores: Houston 65.5 (higher = more exposed)."
    )
    excluded_draft = (
        "Houston is the most exposed hub after removing winter. "
        "It has a score of 65.5 and is a high historical hurricane-risk area. "
        "Scores: Houston 65.5 (higher = more exposed)."
    )
    compared = (
        "Dallas hub (South region) is more exposed to heat disruption than Atlanta hub (South region). "
        "Dallas had 153 days reaching 95 F in 2025. "
        "Scores: Dallas 81.2, Atlanta 39.9 (higher = more exposed)."
    )
    compared_draft = (
        "Dallas hub (South region) is more exposed to heat disruption than Atlanta hub (South region). "
        "Dallas had 153 days reaching 95 F in 2025. "
        "The overall scores are Dallas 81.2 and Atlanta 39.9 (higher = more exposed). "
        "Scores: Dallas 81.2, Atlanta 39.9 (higher = more exposed)."
    )
    for skeleton, draft in ((rank, rank_draft), (safest, safest_draft), (excluded, excluded_draft), (compared, compared_draft)):
        shown = apply_model_prose(AnswerResult(answer=skeleton), AnswerDraft(answer=draft), None, {})
        assert shown.fallback is False
        assert shown.answer.lower().count("higher = more exposed") == 1
        assert "score of." not in shown.answer.lower()
        assert "score for" not in shown.answer.lower() or not shown.answer.lower().rstrip().endswith("is.")
    assert "The overall scores are Dallas 81.2 and Atlanta 39.9." in apply_model_prose(
        AnswerResult(answer=compared), AnswerDraft(answer=compared_draft), None, {}
    ).answer


def test_day_count_words_and_a_missing_notice_still_keep_the_draft():
    skeleton = (
        "Denver hub (Mountain region) had 8 snow days (2.7% of the year). "
        "A snow day is daily snowfall >= 1 cm. Sources: Open-Meteo 2025 weather."
    )
    draft = (
        "Denver hub (Mountain region) had eight snow days, two point seven percent of the year. "
        "A snow day is daily snowfall >= 1 cm."
    )
    assert _fact_numbers("ten") == []
    assert _fact_numbers("eight") == [8.0]
    assert _fact_numbers("2.7 percent") == [2.7]
    assert _fact_numbers("seventeen") == [17.0]
    assert _fact_numbers("four point seven percent") == [4.7]
    assert _fact_numbers("four-point-seven percent") == [4.7]
    assert _fact_numbers("4.7%") == _fact_numbers("4.7 percent")
    assert _fact_numbers("seven\u00adteen") == [17.0]
    snow = "Denver hub (Mountain region) had 17 snow days (4.7% of the year). A snow day is daily snowfall >= 1 cm."
    spoken_snow = "Denver hub (Mountain region) had seventeen snow days, four point seven percent of the year. A snow day is daily snowfall >= 1 cm."
    assert _draft_is_usable(spoken_snow, {}, [], snow)
    percent_words = "Denver hub (Mountain region) had seventeen snow days (4.7 percent of the year). A snow day is daily snowfall >= 1 cm."
    kept_snow = apply_model_prose(AnswerResult(answer=snow), AnswerDraft(answer=percent_words), None, {})
    assert kept_snow.fallback is False
    assert kept_snow.rejection_reason is None
    miami = "Houston had 14 heavy-rain days and Miami had 5. Scores: hurricane Houston 80.0, Miami 80.0; flood Houston 44.9, Miami 40.2."
    assert _draft_is_usable(miami.replace("44.9", "44.9, about forty-five"), {}, [], miami)
    assert _fact_numbers("fourteen") == [14.0]
    assert _fact_numbers("ninety-nine") == [99.0]
    assert _fact_numbers("ninety\u2011nine") == [99.0]
    assert _fact_numbers("two hundred and twelve") == [212.0]
    spoken_days = miami.replace("14", "fourteen").replace("Miami had 5", "Miami had five")
    assert _draft_is_usable(spoken_days, {}, [], miami)
    kept_days = apply_model_prose(AnswerResult(answer=miami), AnswerDraft(answer=spoken_days), None, {})
    assert kept_days.fallback is False
    assert "fourteen" not in kept_days.answer
    assert "14 heavy-rain days" in kept_days.answer
    assert _draft_is_usable(draft, {}, [], skeleton)
    assert _draft_is_usable(draft.replace("two point seven percent", "2.7 percent"), {}, [], skeleton)
    assert not _draft_is_usable(draft.replace("eight", "nine"), {}, [], skeleton)
    assert not _draft_is_usable(draft.replace("two point seven", "8.1"), {}, [], skeleton)
    kept = apply_model_prose(AnswerResult(answer=skeleton), AnswerDraft(answer=draft + " It has a score of 57.5."), None, {})
    assert kept.fallback is True
    assert kept.answer == skeleton
    assert "score of." not in kept.answer.lower()
    noticed = apply_model_prose(
        AnswerResult(answer=f"{WINDOW_NOTICE} {skeleton}"),
        AnswerDraft(answer=draft),
        None,
        {"required_notice": WINDOW_NOTICE},
    )
    assert noticed.fallback is False
    assert WINDOW_NOTICE in noticed.answer


def test_assignment_paraphrases_do_not_fall_back(client):
    questions = [
        "Which hubs in the Midwest are most exposed to winter disruption?",
        "Compare Miami and Houston in terms of hurricane and flood exposure.",
        "What percentage of days in Denver last year had snowfall?",
        "Why is the Dallas hub's weather disruption risk high?",
    ]
    for question in questions:
        body = _chat(client, question)
        base = AnswerResult.model_validate(body["answer"])
        paraphrase = base.answer.replace("%", " percent").replace("32 F", "32°F").replace("Sources:", "Sources: recorded from")
        kept = apply_model_prose(base, AnswerDraft(answer=paraphrase), None, body["evidence"])
        assert not any("did not return a valid briefing" in item.lower() for item in kept.uncertainties)
        assert "Sources:" in kept.answer
        dropped = base.answer.split("Sources:", 1)[0].strip()
        restored = apply_model_prose(base, AnswerDraft(answer=dropped), None, body["evidence"])
        assert restored.answer.rstrip().endswith(base.answer[base.answer.index("Sources:") :].strip())
        assert not any("did not return a valid briefing" in item.lower() for item in restored.uncertainties)


def test_gap_draft_that_reverses_the_leader_is_rejected(client):
    first = _chat(client, "Compare Chicago and Detroit on winter")
    body = _chat(client, "why?", first["session_id"])
    base = AnswerResult.model_validate(body["answer"])
    assert "Chicago trails Detroit" in base.answer
    flipped = base.answer.replace("Chicago trails Detroit", "Detroit trails Chicago")
    rejected = apply_model_prose(base, AnswerDraft(answer=flipped), None, body["evidence"])
    assert rejected.answer == base.answer
    assert rejected.fallback is True
    assert rejected.rejection_reason
    visible = " ".join([rejected.answer, *rejected.uncertainties, *rejected.assumptions]).lower()
    assert "did not return a valid briefing" not in visible


def test_day_count_draft_accepts_a_degree_symbol():
    skeleton = (
        "Minneapolis hub has the most freeze days: 212 of 365, 58.1%. "
        "Next is Denver hub at 151 of 365, 41.4%. "
        "A freeze day has a minimum of 32 F or lower. "
        "Source: offline stand-in for the Open-Meteo archive."
    )
    draft = (
        "Minneapolis leads with 212 freeze days, 58.1% of 365. "
        "Denver is next at 151 of 365, 41.4%. "
        "A freeze day is a low of 32°F. "
        "Source: offline stand-in for the Open-Meteo archive."
    )
    evidence = {"metric": {"numerator": 212, "value": 58.1, "denominator": 365}}
    assert _draft_is_usable(draft, evidence, [], skeleton)
    assert not _draft_is_usable(draft.replace("151", "999"), evidence, [], skeleton)


def test_tied_next_day_counts_are_all_named():
    from types import SimpleNamespace

    from app.agent.composer import _next_count_clause

    def hub(hub_id: str, name: str, region: str):
        return SimpleNamespace(hub_id=hub_id, name=name, region=region)

    packed = [
        (hub("dallas", "Dallas", "South"), {"numerator": 7, "denominator": 365, "value": 1.9}),
        (hub("losangeles", "Los Angeles", "West"), {"numerator": 3, "denominator": 365, "value": 0.8}),
        (hub("denver", "Denver", "Mountain"), {"numerator": 3, "denominator": 365, "value": 0.8}),
        (hub("newyork", "New York/Newark", "Northeast"), {"numerator": 3, "denominator": 365, "value": 0.8}),
        (hub("houston", "Houston", "South"), {"numerator": 0, "denominator": 365, "value": 0.0}),
    ]
    text = _next_count_clause(packed, set())
    assert "Next are" in text
    assert "each 3 of 365" in text
    assert "Los Angeles hub (West region)" in text
    assert "Denver hub (Mountain region)" in text
    assert "New York/Newark hub (Northeast region)" in text
    assert "Houston" not in text


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
    assert "Denver" in text
    assert "Sources:" in text
    assert "FEMA NRI" in text
    assert "freeze-day rate" not in text.lower()
    assert "not a probability" not in text.lower()
    assert "weather-day rates" not in text.lower()
    assert "hurricane" not in text.lower()
    assert "other factors" not in body["evidence"]["breakdown"].lower()
    assert "heat" in body["evidence"]["breakdown"].lower()
    assert "Want the full score breakdown?" in body["answer"]["follow_up_questions"]
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
    assert text.startswith("Minneapolis, Detroit, and Chicago")
    assert "Chicago" in text
    assert "Detroit" in text
    assert "Scores:" in text
    assert not any(char.isdigit() for char in text.split(".", 1)[0])
    assert "FEMA rates it" not in text


def test_why_after_a_ranking_explains_the_gap(client):
    first = _chat(client, "Which hubs in the Midwest are most exposed to winter disruption?")
    body = _chat(client, "why?", first["session_id"])
    text = body["answer"]["answer"]
    assert body["intent"]["intent"] == "explain_hub"
    assert "edges out" in text
    assert "Minneapolis" in text and "Detroit" in text
    assert "freezing days" in text and " vs " in text
    assert "snow-day rate" in text
    assert "both are rated high winter-risk by FEMA" in text
    assert "most exposed to winter disruption, with" not in text


def test_plural_ranking_draft_names_every_hub_in_plain_words():
    skeleton = (
        "Minneapolis, Detroit, and Chicago are the most exposed to winter disruption, with Minneapolis on top. "
        "Minneapolis had 212 freezing days in 2025. "
        "Scores: Minneapolis 72.3, Detroit 51.2, Chicago 44.5 (higher = more exposed)."
    )
    labeled = (
        "Minneapolis hub (Midwest region) and Detroit hub (Midwest region) are the most exposed to winter disruption. "
        "Minneapolis had 212 freezing days in 2025. "
        "Scores: Minneapolis 72.3, Detroit 51.2, Chicago 44.5 (higher = more exposed)."
    )
    assert not _draft_is_usable(labeled, {}, [], skeleton)
    shown = apply_model_prose(AnswerResult(answer=skeleton), AnswerDraft(answer=labeled), None, {})
    assert shown.fallback is False
    assert shown.answer.startswith("Minneapolis, Detroit, and Chicago")
    assert "hub (" not in shown.answer
    assert shown.answer.lower().count("scores:") == 1
    overall = (
        "New York/Newark, Houston, and Dallas are the most exposed to weather disruption, with New York/Newark slightly ahead. "
        "FEMA rates New York/Newark a high historical hurricane-risk area. "
        "Scores: New York/Newark 50.6, Houston 51.2, Dallas 47.1 (higher = more exposed)."
    )
    dropped = (
        "New York/Newark and Houston are the most exposed to weather disruption, with New York/Newark slightly ahead. "
        "Dallas trails the other two. FEMA rates New York/Newark a high historical hurricane-risk area. "
        "Scores: New York/Newark 50.6, Houston 51.2, Dallas 47.1 (higher = more exposed)."
    )
    assert not _draft_is_usable(dropped, {}, [], overall)
    assert _draft_is_usable(overall, {}, [], overall)
    ranked = (
        "New York/Newark, Houston, and Dallas are the most exposed to weather disruption, with New York/Newark slightly ahead. "
        "FEMA rates New York/Newark a high historical hurricane-risk area. "
        "Scores: New York/Newark 46.8, Houston 44.2, Dallas 43.0 (higher = more exposed)."
    )
    trailing = ranked.replace(
        "FEMA rates",
        "Atlanta trails the rest of the set. FEMA rates",
    )
    assert _draft_is_usable(trailing, {}, [], ranked)
    promoted = ranked.replace(
        "FEMA rates",
        "Atlanta is more exposed than New York/Newark. FEMA rates",
    )
    assert not _draft_is_usable(promoted, {}, [], ranked)
    named_in_opening = ranked.replace(
        "New York/Newark, Houston, and Dallas",
        "New York/Newark, Houston, Dallas, and Atlanta",
    )
    assert not _draft_is_usable(named_in_opening, {}, [], ranked)
    gap = (
        "Minneapolis edges out Detroit mainly because it had more freezing days (137 vs 120) and a higher snow-day rate; "
        "both are rated high winter-risk by FEMA. "
        "Scores: Minneapolis 45.1, Detroit 42.3."
    )
    restated = (
        "Minneapolis edges out Detroit mainly because it had more freezing days (137 vs 120). "
        "Both are rated high winter-risk by FEMA. "
        "Scores: Minneapolis 45.1, Detroit 42.3."
    )
    assert _draft_is_usable(restated, {}, [], gap)
    spelled_gap = restated.replace("137", "one hundred thirty-seven").replace("120", "one hundred twenty")
    assert _draft_is_usable(spelled_gap, {}, [], gap)
    kept_gap = apply_model_prose(AnswerResult(answer=gap), AnswerDraft(answer=spelled_gap), None, {})
    assert kept_gap.fallback is False
    assert "137 vs 120" in kept_gap.answer
    assert "one hundred" not in kept_gap.answer
    assert not _draft_is_usable(restated.replace("(137 vs 120)", ""), {}, [], gap)
    restored = apply_model_prose(
        AnswerResult(answer=gap),
        AnswerDraft(answer=restated.replace("(137 vs 120)", "")),
        None,
        {},
    )
    assert restored.fallback is False
    assert "137 vs 120" in restored.answer
    assert restored.answer.lower().count("edges out") == 1
    assert restored.answer.lower().count("scores:") == 1


def test_single_hub_follow_up_stays_relative_to_the_set(client):
    first = _chat(client, "Which hubs in the Midwest are most exposed to winter disruption?")
    body = _chat(client, "and Chicago?", first["session_id"])
    text = body["answer"]["answer"]
    assert "least exposed of the three Midwest hubs" in text
    assert "FEMA still rates it a high historical winter-risk area" in text
    assert "freezing days in 2025" in text
    assert "faces high" not in text
    after_why = _chat(client, "why?", first["session_id"])
    later = _chat(client, "and Chicago?", after_why["session_id"])
    later_text = later["answer"]["answer"]
    assert "least exposed of the three Midwest hubs" in later_text
    assert "faces high" not in later_text


def test_compare_does_not_invent_a_priority(client):
    body = _chat(client, "Compare Miami and Houston in terms of hurricane and flood exposure.")
    assert "I'd prioritize" not in body["answer"]["answer"]
    skeleton = (
        "Houston hub (South region) is more exposed to flood than Miami hub (South region). "
        "Scores: flood Houston 44.9, Miami 40.2."
    )
    draft = (
        "Houston hub (South region) is more exposed to flood than Miami hub (South region). "
        "I'd prioritize Houston for flood exposure. "
        "Scores: flood Houston 44.9, Miami 40.2."
    )
    shown = apply_model_prose(AnswerResult(answer=skeleton), AnswerDraft(answer=draft), None, {})
    assert shown.fallback is False
    assert "I'd prioritize" not in shown.answer
    assert "44.9" in shown.answer


def test_a_draft_recommendation_is_not_repeated():
    skeleton = (
        "New York/Newark is the most vulnerable hub to weather disruption. "
        "FEMA rates it a high historical hurricane-risk area. "
        "Based on those risk drivers, I'd prioritize hurricane and flood protection and winter readiness. "
        "Scores: New York/Newark 46.8 (higher = more exposed). "
        "Sources: Open-Meteo 2025 weather, FEMA NRI, NWS alerts."
    )
    draft = (
        "New York/Newark is the most vulnerable hub to weather disruption. "
        "FEMA rates it a high historical hurricane-risk area. "
        "I recommend prioritizing hurricane and flood protection and winter readiness. "
        "Scores: New York/Newark 46.8 (higher = more exposed). "
        "Sources: Open-Meteo 2025 weather, FEMA NRI, NWS alerts."
    )
    shown = apply_model_prose(AnswerResult(answer=skeleton), AnswerDraft(answer=draft), None, {})
    assert shown.fallback is False
    assert "I recommend prioritizing hurricane and flood protection and winter readiness." in shown.answer
    assert "Based on those risk drivers, I'd prioritize" not in shown.answer
    curly = draft.replace("I recommend prioritizing", "I\u2019d prioritize")
    kept = apply_model_prose(AnswerResult(answer=skeleton), AnswerDraft(answer=curly), None, {})
    assert kept.fallback is False
    assert len(re.findall(r"(?i)\bprioriti[sz]\w*", kept.answer)) == 1


def test_literal_newline_does_not_leave_a_second_scores_line():
    skeleton = (
        "Houston hub (South region) is more exposed to flood than Miami hub (South region). "
        "Scores: hurricane Houston 80.0, Miami 80.0; flood Houston 44.9, Miami 40.2. "
        "Sources: Open-Meteo 2025 weather, FEMA NRI, NWS alerts."
    )
    draft = skeleton + "\\nScores: hurricane Houston 80.0, Miami 80.0. Sources: Open-Meteo 2025 weather."
    shown = apply_model_prose(AnswerResult(answer=skeleton), AnswerDraft(answer=draft), None, {})
    assert shown.fallback is False
    assert "\\n" not in shown.answer
    assert shown.answer.lower().count("scores:") == 1
    assert shown.answer.lower().count("sources:") == 1
    assert "44.9" in shown.answer


def test_investment_recommendation_is_its_own_sentence():
    skeleton = (
        "New York/Newark is the most vulnerable hub to weather disruption. "
        "FEMA rates it a high historical hurricane-risk area. "
        "Based on those risk drivers, I'd prioritize hurricane and flood protection and winter readiness. "
        "Scores: New York/Newark 46.8 (higher = more exposed)."
    )
    glued = (
        "New York/Newark is the most vulnerable hub to weather disruption. "
        "FEMA rates it a high historical hurricane-risk area, and based on those risk drivers, I'd prioritize hurricane and flood protection and winter readiness. "
        "Scores: New York/Newark 46.8 (higher = more exposed)."
    )
    shown = apply_model_prose(AnswerResult(answer=skeleton), AnswerDraft(answer=glued), None, {})
    assert shown.fallback is False
    assert "area. Based on those risk drivers, I'd prioritize hurricane and flood protection and winter readiness." in shown.answer
    assert shown.answer.lower().count("higher = more exposed") == 1


def test_where_to_invest_suggests_from_the_leaders_drivers(client):
    body = _chat(client, "Where should we invest to reduce weather disruption?")
    text = body["answer"]["answer"]
    assert body["intent"]["intent"] == "rank_hubs"
    assert "most vulnerable" in text
    assert "Based on those risk drivers, I'd prioritize" in text
    suggestion = text.split("Based on those risk drivers", 1)[1].split("Scores:", 1)[0]
    assert not any(char.isdigit() for char in suggestion)
    assert text.split("Scores:", 1)[0].rstrip().endswith(".")
    assert "$" not in text
    assert "cost" not in text.lower()
    strengthened = _chat(client, "What should we strengthen in Miami?")
    miami = strengthened["answer"]["answer"]
    prioritized = _chat(client, "What should we prioritize?")
    assert prioritized["intent"]["intent"] == "rank_hubs"
    assert "Based on those risk drivers, I'd prioritize" in prioritized["answer"]["answer"]
    assert strengthened["intent"]["intent"] == "explain_hub"
    assert "Miami" in miami
    assert "Based on those risk drivers, I'd prioritize" in miami
    assert not any(char.isdigit() for char in miami.split("Based on those risk drivers", 1)[1].split("Scores:", 1)[0])


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
    monkeypatch.setattr(
        "app.risk.weather._fetch_open_meteo",
        lambda _hub, start, end: (
            [
                DailyObs(start.isoformat(), 40.0, 20.0, 0.0, 0.4, 10.0),
                DailyObs(end.isoformat(), 41.0, 21.0, 0.0, 1.5, 12.0),
            ],
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
