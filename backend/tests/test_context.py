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


def test_safer_without_context_ranks_every_hub():
    resolved = _resolve("Which hub is safest?")
    assert resolved.needs_clarification is False
    assert resolved.intent == "rank_hubs"
    assert resolved.hub_ids == []
    assert resolved.region is None
    assert resolved.safety_framing is True


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


def test_source_question_is_methodology_without_inheriting_a_hub():
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
    assert resolved.intent == "methodology"
    assert resolved.hub_ids == []
    assert resolved.detail == "sources"


def test_flood_follow_up_without_context_asks():
    resolved = _resolve("What about floods only?")
    assert resolved.needs_clarification is True


def test_negation_verbs_drop_the_named_hazard():
    cases = {
        "Which hubs are most exposed if we exclude heat?": "heat",
        "Which hubs are most exposed, leave out winter?": "winter",
        "Which hubs are most exposed and drop hurricanes?": "hurricane",
        "Which hubs are most exposed, not counting floods?": "flood",
        "Which hubs are most exposed, heat excluded?": "heat",
    }
    for message, hazard in cases.items():
        resolved = _resolve(message)
        assert resolved.intent == "rank_hubs", message
        assert resolved.excluded_hazards == [hazard], message
        assert resolved.hub_ids == []


def test_january_follow_up_keeps_the_snowfall_question():
    context = SessionContext(
        hub_ids=["denver"],
        metric="snowfall_day_pct",
        time_window="last_calendar_year",
        last_intent="metric_lookup",
        last_result_hub_ids=["denver"],
    )
    resolved = _resolve("What about January?", context)
    assert resolved.intent == "metric_lookup"
    assert resolved.hub_ids == ["denver"]
    assert resolved.metric == "snowfall_day_pct"
    assert resolved.unsupported_window is True
    assert resolved.time_window == "last_calendar_year"


def test_show_the_breakdown_keeps_the_ranking():
    context = SessionContext(
        region="Midwest",
        hazards=["winter"],
        last_intent="rank_hubs",
        last_result_hub_ids=["minneapolis", "chicago", "detroit"],
    )
    resolved = _resolve("Show the breakdown", context)
    assert resolved.intent == "rank_hubs"
    assert resolved.region == "Midwest"
    assert resolved.detail == "breakdown"
    assert resolved.needs_clarification is False


def test_merge_ignores_a_region_or_hub_the_user_did_not_name():
    from app.agent.orchestrator import _merge
    from app.agent.schemas import IntentResult

    rules = parse_message("Which hubs are most exposed?")
    model = IntentResult(
        intent="rank_hubs",
        hub_ids=["denver"],
        region="Midwest",
        hazards=["heat"],
        needs_clarification=False,
    )
    merged = _merge(rules, model, SessionContext())
    assert merged.region is None
    assert merged.hub_ids == []

    named = parse_message("Which hubs in the Midwest are most exposed?")
    overwritten = _merge(
        named,
        IntentResult(intent="rank_hubs", hub_ids=["miami"], region="South", needs_clarification=False),
        SessionContext(),
    )
    assert overwritten.region == "Midwest"
    assert overwritten.hub_ids == []


def test_dalas_counts_as_dallas():
    resolved = _resolve("Why is Dalas risky?")
    assert resolved.intent == "explain_hub"
    assert resolved.hub_ids == ["dallas"]
    assert resolved.needs_clarification is False


def test_yes_after_the_offer_asks_for_the_breakdown():
    context = SessionContext(
        region=None,
        hazards=[],
        last_intent="rank_hubs",
        last_result_hub_ids=["miami", "houston"],
        offered_breakdown=True,
    )
    resolved = _resolve("yes", context)
    assert resolved.intent == "rank_hubs"
    assert resolved.detail == "breakdown"
    assert resolved.needs_clarification is False


