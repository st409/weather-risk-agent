"""Route a chat turn: parse intent, score in Python, then draft the reply."""

from __future__ import annotations

import json
import logging
import re
import uuid

from app.agent.composer import (
    apply_model_prose,
    canned_answer,
    clarification_answer,
    deterministic_answer,
    evidence_block,
)
from app.agent.llm import llm_enabled, request_answer, request_intent
from app.agent.session_context import (
    HAZARDS,
    METRICS,
    WINDOWS,
    ParsedMessage,
    SessionContext,
    parse_message,
    resolve_intent,
)
from app.db import repository
from app.risk.engine import public_report, score_selection
from app.risk.hubs import HUBS

logger = logging.getLogger(__name__)


def handle_chat(session_id: str | None, message: str) -> dict:
    text = " ".join(message.strip().split())
    session_id = _clean_session(session_id)
    repository.ensure_session(session_id)
    context = SessionContext.from_dict(repository.get_context(session_id))
    repository.add_message(session_id, "user", text, None)

    preview = parse_message(text)
    formula = bool(re.search(r"\bhow is (?:it|the score|this) calculated\b", text.lower()))
    keep_formula = preview.intent == "methodology" and formula and context.last_intent
    if preview.intent in {"unsupported_language", "greeting", "off_topic", "methodology"} and not keep_formula:
        resolved = resolve_intent(preview, context)
        resolved.question = text
        resolved.prior_question = context.last_user_message
        answer = canned_answer(resolved)
        evidence = _empty_evidence()
        evidence["detail"] = resolved.detail
        payload = _payload(session_id, answer, resolved, evidence)
        repository.add_message(session_id, "assistant", answer.answer, payload)
        return payload

    parsed, intent_note = _choose_parse(text, context)
    if formula and context.last_intent and not parsed.hub_ids:
        parsed.intent = "follow_up"
        parsed.detail = "breakdown"
    resolved = resolve_intent(parsed, context)
    resolved.question = text
    resolved.prior_question = context.last_user_message
    resolved.show_score_legend = not context.score_legend_shown
    if resolved.needs_clarification:
        answer = clarification_answer(resolved.clarifying_question or "Could you narrow that down?")
        if intent_note:
            answer.uncertainties = [intent_note, *answer.uncertainties]
        payload = _payload(session_id, answer, resolved, _empty_evidence())
        repository.add_message(session_id, "assistant", answer.answer, payload)
        return payload

    hub_ids, region = _targets(resolved)
    reports, window_key, _label, _span = score_selection(hub_ids, region, resolved.time_window)
    resolved.time_window = window_key
    if not reports:
        answer = clarification_answer("None of the hubs in this set match that request.")
        payload = _payload(session_id, answer, resolved, _empty_evidence())
        repository.add_message(session_id, "assistant", answer.answer, payload)
        return payload

    evidence = evidence_block(resolved, reports)
    answer = deterministic_answer(resolved, reports)
    if llm_enabled():
        drafted, failure = request_answer(
            text,
            [item.model_dump() for item in answer.scores],
            evidence,
            answer.answer,
            context.for_intent_parser(),
        )
        answer = apply_model_prose(answer, drafted, failure, evidence)
    else:
        answer = apply_model_prose(answer, None, None, evidence)
    if intent_note:
        answer.uncertainties = [intent_note, *answer.uncertainties]

    ordered_ids = [row["hub_id"] for row in evidence["rankings"]]
    if resolved.explain_gap and len(resolved.hub_ids) > 1:
        focus_hub_id = resolved.hub_ids[1]
    elif len(resolved.hub_ids) == 1:
        focus_hub_id = resolved.hub_ids[0]
    else:
        focus_hub_id = None
    keep_ranking = bool(context.last_result_hub_ids) and (resolved.explain_gap or resolved.inherited)
    repository.save_context(
        session_id,
        SessionContext(
            hub_ids=[focus_hub_id] if focus_hub_id else (list(resolved.hub_ids) or ordered_ids),
            region=resolved.region,
            hazards=list(resolved.hazards),
            metric=resolved.metric if resolved.intent == "metric_lookup" else None,
            time_window=window_key,
            last_intent=resolved.intent,
            last_result_hub_ids=list(context.last_result_hub_ids) if keep_ranking else ordered_ids,
            last_user_message=text,
            offered_breakdown=any("full score breakdown" in item.lower() for item in answer.follow_up_questions),
            focus_hub_id=focus_hub_id,
            excluded_hazards=list(resolved.excluded_hazards),
            score_legend_shown=context.score_legend_shown or "higher = more exposed" in answer.answer,
        ).to_dict(),
    )
    for report in reports:
        repository.save_score_snapshot(public_report(report))
    payload = _payload(session_id, answer, resolved, evidence)
    repository.add_message(session_id, "assistant", answer.answer, payload)
    return payload


