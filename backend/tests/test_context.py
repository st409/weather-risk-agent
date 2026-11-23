"""Follow-up memory is resolved in Python, before any model call."""

from app.agent.session_context import SessionContext, parse_message, resolve_intent


def _resolve(message: str, context: SessionContext | None = None):
    return resolve_intent(parse_message(message), context or SessionContext())


def test_midwest_winter_ranking():
    resolved = _resolve("Which hubs in the Midwest are most exposed to winter disruption?")
    assert resolved.intent == "rank_hubs"
    assert resolved.region == "Midwest"
    assert resolved.hazards == ["winter"]
    assert resolved.needs_clarification is False


def test_miami_houston_comparison():
    resolved = _resolve("Compare Miami and Houston in terms of hurricane and flood exposure.")
    assert resolved.intent == "compare_hubs"
    assert resolved.hub_ids == ["miami", "houston"]
    assert resolved.hazards == ["hurricane", "flood"]


def test_denver_snowfall_percentage():
    resolved = _resolve("What percentage of days in Denver last year had snowfall?")
    assert resolved.intent == "metric_lookup"
    assert resolved.hub_ids == ["denver"]
    assert resolved.metric == "snowfall_day_pct"
    assert resolved.time_window == "last_calendar_year"


def test_dallas_explanation():
    resolved = _resolve("Why is the Dallas hub's weather disruption risk high?")
    assert resolved.intent == "explain_hub"
    assert resolved.hub_ids == ["dallas"]
    assert resolved.needs_clarification is False


def test_flood_follow_up_keeps_the_comparison():
    context = SessionContext(
        hub_ids=["miami", "houston"],
        hazards=["hurricane", "flood"],
        time_window="last_calendar_year",
        last_intent="compare_hubs",
        last_result_hub_ids=["houston", "miami"],
    )
    resolved = _resolve("What about floods only?", context)
    assert resolved.needs_clarification is False
    assert resolved.intent == "compare_hubs"
    assert resolved.hub_ids == ["miami", "houston"]
    assert resolved.hazards == ["flood"]
    assert resolved.inherited is True


def test_why_top_uses_previous_order():
    context = SessionContext(
        region="Midwest",
        hazards=["winter"],
        last_intent="rank_hubs",
        last_result_hub_ids=["minneapolis", "detroit", "chicago"],
    )
    resolved = _resolve("Why is the top one risky?", context)
    assert resolved.intent == "explain_hub"
    assert resolved.hub_ids == ["minneapolis"]


def test_unknown_hub_asks_instead_of_guessing():
    resolved = _resolve("How exposed is Phoenix to heat?")
    assert resolved.needs_clarification is True
    assert "Phoenix" in (resolved.clarifying_question or "")


def test_safer_without_context_defines_the_word():
    resolved = _resolve("Which is safer?")
    assert resolved.needs_clarification is True
    text = resolved.clarifying_question or ""
    assert "lower" in text
    assert "disruption" in text


def test_safer_after_compare_uses_those_hubs():
    context = SessionContext(
        hub_ids=["miami", "houston"],
        hazards=["hurricane", "flood"],
        last_intent="compare_hubs",
        last_result_hub_ids=["miami", "houston"],
    )
    resolved = _resolve("Which is safer?", context)
    assert resolved.needs_clarification is False
    assert resolved.intent == "compare_hubs"
    assert resolved.hub_ids == ["miami", "houston"]
    assert resolved.safety_framing is True
    assert resolved.hazards == []


def test_source_question_is_a_follow_up_with_detail():
    context = SessionContext(
        hub_ids=["denver"],
        hazards=["winter"],
        time_window="last_calendar_year",
        last_intent="metric_lookup",
        metric="snowfall_day_pct",
        last_result_hub_ids=["denver"],
        last_user_message="How many days did it snow in Denver last year?",
    )
    resolved = _resolve("What are your sources?", context)
    assert resolved.needs_clarification is False
    assert resolved.hub_ids == ["denver"]
    assert resolved.detail == "sources"


def test_flood_follow_up_without_context_asks():
    resolved = _resolve("What about floods only?")
    assert resolved.needs_clarification is True


def test_hebrew_flood_follow_up_uses_previous_turn():
    context = SessionContext(
        hub_ids=["miami", "houston"],
        hazards=["hurricane", "flood"],
        time_window="last_calendar_year",
        last_intent="compare_hubs",
        last_result_hub_ids=["houston", "miami"],
        last_user_message="Compare Miami and Houston in terms of hurricane and flood exposure.",
    )
    resolved = _resolve("ומה לגבי שיטפונות בלבד?", context)
    assert resolved.needs_clarification is False
    assert resolved.intent == "compare_hubs"
    assert resolved.hub_ids == ["miami", "houston"]
    assert resolved.hazards == ["flood"]
    previous = context.for_intent_parser()
    assert previous["hub_ids"] == ["miami", "houston"]
    assert previous["hazards"] == ["hurricane", "flood"]
    assert "Miami" in previous["previous_user_message"]