def test_follow_up_uses_the_hub_just_discussed():
    context = SessionContext(
        hub_ids=["minneapolis", "detroit"],
        focus_hub_id="detroit",
        hazards=["winter"],
        last_intent="explain_hub",
        last_result_hub_ids=["minneapolis", "detroit", "chicago"],
    )
    breakdown = _resolve("Show the full breakdown", context)
    assert breakdown.intent == "explain_hub"
    assert breakdown.hub_ids == ["detroit"]
    assert breakdown.detail == "breakdown"
    window = _resolve("trailing 365 days", context)
    assert window.intent == "explain_hub"
    assert window.hub_ids == ["detroit"]
    assert window.time_window == "trailing_365"


def test_why_number_two_compares_with_the_leader():
    context = SessionContext(
        region="Midwest",
        hazards=["winter"],
        last_intent="rank_hubs",
        last_result_hub_ids=["minneapolis", "chicago", "detroit"],
    )
    resolved = _resolve("Why is #2 lower?", context)
    assert resolved.explain_gap is True
    assert resolved.hub_ids == ["minneapolis", "chicago"]
    assert resolved.hazards == ["winter"]


def test_hebrew_message_does_not_rank():
    context = SessionContext(
        hub_ids=["miami", "houston"],
        hazards=["hurricane", "flood"],
        time_window="last_calendar_year",
        last_intent="compare_hubs",
        last_result_hub_ids=["houston", "miami"],
        last_user_message="Compare Miami and Houston in terms of hurricane and flood exposure.",
    )
    resolved = _resolve("ומה לגבי שיטפונות בלבד?", context)
    assert resolved.intent == "unsupported_language"
    assert resolved.hub_ids == []
    assert resolved.needs_clarification is False


def test_southern_is_the_south_and_multi_region_is_a_union():
    southern = _resolve("Rank the Southern hubs by severe storm")
    assert southern.intent == "rank_hubs"
    assert southern.region == "South"
    assert southern.hazards == ["severe_storm"]
    assert southern.hub_ids == []
    west = _resolve("Rank the West and Mountain hubs by overall risk")
    assert west.intent == "rank_hubs"
    assert set(west.hub_ids) == {"denver", "losangeles"}
    flood = _resolve("Rank the Northeast and South hubs by flood risk")
    assert flood.intent == "rank_hubs"
    assert set(flood.hub_ids) == {"newyork", "dallas", "houston", "miami", "atlanta"}


def test_day_counts_stay_on_the_metric():
    freeze = _resolve("Which hub has the most freeze days?")
    assert freeze.intent == "metric_lookup"
    assert freeze.metric == "freeze_day_pct"
    assert freeze.needs_clarification is False
    snow = _resolve("Does Chicago or Minneapolis have more snow days in 2025?")
    assert snow.intent == "metric_lookup"
    assert snow.metric == "snowfall_day_pct"
    assert set(snow.hub_ids) == {"chicago", "minneapolis"}
    assert snow.needs_clarification is False


def test_follow_up_keeps_the_ranking_leader():
    context = SessionContext(
        hazards=["heat"],
        last_intent="rank_hubs",
        last_result_hub_ids=["dallas", "miami", "atlanta"],
    )
    compared = _resolve("How does Atlanta compare on heat?", context)
    assert compared.intent == "compare_hubs"
    assert compared.hub_ids == ["dallas", "atlanta"]
    assert compared.hazards == ["heat"]
    assert compared.needs_clarification is False
    lower = _resolve("Why is Atlanta lower?", context)
    assert lower.explain_gap is True
    assert lower.hub_ids == ["dallas", "atlanta"]
    assert lower.hazards == ["heat"]


def test_harden_and_freshness_are_answered():
    harden = _resolve("If we could only harden one hub this winter, which one should we pick?")
    assert harden.intent == "rank_hubs"
    assert harden.hazards == ["winter"]
    assert harden.needs_clarification is False
    fresh = _resolve("How fresh is the weather and FEMA data behind these scores?")
    assert fresh.intent == "methodology"
    assert fresh.needs_clarification is False