def _choose_parse(message: str, context: SessionContext) -> tuple[ParsedMessage, str | None]:
    rules = parse_message(message)
    if not llm_enabled():
        return rules, None
    catalog = "\n".join(f"{hub.hub_id}: {hub.name}, {hub.region}" for hub in HUBS)
    try:
        model_intent, note = request_intent(message, json.dumps(context.for_intent_parser(), ensure_ascii=True), catalog)
    except Exception as exc:
        logger.warning("intent model failed: %s", exc)
        return rules, f"language model request failed ({exc.__class__.__name__})"
    if model_intent is None:
        return rules, note
    return _merge(rules, model_intent, context), None


def _merge(rules: ParsedMessage, model_intent, context: SessionContext) -> ParsedMessage:
    # The model already received previous_turn. On a follow-up it cannot replace
    # the hubs from that turn; resolve_intent copies them.
    named_hubs = list(rules.hub_ids)
    if (
        model_intent.intent == "follow_up"
        and rules.intent == "explain_hub"
        and not named_hubs
        and context.last_result_hub_ids
    ):
        rules.intent = "follow_up"
        if rules.ordinal_index is None:
            rules.ordinal_index = 0
    if rules.intent in {"alerts", "methodology", "greeting", "off_topic", "unsupported_language"}:
        return rules
    if rules.intent == "follow_up" or (rules.intent == "unknown" and model_intent.intent == "follow_up"):
        rules.intent = "follow_up"
        if not rules.hazards_explicit:
            hazards = [hazard for hazard in model_intent.hazards if hazard in HAZARDS and hazard not in rules.excluded_hazards]
            if hazards:
                rules.hazards = hazards
                rules.hazards_explicit = True
        if rules.time_window is None and model_intent.time_window in WINDOWS and not rules.unsupported_window:
            rules.time_window = model_intent.time_window
        return rules
    if rules.intent == "unknown" and model_intent.intent in {
        "rank_hubs",
        "compare_hubs",
        "metric_lookup",
        "explain_hub",
        "follow_up",
    }:
        rules.intent = model_intent.intent
    if not named_hubs:
        rules.hub_ids = []
    if not rules.region_explicit:
        rules.region = None
    if not rules.hazards_explicit:
        hazards = [hazard for hazard in model_intent.hazards if hazard in HAZARDS]
        if hazards:
            rules.hazards = hazards
            rules.hazards_explicit = True
    if rules.metric is None and model_intent.metric in METRICS:
        rules.metric = model_intent.metric
    if rules.time_window is None and model_intent.time_window in WINDOWS and not rules.unsupported_window:
        rules.time_window = model_intent.time_window
    if rules.unsupported_window:
        rules.time_window = "last_calendar_year"
    return rules


def _targets(resolved) -> tuple[list[str] | None, str | None]:
    if resolved.intent == "alerts":
        return None, None
    if resolved.hub_ids:
        return list(resolved.hub_ids), None
    return None, resolved.region


def _payload(session_id: str, answer, resolved, evidence: dict) -> dict:
    return {
        "session_id": session_id,
        "answer": answer.model_dump(),
        "intent": resolved.view(),
        "evidence": evidence,
    }


def _empty_evidence() -> dict:
    return {
        "window_label": "",
        "window_start": "",
        "window_end": "",
        "focus_hazards": [],
        "safety_framing": False,
        "used_fallback_weather": False,
        "rankings": [],
        "metric": None,
    }


def _clean_session(session_id: str | None) -> str:
    if session_id and re.fullmatch(r"[A-Za-z0-9_-]{8,64}", session_id):
        return session_id
    return uuid.uuid4().hex