def test_bare_why_after_compare_explains_the_gap():
    context = SessionContext(
        hub_ids=["chicago", "detroit"],
        hazards=["winter"],
        last_intent="compare_hubs",
        last_result_hub_ids=["detroit", "chicago"],
    )
    resolved = _resolve("why?", context)
    assert resolved.intent == "explain_hub"
    assert resolved.explain_gap is True
    assert resolved.hub_ids == ["detroit", "chicago"]
    assert resolved.hazards == ["winter"]


def test_bare_why_after_exclusion_keeps_the_filter():
    context = SessionContext(
        hub_ids=["newyork", "houston", "dallas"],
        last_intent="rank_hubs",
        last_result_hub_ids=["newyork", "houston", "dallas"],
        excluded_hazards=["flood"],
    )
    resolved = _resolve("why?", context)
    assert resolved.intent == "explain_hub"
    assert resolved.explain_gap is True
    assert resolved.rank_gap is True
    assert resolved.excluded_hazards == ["flood"]
    assert resolved.hub_ids == ["newyork", "houston"]


def test_bare_why_after_a_day_count_stays_on_that_count():
    context = SessionContext(
        hub_ids=["minneapolis", "detroit", "chicago"],
        region="Midwest",
        hazards=["winter"],
        metric="freeze_day_pct",
        last_intent="metric_lookup",
        last_result_hub_ids=["minneapolis", "detroit", "chicago"],
    )
    resolved = _resolve("why?", context)
    assert resolved.intent == "metric_lookup"
    assert resolved.metric == "freeze_day_pct"
    assert resolved.hazards == []
    assert resolved.region == "Midwest"


def test_which_and_why_still_ranks():
    resolved = _resolve("If we could only harden one hub this winter, which one should we choose and why?")
    assert resolved.intent == "rank_hubs"
    assert resolved.hazards == ["winter"]
    assert resolved.hub_ids == []
    assert resolved.needs_clarification is False


def test_and_denver_keeps_the_freeze_metric():
    context = SessionContext(
        hub_ids=["detroit"],
        focus_hub_id="detroit",
        metric="freeze_day_pct",
        hazards=["winter"],
        last_intent="metric_lookup",
        last_result_hub_ids=["detroit"],
    )
    resolved = _resolve("and Denver?", context)
    assert resolved.intent == "metric_lookup"
    assert resolved.metric == "freeze_day_pct"
    assert resolved.hub_ids == ["denver"]
    assert resolved.hazards == []


def test_assumptions_about_ranking_stay_methodology():
    resolved = _resolve("What assumptions most affect the ranking?")
    assert resolved.intent == "methodology"
    assert resolved.needs_clarification is False


def test_extreme_heat_days_are_a_metric():
    resolved = _resolve("Which hub has the most extreme heat days?")
    assert resolved.intent == "metric_lookup"
    assert resolved.metric == "extreme_heat_day_pct"
    assert resolved.needs_clarification is False


def test_similarity_is_not_a_ranking():
    resolved = _resolve("Which two hubs are most similar?")
    assert resolved.needs_clarification is True
    assert resolved.intent == "unknown"
    assert "isn't a supported comparison" in (resolved.clarifying_question or "")


def test_breakdown_after_a_metric_explains_the_hazard():
    context = SessionContext(
        hub_ids=["denver"],
        focus_hub_id="denver",
        metric="snowfall_day_pct",
        last_intent="metric_lookup",
        last_result_hub_ids=["denver"],
    )
    resolved = _resolve("Show the full breakdown", context)
    assert resolved.intent == "explain_hub"
    assert resolved.hub_ids == ["denver"]
    assert resolved.detail == "breakdown"
    assert resolved.hazards == ["winter"]
    assert resolved.metric is None
