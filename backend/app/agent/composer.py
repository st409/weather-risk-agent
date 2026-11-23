"""Deterministic briefings. The model may replace the prose later; scores stay here."""

from __future__ import annotations

import logging
import math
import re

logger = logging.getLogger(__name__)

from app.agent.schemas import AnswerDraft, AnswerResult, ScoreItem, SourceItem
from app.agent.session_context import ResolvedIntent, _unsupported_window
from app.risk.hubs import HUBS
from app.risk.scoring import metric_payload
from app.risk.types import HubReport, SourceRef

NRI_NOTE = (
    "FEMA hazard scores for these large counties often sit near the top of the 0-100 scale "
    "because the index includes expected loss, not only how often the weather happens. "
    "Daily rates are what separate saturated baselines."
)

HAZARD_LABEL = {
    "winter": "winter",
    "flood": "flood",
    "hurricane": "hurricane",
    "heat": "heat",
    "severe_storm": "severe storm",
    "overall": "overall disruption",
}


OVERALL_WEIGHTS = {
    "winter": 0.30,
    "flood": 0.25,
    "hurricane": 0.20,
    "severe_storm": 0.15,
    "heat": 0.10,
}

EXAMPLES = (
    "Which hub is most exposed to flooding?",
    "Compare Miami and Houston on hurricane and flood.",
    "How many snow days did Denver have last year?",
)


def deterministic_answer(resolved: ResolvedIntent, reports: list[HubReport]) -> AnswerResult:
    if resolved.intent == "alerts":
        return _alerts_answer(reports)
    if resolved.explain_gap:
        return _gap_answer(resolved, reports)
    ordered = _order_for(resolved, reports)
    if resolved.intent == "metric_lookup":
        return _metric_answer(resolved, ordered)
    if resolved.intent == "compare_hubs":
        return _compare_answer(resolved, ordered)
    if resolved.intent == "explain_hub":
        return _explain_answer(resolved, ordered)
    return _rank_answer(resolved, ordered)


def clarification_answer(question: str) -> AnswerResult:
    return AnswerResult(
        answer=question,
        key_findings=[],
        scores=[],
        assumptions=[
            "This demo only scores ten fixed logistics hubs, so a place or comparison outside that set gets one clarifying question instead of a guess."
        ],
        uncertainties=[],
        sources=[
            SourceItem(
                name="Fixed hub list",
                source_type="static_public_dataset",
                retrieved_at="2026-11-23",
                uncertainty_note="Hub names, coordinates, and regions are maintained in code for this demo.",
            )
        ],
        follow_up_questions=[
            "Which hubs in this set are most exposed to winter disruption?",
            "Compare two hubs on one hazard.",
            "How many snow days did one hub have last year?",
        ],
    )


def order_reports(reports: list[HubReport], hazards: list[str], safer_first: bool) -> list[HubReport]:
    return sorted(reports, key=lambda report: report.focus_score(hazards), reverse=not safer_first)


METRIC_HAZARD = {
    "snowfall_day_pct": "winter",
    "freeze_day_pct": "winter",
    "heat_day_pct": "heat",
    "extreme_heat_day_pct": "heat",
    "heavy_rain_day_pct": "flood",
    "very_heavy_rain_day_pct": "flood",
}


def evidence_block(resolved: ResolvedIntent, reports: list[HubReport]) -> dict:
    focus = [] if resolved.intent == "metric_lookup" else list(resolved.hazards)
    ordered = _order_for(resolved, reports)
    metric = None
    if resolved.intent == "metric_lookup" and ordered and resolved.metric:
        metric = metric_payload(ordered[0], resolved.metric)
        if metric and metric.get("value") is not None:
            metric = {**metric, "value": round(float(metric["value"]), 1)}
    rows = []
    for report in ordered:
        row = _ranking_row(report, focus)
        if resolved.intent == "metric_lookup":
            row["drivers"] = []
        if resolved.excluded_hazards:
            total, parts = _renormalized(report, resolved.excluded_hazards)
            shown = _allocate_tenths([part[2] for part in parts], total)
            row["overall_score"] = _one_decimal(total)
            row["focus_score"] = _one_decimal(total)
            row["hazard_scores"] = {name: points for (name, _score, _contrib), points in zip(parts, shown)}
        rows.append(row)
    if resolved.intent == "metric_lookup" and metric:
        breakdown = f"{metric.get('numerator')} days, {metric.get('value')}% of {metric.get('denominator')} valid days."
        metric_rows = []
        for report in ordered:
            payload = metric_payload(report, resolved.metric or "")
            if payload and payload.get("value") is not None:
                metric_rows.append({**payload, "value": round(float(payload["value"]), 1)})
        details: dict = {"metric": metric, "metric_rows": metric_rows}
    elif ordered:
        breakdown = _full_breakdown(resolved, ordered)
        details = {"hubs": _detail_hubs(resolved, ordered)}
    else:
        breakdown = ""
        details = {}
    window = ordered[0] if ordered else None
    return {
        "window_label": window.window_label if window else "",
        "window_start": window.window_start if window else "",
        "window_end": window.window_end if window else "",
        "focus_hazards": focus,
        "safety_framing": resolved.safety_framing,
        "used_fallback_weather": any(report.weather_fallback for report in ordered),
        "rankings": rows,
        "metric": metric,
        "detail": resolved.detail,
        "scope": _scope_sentence(resolved, ordered) if ordered else "",
        "breakdown": breakdown,
        "details": details,
        "unsupported_window": _asks_unsupported_window(resolved),
        "required_notice": _window_notice(resolved),
        "lock_breakdown": resolved.detail in {"breakdown", "evidence"} and resolved.intent != "metric_lookup",
        "required_driver_phrase": _required_driver_phrase(resolved, subject) if (subject := _subject_report(resolved, ordered)) else "",
        "driver_score_traps": _driver_score_traps(subject, resolved) if subject and resolved.intent != "metric_lookup" else [],
    }


def _one_decimal(value: float) -> float:
    return round(float(value) + 0.0, 1)


def apply_model_prose(
    base: AnswerResult,
    drafted: AnswerDraft | None,
    failure_note: str | None,
    evidence: dict | None = None,
) -> AnswerResult:
    """Keep every score, finding, assumption, and source that Python computed."""
    uncertainties = list(base.uncertainties)
    assumptions = list(base.assumptions)
    if drafted is not None:
        cleaned = _prepare_draft(drafted.answer, base.answer)
        notice = str((evidence or {}).get("required_notice") or "")
        if notice and notice not in cleaned:
            cleaned = f"{notice} {cleaned}".strip()
        reason = _draft_rejection(cleaned, evidence, base.scores, base.answer)
        if reason:
            logger.warning("draft rejected: %s | draft: %s", reason, cleaned[:240].replace("\n", " "))
            drafted = None
            failure_note = reason
        else:
            drafted = drafted.model_copy(update={"answer": cleaned})
    if drafted is None and failure_note:
        logger.warning("answer skeleton shown: %s", failure_note)
    if drafted is None:
        if not failure_note:
            assumptions.append("Wording is assembled directly from the scored evidence.")
        return _lock_breakdown(
            base.model_copy(
                update={
                    "assumptions": _unique(assumptions),
                    "uncertainties": _unique(uncertainties),
                    "fallback": bool(failure_note),
                    "rejection_reason": failure_note,
                }
            ),
            evidence,
        )
    answer = drafted.answer.strip()
    return _lock_breakdown(
        AnswerResult(
            answer=answer,
            key_findings=list(base.key_findings),
            scores=list(base.scores),
            assumptions=_unique(assumptions),
            uncertainties=_unique(uncertainties),
            sources=list(base.sources),
            follow_up_questions=list(base.follow_up_questions)[:4],
        ),
        evidence,
    )


def _lock_breakdown(result: AnswerResult, evidence: dict | None) -> AnswerResult:
    """The contributor lines stay the code's text, appended after whatever the model wrote."""
    if not evidence or not evidence.get("lock_breakdown"):
        return result
    block = str(evidence.get("breakdown") or "").strip()
    if not block or block in result.answer:
        return result
    return result.model_copy(update={"answer": f"{result.answer.rstrip()} {block}"})


def _normalize_degree(text: str) -> str:
    return text.replace("°", " ").replace("º", " ")


def _next_count_clause(packed: list[tuple[HubReport, dict]], seen: set[str]) -> str:
    """Name every hub tied for the next day count, not only the first of them."""
    rest = packed[1:]
    if not rest:
        return ""
    next_count = rest[0][1]["numerator"]
    tied = [(report, payload) for report, payload in rest if payload["numerator"] == next_count]
    if len(tied) == 1:
        other, second = tied[0]
        return f" Next is {_mention(other, seen)} at {second['numerator']} of {second['denominator']}, {second['value']:.1f}%."
    names = _join([_mention(report, seen) for report, _payload in tied])
    sample = tied[0][1]
    return f" Next are {names}, each {sample['numerator']} of {sample['denominator']}, {sample['value']:.1f}%."


def _scores_line(resolved: ResolvedIntent, reports: list[HubReport], hazard: str | None = None) -> str:
    bits = []
    for report in reports:
        if hazard is None:
            value = _rank_score(resolved, report)
        else:
            value = report.focus_score([] if hazard == "overall" else [hazard])
        bits.append(f"{report.name} {_n(value)}")
    if not bits:
        return ""
    line = "Scores: " + ", ".join(bits)
    if resolved.show_score_legend:
        line += " (higher = more exposed)"
    return line + "."


def _compare_scores_line(resolved: ResolvedIntent, reports: list[HubReport], hazards: list[str]) -> str:
    """Label each hazard when a comparison covers more than one."""
    shown = [hazard for hazard in hazards if hazard != "overall"] or ["overall"]
    groups = []
    for hazard in shown:
        key = [] if hazard == "overall" else [hazard]
        ordered = sorted(reports, key=lambda report: report.focus_score(key), reverse=True)
        bits = [f"{report.name} {_n(report.focus_score(key))}" for report in ordered]
        groups.append(", ".join(bits) if len(shown) == 1 else f"{HAZARD_LABEL[hazard]} " + ", ".join(bits))
    line = "Scores: " + "; ".join(groups)
    if resolved.show_score_legend:
        line += " (higher = more exposed)"
    return line + "."


def _tied_leaders(resolved: ResolvedIntent, reports: list[HubReport]) -> list[HubReport]:
    if not reports:
        return []
    lead = _n(_rank_score(resolved, reports[0]))
    return [report for report in reports if _n(_rank_score(resolved, report)) == lead]


def _plural_question(resolved: ResolvedIntent) -> bool:
    """'Which hubs' and 'rank' name several hubs. 'Which hub' names one.

    A trailing-365 follow-up keeps the previous ranking's plural shape.
    """
    question = (resolved.question or "").lower()
    prior = (resolved.prior_question or "").lower()
    singular = r"\bwhich hub\b|\briskiest hub\b|\bsafest hub\b|\bone hub\b"
    plural = r"\bwhich hubs\b|\brank\b|\bhubs\b"
    if re.search(singular, question):
        return False
    if re.search(plural, question) and not re.search(r"\bwhich hub\b", question):
        return True
    if resolved.inherited and re.search(plural, prior) and not re.search(r"\bwhich hub\b", prior):
        return True
    return False


def _topic_phrase(resolved: ResolvedIntent) -> str:
    if resolved.excluded_hazards:
        dropped = _join([HAZARD_LABEL[hazard] for hazard in resolved.excluded_hazards])
        return f"weather disruption after removing {dropped}"
    if not resolved.hazards:
        return "weather disruption"
    label = _focus_label(resolved.hazards)
    if label == "severe storm":
        return "severe storms"
    if label == "hurricane":
        return "hurricanes"
    return f"{label} disruption"


def _rank_opening(resolved: ResolvedIntent, reports: list[HubReport], seen: set[str]) -> str:
    leader = reports[0]
    tied = _tied_leaders(resolved, reports)
    topic = _topic_phrase(resolved)
    if resolved.asks_investment and not resolved.safety_framing:
        if len(tied) >= 2:
            return f"{_name_list(tied[:3])} are effectively tied as the most vulnerable to {topic}."
        return f"{leader.name} is the most vulnerable hub to {topic}."
    if len(tied) >= 2 and not resolved.safety_framing:
        names = _name_list(tied[:3])
        return f"{names} are effectively tied as the most exposed to {topic}."
    if resolved.safety_framing:
        return f"{leader.name} is the least exposed hub to weather disruption."
    if resolved.excluded_hazards and not (_plural_question(resolved) and len(reports) >= 2):
        dropped = _join([HAZARD_LABEL[hazard] for hazard in resolved.excluded_hazards])
        return f"{leader.name} is the most exposed hub after removing {dropped}."
    if _plural_question(resolved) and len(reports) >= 2:
        names = _name_list(reports[:3])
        gap = abs(_rank_score(resolved, reports[0]) - _rank_score(resolved, reports[1]))
        ahead = "slightly ahead" if gap <= 5 else "on top"
        return f"{names} are the most exposed to {topic}, with {leader.name} {ahead}."
    return f"{leader.name} is the most exposed hub to {topic}."


def _rank_reason(resolved: ResolvedIntent, reports: list[HubReport], seen: set[str]) -> str:
    if resolved.safety_framing and len(reports) > 1:
        others = _join([report.name for report in reports[1:3]])
        return f"{others} are riskier."
    if resolved.excluded_hazards:
        return _remaining_reason(reports[0], resolved.excluded_hazards, reports)
    return _plain_reason(reports[0], resolved.hazards, reports)


def _remaining_reason(report: HubReport, excluded: list[str], peers: list[HubReport]) -> str:
    """After a hazard is removed, the reason uses the hazards that are still in the score."""
    _total, parts = _renormalized(report, excluded)
    names = [name for name, _score, _contribution in parts][:2]
    if not names:
        return ""
    labels = _join([HAZARD_LABEL[name] for name in names])
    lead = f"The remaining driver is {labels}" if len(names) == 1 else f"The remaining drivers are {labels}"
    fact = ""
    for name in names:
        for clause in _plain_clauses(report, [name], peers):
            if _mentions_hazard(clause, excluded):
                continue
            fact = clause if clause.endswith(".") else clause + "."
            break
        if fact:
            break
    if not fact:
        return lead + "."
    return f"{lead}. {fact}"


def _mentions_hazard(text: str, hazards: list[str]) -> bool:
    for hazard in hazards:
        if re.search(rf"\b{re.escape(HAZARD_LABEL[hazard])}\b", text, re.I):
            return True
    return False


def _scored_reports(resolved: ResolvedIntent, reports: list[HubReport]) -> list[HubReport]:
    """Scores name only the hubs the opening already named."""
    if not reports:
        return []
    tied = _tied_leaders(resolved, reports)
    if len(tied) >= 2 and not resolved.safety_framing:
        return tied[:3]
    if resolved.safety_framing or (_plural_question(resolved) and len(reports) >= 2):
        return reports[:3]
    return reports[:1]


def _behind_clause(resolved: ResolvedIntent, reports: list[HubReport], seen: set[str], word: str) -> str:
    lead = _n(_rank_score(resolved, reports[0]))
    parts = []
    for other in reports[1:3]:
        if _n(_rank_score(resolved, other)) == lead:
            continue
        drivers = _driver_words(other, resolved.hazards or ["overall"], resolved.excluded_hazards, limit=2)
        name = _mention(other, seen)
        if drivers:
            parts.append(f"{name} is {word}, driven by {drivers}")
        else:
            parts.append(f"{name} is {word}")
    if not parts:
        return ""
    return " ".join(f"{part}." for part in parts)


def _lead_fact(report: HubReport, hazards: list[str], peers: list[HubReport]) -> str:
    for driver in _ranked_drivers(report, hazards or ["overall"]):
        counted = _count_fact(report, driver.metric_name, peers)
        if counted:
            return counted
        if driver.metric_name.endswith("_baseline_score"):
            label = _short_driver(driver).removeprefix("the ")
            return f"The main reason is {label}."
        if driver.metric_name.endswith("_alert_score"):
            if report.active_events:
                return f"The main reason is active alerts: {_join(report.active_events)}."
            return "The main reason is the alert history for this hazard."
    return ""


def _count_fact(report: HubReport, metric_name: str, peers: list[HubReport]) -> str:
    spec = {
        "freeze_days_pct": ("freeze_days", "freeze_valid_days", "freezing days"),
        "snow_days_pct": ("snow_days", "snow_valid_days", "snow days"),
        "extreme_heat_days_pct": ("extreme_heat_days", "temp_max_valid_days", "extreme heat days"),
        "heat_days_pct": ("heat_days", "temp_max_valid_days", "days reaching 95 F"),
        "heavy_rain_days_pct": ("heavy_rain_days", "precip_valid_days", "heavy-rain days"),
        "very_heavy_rain_days_pct": ("very_heavy_rain_days", "precip_valid_days", "very-heavy-rain days"),
        "high_wind_days_pct": ("high_wind_days", "wind_valid_days", "high-wind days"),
    }
    if metric_name not in spec:
        return ""
    count_name, valid_name, noun = spec[metric_name]
    count = int(getattr(report.metrics, count_name) or 0)
    valid = int(getattr(report.metrics, valid_name) or 0)
    year = "2025" if str(report.window_start).startswith("2025") or "2025" in report.window_label else report.window_label
    text = f"In {year} it had {count} {noun}"
    if valid and metric_name == "freeze_days_pct" and count / valid >= 1 / 3:
        text += ", more than a third of the year"
    if peers and all(count >= int(getattr(other.metrics, count_name) or 0) for other in peers):
        text += " and the most of all 10 hubs" if len(peers) == 10 else " and the most in this set" if len(peers) > 1 else ""
    return text + "."


def _primary_pair(reports: list[HubReport], focus: list[str], safer: bool) -> tuple[float, str, HubReport, HubReport]:
    gaps = []
    for hazard in focus:
        key = [] if hazard == "overall" else [hazard]
        scored = sorted(reports, key=lambda report: report.focus_score(key), reverse=not safer)
        high, low = scored[0], scored[-1]
        gap = abs(high.focus_score(key) - low.focus_score(key))
        gaps.append((gap, hazard, high, low))
    gaps.sort(key=lambda item: item[0], reverse=True)
    return gaps[0]


def _compare_order(reports: list[HubReport], focus: list[str], safer: bool) -> list[HubReport]:
    _gap, hazard, _high, _low = _primary_pair(reports, focus, safer)
    key = [] if hazard == "overall" else [hazard]
    return sorted(reports, key=lambda report: report.focus_score(key), reverse=True)


def _compare_opening(reports: list[HubReport], focus: list[str], seen: set[str], safer: bool) -> str:
    gap, hazard, first, last = _primary_pair(reports, focus, safer)
    label = HAZARD_LABEL[hazard]
    key = [] if hazard == "overall" else [hazard]
    higher = first if first.focus_score(key) >= last.focus_score(key) else last
    lower = last if higher is first else first
    high_name = _mention(higher, seen)
    low_name = _mention(lower, seen)
    if _n(higher.focus_score(key)) == _n(lower.focus_score(key)):
        return f"{high_name} and {low_name} are effectively tied on {label}."
    if gap <= 1.0:
        return f"{high_name} and {low_name} are practically tied on {label}."
    return f"{high_name} is more exposed to {label} than {low_name}."


def _explain_opening(resolved: ResolvedIntent, report: HubReport, seen: set[str]) -> str:
    if resolved.excluded_hazards:
        dropped = _join([HAZARD_LABEL[hazard] for hazard in resolved.excluded_hazards])
        return f"{report.name} is the most exposed hub after removing {dropped}."
    if resolved.hazards:
        return f"{report.name} faces high {_topic_phrase(resolved)}."
    return f"{report.name} faces high weather disruption risk."


def _explain_reason(resolved: ResolvedIntent, report: HubReport) -> str:
    if resolved.excluded_hazards:
        return _remaining_reason(report, resolved.excluded_hazards, [report])
    return _plain_reason(report, resolved.hazards, [report])


def _plain_reason(report: HubReport, hazards: list[str], peers: list[HubReport]) -> str:
    """One or two concrete facts. No internal driver names and no hazard scores."""
    clauses = _plain_clauses(report, hazards, peers)
    if not clauses:
        return ""
    if len(clauses) == 1:
        return clauses[0] if clauses[0].endswith(".") else clauses[0] + "."
    second = clauses[1]
    if second.startswith("An "):
        second = "an " + second[3:]
    return f"{clauses[0].rstrip('.')}, and {second}."


def _plain_clauses(report: HubReport, hazards: list[str], peers: list[HubReport]) -> list[str]:
    clauses: list[str] = []
    for driver in _fact_drivers(report, hazards):
        clause = _plain_clause(report, driver, peers)
        if clause and clause not in clauses:
            clauses.append(clause)
        if len(clauses) >= 2:
            break
    return clauses


def _fact_drivers(report: HubReport, hazards: list[str]):
    found = []
    for driver in _ranked_drivers(report, hazards or ["overall"]):
        hazard = str(driver.metric_name).removesuffix("_score")
        if hazard in {"winter", "flood", "hurricane", "heat", "severe_storm"} and not str(driver.metric_name).endswith("baseline_score"):
            found.extend(_ranked_drivers(report, [hazard])[:3])
        else:
            found.append(driver)
    return found


def _plain_clause(report: HubReport, driver, peers: list[HubReport]) -> str:
    counted = _count_clause(report, driver.metric_name, peers)
    if counted:
        return counted
    hazard = _BASELINE_HAZARD.get(driver.metric_name)
    if hazard and driver.value is not None and float(driver.value) >= 50:
        return f"FEMA rates it a high historical {hazard}-risk area"
    if str(driver.metric_name).endswith("_alert_score") and report.active_events:
        return f"an active {_join(report.active_events)} alert"
    return ""


_COUNT_FIELDS = {
    "freeze_days_pct": ("freeze_days", "freezing days"),
    "snow_days_pct": ("snow_days", "snow days"),
    "extreme_heat_days_pct": ("extreme_heat_days", "extreme heat days"),
    "heat_days_pct": ("heat_days", "days reaching 95 F"),
    "heavy_rain_days_pct": ("heavy_rain_days", "heavy-rain days"),
    "very_heavy_rain_days_pct": ("very_heavy_rain_days", "very-heavy-rain days"),
    "high_wind_days_pct": ("high_wind_days", "high-wind days"),
}

_BASELINE_HAZARD = {
    "winter_baseline_score": "winter",
    "flood_baseline_score": "flood",
    "hurricane_baseline_score": "hurricane",
    "heat_baseline_score": "heat",
    "severe_baseline_score": "severe-storm",
}


def _count_clause(report: HubReport, metric_name: str, peers: list[HubReport]) -> str:
    spec = _COUNT_FIELDS.get(metric_name)
    if spec is None:
        return ""
    count_name, noun = spec
    count = int(getattr(report.metrics, count_name) or 0)
    year = "2025" if str(report.window_start).startswith("2025") or "2025" in report.window_label else report.window_label
    text = f"{report.name} had {count} {noun} in {year}"
    if peers and len(peers) > 1 and all(count >= int(getattr(other.metrics, count_name) or 0) for other in peers):
        text += " and the most of all hubs" if len(peers) >= 10 else " and the most in this set"
    return text


def _rank_answer(resolved: ResolvedIntent, reports: list[HubReport]) -> AnswerResult:
    focus = resolved.hazards
    label = _focus_label(focus)
    if not reports:
        return clarification_answer("None of the hubs in this set match that region.")
    seen: set[str] = set()
    sentences = [_rank_opening(resolved, reports, seen), _rank_reason(resolved, reports, seen)]
    if resolved.asks_investment:
        sentences.append(_investment_sentence(reports[0]))
    sentences.append(_scores_line(resolved, _scored_reports(resolved, reports)))
    text = _finish_text(resolved, reports, sentences)
    findings = [f"{report.name} {label} score: {_n(_rank_score(resolved, report))}" for report in reports[:5]]
    leader = reports[0]
    driver = _top_driver(leader, focus)
    if driver:
        findings.append(f"Largest {label} driver for {leader.name}: {driver.metric_name} at {driver.value:.1f} (contribution {driver.contribution:.1f}).")
    return _pack(resolved, reports, text, findings, _followups_for(resolved, reports))


def _compare_answer(resolved: ResolvedIntent, reports: list[HubReport]) -> AnswerResult:
    focus = resolved.hazards or ["overall"]
    findings = []
    seen: set[str] = set()
    _verdict, gaps = _compare_verdict(reports, focus, set(), resolved.safety_framing)
    for hazard in focus:
        for report in reports:
            label = HAZARD_LABEL[hazard]
            findings.append(f"{report.name} {label}: {_n(report.focus_score([hazard] if hazard != 'overall' else []))}")
    hazard_focus = [hazard for hazard in focus if hazard != "overall"]
    _gap, primary, _high, _low = _primary_pair(reports, focus, resolved.safety_framing)
    ordered = _compare_order(reports, focus, resolved.safety_framing)
    opening = _compare_opening(ordered, focus, seen, resolved.safety_framing)
    reason = " ".join(
        part
        for part in (
            _baselines_once(ordered, hazard_focus, seen),
            _short_separator(ordered, hazard_focus),
            " ".join(gap for gap in gaps if "vs" not in gap and not re.search(r"\d", gap)),
        )
        if part
    )
    text = _finish_text(resolved, ordered, [opening, reason, _compare_scores_line(resolved, ordered, focus)])
    return _pack(resolved, reports, text, findings[:8], _followups_for(resolved, reports))


def _explain_answer(resolved: ResolvedIntent, reports: list[HubReport]) -> AnswerResult:
    report = reports[0]
    focus = resolved.hazards
    seen: set[str] = set()
    sentences = [_explain_opening(resolved, report, seen), _explain_reason(resolved, report)]
    if resolved.asks_investment:
        sentences.append(_investment_sentence(report))
    sentences.append(_scores_line(resolved, [report]))
    text = _finish_text(resolved, [report], sentences)
    findings = [f"{HAZARD_LABEL[hazard].capitalize()} score: {_n(report.hazards[hazard].score)}" for hazard in ("winter", "flood", "hurricane", "severe_storm", "heat")]
    findings.insert(0, f"Overall score: {_n(report.overall_score)}")
    return _pack(resolved, [report], text, findings, _followups_for(resolved, [report]))


def _metric_answer(resolved: ResolvedIntent, reports: list[HubReport]) -> AnswerResult:
    packed = []
    for report in reports:
        payload = metric_payload(report, resolved.metric or "")
        if payload is not None and payload["value"] is not None:
            packed.append((report, payload))
    if not packed:
        return clarification_answer("I couldn't compute that percentage from the days on hand. Try snowfall, freeze, heat, or heavy rain.")
    packed.sort(key=lambda item: (item[1]["numerator"] if item[1]["numerator"] is not None else -1, item[1]["value"]), reverse=True)
    if len(packed) == 1:
        return _one_metric_answer(resolved, packed[0][0], packed[0][1])
    return _metric_group_answer(resolved, packed)


def _one_metric_answer(resolved: ResolvedIntent, report: HubReport, payload: dict) -> AnswerResult:
    value = payload["value"]
    noun = {"snowfall_day_pct": "snow days", "freeze_day_pct": "freezing days"}.get(resolved.metric or "", payload["label"].lower())
    threshold = _metric_threshold(resolved.metric or "")
    seen: set[str] = set()
    direct = f"{_mention(report, seen)} had {payload['numerator']} {noun} ({value:.1f}% of the year). {threshold}"
    text = _finish_text(resolved, [report], [direct])
    findings = [
        f"{payload['label']}: {value:.1f}%",
        f"{payload['numerator']} days met the threshold out of {payload['denominator']} valid days.",
        f"Window: {report.window_label}.",
    ]
    scores = [_metric_score(report, payload)]
    answer = _base(resolved, [report], text, findings, _followups_for(resolved, [report]))
    return answer.model_copy(update={"scores": scores})


def _metric_group_answer(resolved: ResolvedIntent, packed: list[tuple[HubReport, dict]]) -> AnswerResult:
    """Compare or rank the day count itself. Do not substitute the winter index."""
    seen: set[str] = set()
    threshold = _metric_threshold(resolved.metric or "")
    leader, lead = packed[0]

    def _count(report: HubReport, payload: dict) -> str:
        return f"{_mention(report, seen)} ({payload['numerator']} days, {payload['value']:.1f}%)"

    if len(packed) == 2:
        other, second = packed[1]
        direct = f"{_count(leader, lead)} over {_count(other, second)}."
    else:
        noun = _metric_noun(resolved.metric or "", lead["label"])
        direct = (
            f"{_mention(leader, seen)} has the most {noun}: {lead['numerator']} of {lead['denominator']}, {lead['value']:.1f}%."
        )
        direct += _next_count_clause(packed, seen)
    direct = f"{direct} {threshold}"
    reports = [report for report, _payload in packed]
    text = _finish_text(resolved, reports, [direct])
    findings = [f"{payload['hub_name']}: {payload['numerator']} days, {payload['value']:.1f}%" for _report, payload in packed[:5]]
    answer = _base(resolved, reports[:5], text, findings, _followups_for(resolved, reports[:3]))
    return answer.model_copy(update={"scores": [_metric_score(report, payload) for report, payload in packed]})


def _metric_score(report: HubReport, payload: dict) -> ScoreItem:
    return ScoreItem(
        hub_id=report.hub_id,
        hub_name=report.name,
        hazard=payload["name"],
        score=_one_decimal(payload["value"]),
        unit="percent",
    )


def _pack(resolved: ResolvedIntent, reports: list[HubReport], text: str, findings: list[str], followups: list[str]) -> AnswerResult:
    return _base(resolved, reports, text, findings, followups)


def _base(resolved: ResolvedIntent, reports: list[HubReport], text: str, findings: list[str], followups: list[str]) -> AnswerResult:
    questions = _filter_followups(resolved, reports, followups)
    offer = "Want the full score breakdown?"
    if resolved.detail in {"breakdown", "evidence"}:
        questions = [question for question in questions if question != offer]
    else:
        questions = [offer, *[question for question in questions if question != offer]]
    return AnswerResult(
        answer=text,
        key_findings=findings[:6],
        scores=_score_items(resolved, reports),
        assumptions=_assumptions(resolved, reports),
        uncertainties=_uncertainties(resolved, reports),
        sources=_source_items(reports),
        follow_up_questions=questions[:3],
    )


def _score_items(resolved: ResolvedIntent, reports: list[HubReport]) -> list[ScoreItem]:
    if resolved.excluded_hazards:
        items: list[ScoreItem] = []
        for report in reports:
            total, parts = _renormalized(report, resolved.excluded_hazards)
            shown = _allocate_tenths([part[2] for part in parts], total)
            items.append(
                ScoreItem(
                    hub_id=report.hub_id,
                    hub_name=report.name,
                    hazard="overall",
                    score=_one_decimal(total),
                    unit="index",
                )
            )
            for (name, _score, _contribution), points in zip(parts, shown):
                items.append(
                    ScoreItem(
                        hub_id=report.hub_id,
                        hub_name=report.name,
                        hazard=name,
                        score=points,
                        unit="index",
                    )
                )
        return items
    hazards = resolved.hazards or ["overall"]
    if resolved.intent == "explain_hub" and reports:
        hazards = ["overall", "winter", "flood", "hurricane", "severe_storm", "heat"]
    items = []
    for report in reports:
        for hazard in hazards:
            if hazard == "overall":
                value = report.overall_score
            elif hazard not in report.hazards:
                continue
            else:
                value = report.hazards[hazard].score
            items.append(
                ScoreItem(
                    hub_id=report.hub_id,
                    hub_name=report.name,
                    hazard=hazard,
                    score=_one_decimal(value),
                    unit="index",
                )
            )
    return items


def _assumptions(resolved: ResolvedIntent, reports: list[HubReport]) -> list[str]:
    window = reports[0].window_label if reports else "the last complete calendar year"
    focus = set(resolved.hazards)
    items = [
        f"Weather-day rates use {window}. National Weather Service alerts describe what is active now, not that historical window.",
        "Scores are capped linear indexes from 0 to 100, not the probability of a missed shipment.",
        "Business-importance weights are stored as 1.0 and are not applied.",
    ]
    if not focus or "winter" in focus or resolved.intent in {"explain_hub", "rank_hubs"}:
        items.append("A snow day is daily snowfall >= 1 cm. A freeze day has a minimum of 32 F or lower.")
    if not focus or "flood" in focus or resolved.intent == "explain_hub":
        items.append("Heavy rain is at least 1 inch in a day, and very heavy rain is at least 2 inches. The flood baseline is the greater of the inland and coastal FEMA scores.")
    if "hurricane" in focus or (resolved.intent == "explain_hub" and any(report.hurricane_published for report in reports)):
        items.append("The hurricane coastal modifier is 100 only for Miami, Houston, and New York/Newark.")
    if not focus or "heat" in focus or resolved.intent == "explain_hub":
        items.append("A heat day has a maximum of at least 95 F. An extreme heat day reaches at least 100 F.")
    if not focus or "severe_storm" in focus or resolved.intent == "explain_hub":
        items.append("The severe-storm baseline averages the FEMA tornado, lightning, hail, and strong-wind scores. A high-wind day gusts to at least 40 mph.")
    if resolved.safety_framing:
        items.append("Safer means a lower disruption score on the hazards in view. It is not an emergency or insurance judgment.")
    if resolved.intent == "metric_lookup":
        items = [
            f"The count uses valid observation days in {window}, skipping days where that field was missing.",
            _metric_threshold(resolved.metric or "").rstrip("."),
            "This is a count of days for one year, not a climate normal.",
        ]
    return items


def _uncertainties(resolved: ResolvedIntent, reports: list[HubReport]) -> list[str]:
    if resolved.intent == "metric_lookup":
        items = [
            f"The count covers only {reports[0].window_label}, one year of reanalysis, not a climate normal or a station log."
        ]
    else:
        items = [
            f"Weather-day rates use only {reports[0].window_label}, a single year, not a climate normal.",
            "National Weather Service alerts are only the alerts active now, not a history of alerts in that year.",
            "FEMA baselines are a static county snapshot, not a live forecast.",
        ]
    if any(report.weather_fallback for report in reports):
        items.insert(0, "Live historical weather was unavailable, so these day counts are an offline stand-in, not verified observations.")
    for report in reports:
        for note in report.uncertainties:
            if _skip_note(resolved, report, note):
                continue
            items.append(note)
    return _unique(items)


def _source_items(reports: list[HubReport]) -> list[SourceItem]:
    return [
        SourceItem(
            name=source.name,
            source_type=source.source_type,  # type: ignore[arg-type]
            retrieved_at=source.retrieved_at,
            uncertainty_note=source.uncertainty_note,
        )
        for source in _merge_sources(reports)
    ]


def _merge_sources(reports: list[HubReport]) -> list[SourceRef]:
    best: dict[tuple[str, str], SourceRef] = {}
    for report in reports:
        for source in report.sources:
            key = (source.name, source.source_type)
            current = best.get(key)
            if current is None or source.retrieved_at > current.retrieved_at:
                best[key] = source
    return list(best.values())


def _ranking_row(report: HubReport, hazards: list[str]) -> dict:
    focus = hazards or ["overall"]
    drivers = []
    for hazard in focus:
        bucket = report.hazards.get(hazard if hazard != "overall" else "overall")
        if hazard == "overall":
            bucket = report.hazards["overall"]
        elif hazard in report.hazards:
            bucket = report.hazards[hazard]
        else:
            continue
        drivers.extend(bucket.drivers)
    drivers = sorted(drivers, key=lambda driver: driver.contribution, reverse=True)[:8]
    return {
        "hub_id": report.hub_id,
        "name": report.name,
        "region": report.region,
        "overall_score": _one_decimal(report.overall_score),
        "focus_score": _one_decimal(report.focus_score(hazards)),
        "hazard_scores": {
            hazard: _one_decimal(report.hazards[hazard].score)
            for hazard in ("winter", "flood", "hurricane", "heat", "severe_storm")
        },
        "drivers": [_public_driver(driver) for driver in drivers],
        "active_events": report.active_events,
    }


def _top_driver(report: HubReport, hazards: list[str]):
    pool = []
    chosen = hazards or ["overall"]
    for hazard in chosen:
        key = "overall" if hazard == "overall" or not hazards else hazard
        if key in report.hazards:
            pool.extend(report.hazards[key].drivers)
    if not pool:
        return None
    return max(pool, key=lambda driver: driver.contribution)


def _climate_gap(report: HubReport) -> str | None:
    snow = report.metrics.snow_days_pct
    baseline = next((driver for driver in report.hazards["winter"].drivers if driver.metric_name == "winter_baseline_score"), None)
    if baseline and baseline.value is not None and baseline.value >= 80 and snow is not None and snow < 8:
        return (
            f"The FEMA winter baseline for {report.name} is {baseline.value:.0f}, but only {snow:.1f}% of days had snowfall, "
            "so the winter score stays well below that baseline."
        )
    return None


def _focus_label(hazards: list[str]) -> str:
    if not hazards:
        return "overall disruption"
    return _join([HAZARD_LABEL[hazard] for hazard in hazards])


def _peer(report: HubReport):
    for hub in HUBS:
        if hub.region == report.region and hub.hub_id != report.hub_id:
            return hub
    return None


def _followups_for(resolved: ResolvedIntent, reports: list[HubReport]) -> list[str]:
    if not reports:
        return []
    top = reports[0]
    other = reports[1] if len(reports) > 1 else None
    peer = _peer(top)
    ideas: list[str] = []
    if resolved.intent != "explain_hub":
        ideas.append(f"Why is {_place(top)} risky?")
    if "flood" not in resolved.hazards:
        ideas.append("What about floods only?")
    elif "heat" not in resolved.hazards:
        ideas.append("What about heat only?")
    if other is not None and resolved.intent != "compare_hubs":
        ideas.append(f"Compare {_place(top)} and {_place(other)}.")
    elif other is not None and "heat" not in resolved.hazards:
        ideas.append(f"Compare {_place(top)} and {_place(other)} on heat.")
    elif peer is not None and resolved.intent != "compare_hubs":
        ideas.append(f"Compare {_place(top)} and {peer.name} hub ({peer.region} region).")
    if resolved.intent != "rank_hubs":
        ideas.append(f"Which hubs in the {top.region} region are most exposed to winter disruption?")
    return ideas


DRIVER_PLAIN = {
    "snow_days_pct": "the snow-day rate",
    "freeze_days_pct": "the freeze-day rate",
    "winter_baseline_score": "the FEMA winter baseline",
    "winter_alert_score": "winter alerts",
    "flood_baseline_score": "the FEMA flood baseline",
    "heavy_rain_days_pct": "the heavy-rain rate",
    "very_heavy_rain_days_pct": "the very-heavy-rain rate",
    "flood_alert_score": "flood alerts",
    "hurricane_baseline_score": "the FEMA hurricane baseline",
    "tropical_alert_score": "tropical alerts",
    "coastal_modifier": "the coastal surge flag",
    "heat_days_pct": "the 95 F day rate",
    "extreme_heat_days_pct": "the 100 F day rate",
    "heat_baseline_score": "the FEMA heat baseline",
    "heat_alert_score": "heat alerts",
    "severe_baseline_score": "the FEMA severe-storm baseline",
    "severe_alert_score": "severe-storm alerts",
    "high_wind_days_pct": "the high-wind rate",
    "winter_score": "winter",
    "flood_score": "flood",
    "hurricane_score": "hurricane",
    "heat_score": "heat",
    "severe_storm_score": "severe storm",
}


def _compose(resolved: ResolvedIntent, reports: list[HubReport], direct: str, why: str) -> str:
    scope = _scope_sentence(resolved, reports)
    assumption = "" if resolved.intent == "metric_lookup" else _assumption_sentence(resolved, reports)
    uncertainty = _uncertainty_sentence(resolved, reports)
    sources = _compact_sources(reports, inherited=resolved.inherited)
    notice = _window_notice(resolved)
    if resolved.detail == "sources":
        parts = [notice, sources, direct, uncertainty]
    elif resolved.detail == "assumptions":
        parts = [notice, assumption, uncertainty, direct]
    elif resolved.detail in {"methodology", "evidence"}:
        parts = [notice, direct, why, assumption, uncertainty]
    else:
        parts = [notice, direct, why, assumption, uncertainty, sources]
    return " ".join(part.strip() for part in parts if part and part.strip())


def _place(report: HubReport) -> str:
    return f"{report.name} hub ({report.region} region)"


def _mention(report: HubReport, seen: set[str]) -> str:
    if report.hub_id in seen:
        return report.name
    seen.add(report.hub_id)
    return _place(report)


WINDOW_NOTICE = "Only calendar year 2025 and trailing 365 days are supported, so this is for calendar year 2025."

_UNSUPPORTED_WINDOW_WORD = re.compile(
    r"\b(january|february|march|april|june|july|august|september|october|november|december|"
    r"jan|feb|mar|apr|jun|jul|aug|sep|sept|oct|nov|dec|q[1-4]|this month|last month|custom range)\b"
    r"|\b(?:in|during|for)\s+may\b",
    re.I,
)


def _asks_unsupported_window(resolved: ResolvedIntent) -> bool:
    """The notice follows only the message just sent, never a later follow-up."""
    return _unsupported_window((resolved.question or "").lower())


def _window_notice(resolved: ResolvedIntent) -> str:
    if not _asks_unsupported_window(resolved):
        return ""
    return WINDOW_NOTICE


def _source_line(resolved: ResolvedIntent, reports: list[HubReport]) -> str:
    label = reports[0].window_label.lower() if reports else ""
    trailing = resolved.time_window == "trailing_365" or "trailing 365" in label
    weather = "offline stand-in for Open-Meteo" if any(report.weather_fallback for report in reports) else "Open-Meteo"
    window = "trailing 365 days" if trailing else "2025 weather"
    if resolved.intent == "metric_lookup":
        return f"Sources: {weather} {window}."
    return f"Sources: {weather} {window}, FEMA NRI, NWS alerts."


def _finish_text(resolved: ResolvedIntent, reports: list[HubReport], sentences: list[str]) -> str:
    parts = [_window_notice(resolved), *sentences, _source_line(resolved, reports)]
    return " ".join(part.strip() for part in parts if part and part.strip())


def _full_breakdown(resolved: ResolvedIntent, reports: list[HubReport]) -> str:
    chunks = []
    if resolved.intent == "compare_hubs":
        hazards = resolved.hazards or ["overall"]
        for report in reports:
            for hazard in hazards:
                key = [] if hazard == "overall" else [hazard]
                total = report.focus_score(key)
                sentence = _contributor_sentence(report, key or ["overall"], total)
                if sentence:
                    chunks.append(f"{report.name} {HAZARD_LABEL[hazard]}: {sentence}")
        return " ".join(chunks)
    if resolved.explain_gap:
        chosen = reports
    elif resolved.intent == "rank_hubs":
        chosen = reports[:3]
    else:
        chosen = reports[:1]
    for report in chosen:
        if resolved.excluded_hazards:
            total, parts = _renormalized(report, resolved.excluded_hazards)
            chunks.append(f"{report.name}: {_renorm_sentence(resolved.excluded_hazards, total, parts)}")
            continue
        hazards = resolved.hazards or ["overall"]
        total = report.focus_score(resolved.hazards)
        sentence = _contributor_sentence(report, hazards, total)
        if sentence:
            chunks.append(f"{report.name}: {sentence}" if len(chosen) > 1 else sentence)
    return " ".join(chunks)


def _detail_hubs(resolved: ResolvedIntent, reports: list[HubReport]) -> list[dict]:
    hubs = []
    chosen = reports[:3] if resolved.intent == "rank_hubs" else reports
    for report in chosen:
        blocks = []
        if resolved.excluded_hazards:
            total, parts = _renormalized(report, resolved.excluded_hazards)
            shown = _allocate_tenths([part[2] for part in parts], total)
            blocks.append(
                {
                    "score": _one_decimal(total),
                    "contributors": [
                        {"name": HAZARD_LABEL[name], "points": points}
                        for (name, _score, _contribution), points in zip(parts, shown)
                    ],
                }
            )
        elif resolved.intent == "compare_hubs":
            for hazard in resolved.hazards or ["overall"]:
                key = [] if hazard == "overall" else [hazard]
                total = report.focus_score(key)
                labels, amounts = _display_amounts(report, key or ["overall"], total)
                shown = _allocate_tenths(amounts, total) if amounts else []
                blocks.append(
                    {
                        "hazard": hazard,
                        "score": _one_decimal(total),
                        "contributors": [{"name": label, "points": points} for label, points in zip(labels, shown)],
                    }
                )
        else:
            total = report.focus_score(resolved.hazards)
            labels, amounts = _display_amounts(report, resolved.hazards or ["overall"], total)
            shown = _allocate_tenths(amounts, total) if amounts else []
            blocks.append(
                {
                    "score": _one_decimal(total),
                    "contributors": [{"name": label, "points": points} for label, points in zip(labels, shown)],
                }
            )
        hubs.append({"hub_id": report.hub_id, "name": report.name, "score": blocks[0]["score"] if blocks else None, "contributors": blocks[0]["contributors"] if blocks else [], "blocks": blocks})
    return hubs


def _hub_count(evidence: dict | None, scores: list[ScoreItem]) -> int:
    ids = {item.hub_id for item in scores}
    for row in (evidence or {}).get("rankings") or []:
        if row.get("hub_id"):
            ids.add(row["hub_id"])
    return max(1, len(ids))


def _order_for(resolved: ResolvedIntent, reports: list[HubReport]) -> list[HubReport]:
    if resolved.excluded_hazards:
        return sorted(
            reports,
            key=lambda report: _renormalized(report, resolved.excluded_hazards)[0],
            reverse=not resolved.safety_framing,
        )
    return order_reports(reports, resolved.hazards, resolved.safety_framing)


def _rank_score(resolved: ResolvedIntent, report: HubReport) -> float:
    if resolved.excluded_hazards:
        return _renormalized(report, resolved.excluded_hazards)[0]
    return report.focus_score(resolved.hazards)


def _renormalized(report: HubReport, excluded: list[str]) -> tuple[float, list[tuple[str, float, float]]]:
    weights = {name: weight for name, weight in OVERALL_WEIGHTS.items() if name not in excluded}
    scale = sum(weights.values()) or 1.0
    parts = []
    total = 0.0
    for name, weight in weights.items():
        score = report.hazards[name].score
        contribution = score * weight / scale
        parts.append((name, score, contribution))
        total += contribution
    parts.sort(key=lambda item: item[2], reverse=True)
    return total, parts


def _renorm_sentence(excluded: list[str], total: float, parts: list[tuple[str, float, float]]) -> str:
    shown = _allocate_tenths([contribution for _name, _score, contribution in parts], total)
    bits = [
        f"{HAZARD_LABEL[name]} {_n(score)} → {points:.1f} of the {_n(total)} points"
        for (name, score, _contribution), points in zip(parts, shown)
    ]
    dropped = _join([HAZARD_LABEL[name] for name in excluded])
    return (
        f"{dropped.capitalize()} is excluded. Remaining weights were renormalized. "
        f"Contributors, largest first: {_join(bits)}. These points sum to {_n(total)}."
    )


def _rank_verdict(resolved: ResolvedIntent, leader: HubReport, seen: set[str]) -> str:
    score = _n(_rank_score(resolved, leader))
    name = _mention(leader, seen)
    if resolved.excluded_hazards:
        dropped = _join([HAZARD_LABEL[hazard] for hazard in resolved.excluded_hazards])
        word = "least" if resolved.safety_framing else "most"
        return f"{name} is the {word} exposed after removing {dropped}, at {score}."
    if resolved.safety_framing:
        return f"{name} has the lowest {_focus_label(resolved.hazards)} score, at {score}."
    if not resolved.hazards:
        return f"{name} has the highest overall disruption score, at {score}."
    return f"{name} is the most exposed to {_focus_label(resolved.hazards)}, at {score}."


def _compare_verdict(reports: list[HubReport], focus: list[str], seen: set[str], safer: bool) -> tuple[str, list[str]]:
    gaps = []
    for hazard in focus:
        key = [] if hazard == "overall" else [hazard]
        scored = sorted(reports, key=lambda report: report.focus_score(key), reverse=not safer)
        high, low = scored[0], scored[-1]
        gap = abs(high.focus_score(key) - low.focus_score(key))
        gaps.append((gap, hazard, high, low))
    gaps.sort(key=lambda item: item[0], reverse=True)
    gap, hazard, high, low = gaps[0]
    label = HAZARD_LABEL[hazard]
    high_score = high.focus_score([] if hazard == "overall" else [hazard])
    low_score = low.focus_score([] if hazard == "overall" else [hazard])
    if gap <= 1.0:
        verdict = f"{_mention(high, seen)} and {_mention(low, seen)} are practically tied on {label}: {_n(high_score)} vs {_n(low_score)}."
    elif safer:
        verdict = f"{_mention(low, seen)} is slightly less exposed than {_mention(high, seen)}, mainly on {label}: {_n(low_score)} vs {_n(high_score)}."
    elif gap <= 5:
        verdict = f"{_mention(high, seen)} is slightly more exposed than {_mention(low, seen)}, mainly on {label}: {_n(high_score)} vs {_n(low_score)}."
    else:
        verdict = f"{_mention(high, seen)} is more exposed than {_mention(low, seen)}, mainly on {label}: {_n(high_score)} vs {_n(low_score)}."
    extras = []
    for other_gap, other_hazard, other_high, other_low in gaps[1:]:
        if other_gap <= 1.0:
            extras.append(f"On {HAZARD_LABEL[other_hazard]}, they are practically tied.")
    return verdict, extras


def _baselines_once(reports: list[HubReport], hazards: list[str], seen: set[str]) -> str:
    identical = []
    for hazard in hazards:
        metric = BASELINE_METRIC.get(hazard)
        if metric is None or len(reports) < 2:
            continue
        left = _find_driver(reports[0], hazard, metric)
        right = _find_driver(reports[1], hazard, metric)
        if left is None or right is None or left.value is None or right.value is None:
            continue
        if abs(left.value - right.value) <= 1.0:
            identical.append(hazard)
    if not identical:
        return ""
    return f"FEMA's historical {_join([HAZARD_LABEL[hazard] for hazard in identical])} risk does not separate them."


def _short_separator(reports: list[HubReport], hazards: list[str]) -> str:
    if len(reports) < 2:
        return ""
    best = None
    for hazard in hazards:
        for name in OBSERVED_METRICS.get(hazard, ()):
            left = _find_driver(reports[0], hazard, name)
            right = _find_driver(reports[1], hazard, name)
            if left is None or right is None:
                continue
            gap = abs((left.value or 0) - (right.value or 0))
            if best is None or gap > best[0]:
                best = (gap, left)
    if best is None or best[0] == 0:
        return ""
    metric_name = best[1].metric_name
    if metric_name in _COUNT_FIELDS:
        _count_name, noun = _COUNT_FIELDS[metric_name]
        left = reports[0]
        right = reports[1]
        return (
            f"{left.name} had {int(getattr(left.metrics, _count_name) or 0)} {noun} and "
            f"{right.name} had {int(getattr(right.metrics, _count_name) or 0)}."
        )
    if str(metric_name).endswith("_alert_score"):
        return "Active alerts separate them."
    if metric_name == "coastal_modifier":
        return "The coastal location separates them."
    return ""


def _best_separator(reports: list[HubReport], hazards: list[str], seen: set[str]) -> str:
    if len(reports) < 2:
        return ""
    best = None
    for hazard in hazards:
        for name in OBSERVED_METRICS.get(hazard, ()):
            left = _find_driver(reports[0], hazard, name)
            right = _find_driver(reports[1], hazard, name)
            if left is None or right is None:
                continue
            gap = abs((left.value or 0) - (right.value or 0))
            if best is None or gap > best[0]:
                best = (gap, left, right, reports[0], reports[1])
    if best is None or best[0] == 0:
        return ""
    _gap, left, right, left_hub, right_hub = best
    return (
        f"What separates them is {_short_driver(left)}: {_mention(left_hub, seen)} {_driver_value(left)} "
        f"versus {_mention(right_hub, seen)} {_driver_value(right)}."
    )


def _alert_implications(reports: list[HubReport]) -> str:
    bits = []
    for report in reports:
        for event in report.active_events[:1]:
            match = re.match(r"(.+) \(([^)]+)\)$", event)
            if match:
                shown = f"a {match.group(2).lower()} {match.group(1)}"
            else:
                shown = event
            bits.append(f"{report.name} has {shown} active now — short-term, not reflected in the annual score.")
    return " ".join(bits[:2])


def _alerts_answer(reports: list[HubReport]) -> AnswerResult:
    active = [report for report in reports if report.active_events]
    quiet = [report for report in reports if not report.active_events]
    if not active:
        text = "No active NWS alerts."
    else:
        lines = [_alert_implications(active)]
        if quiet:
            lines.append("No active alerts for the other hubs.")
        text = " ".join(part for part in lines if part)
    text += " Uncertainty: NWS shows only alerts active now, not a history for the year. Sources: NWS active alerts."
    return AnswerResult(
        answer=text,
        key_findings=[f"{report.name}: {', '.join(report.active_events) or 'none'}" for report in reports[:6]],
        assumptions=["Active alerts are a current snapshot and are not part of the annual score."],
        uncertainties=["National Weather Service alerts are only the alerts active now."],
        sources=_source_items(reports),
        follow_up_questions=[
            "Want the full score breakdown?",
            "Which hub is most exposed to flooding?",
            "Compare Miami and Houston on hurricane and flood.",
        ],
    )


def canned_answer(resolved: ResolvedIntent) -> AnswerResult:
    examples = " ".join(f"'{item}'" for item in EXAMPLES)
    if resolved.intent == "unsupported_language":
        return AnswerResult(answer="I currently support English only. Try: 'Which hub is most exposed to flooding?'")
    if resolved.intent == "greeting":
        return AnswerResult(
            answer=f"I compare weather-disruption risk for 10 US logistics hubs. Try: {examples}",
            follow_up_questions=list(EXAMPLES),
        )
    if resolved.intent == "off_topic":
        return AnswerResult(
            answer=f"I only cover weather-disruption risk for the 10 hubs. Try: {examples}",
            follow_up_questions=list(EXAMPLES),
        )
    text = (
        "Scores use calendar year 2025 unless you ask for a trailing 365 days. "
        "Fixed overall weights are winter 0.30, flood 0.25, hurricane 0.20, severe storm 0.15, and heat 0.10. "
        "A missing input is dropped and the remaining weights are renormalized. A snow day is daily snowfall >= 1 cm. "
        "The score is a 0-100 index, not a probability. "
        "Weather and FEMA inputs were retrieved 2026-11-23. "
        "Uncertainty: weather days are a single year, NWS shows only alerts active now, and FEMA is a static county snapshot. "
        "Sources: Open-Meteo archive, NWS active alerts, and the FEMA National Risk Index snapshot."
    )
    return AnswerResult(
        answer=text,
        assumptions=[
            "The demo answers in English only.",
            "Supported windows are calendar year 2025 and a trailing 365 days.",
            "Overall weights are fixed: winter 0.30, flood 0.25, hurricane 0.20, severe storm 0.15, heat 0.10.",
        ],
        uncertainties=["Weather days are a single year, NWS alerts are only those active now, and FEMA is a static snapshot."],
        sources=[
            SourceItem(
                name="Open-Meteo archive, NWS active alerts, and the FEMA National Risk Index snapshot",
                source_type="static_public_dataset",
                retrieved_at="2026-11-23",
                uncertainty_note="Methodology summary. Live answers still cite the source used for that hub.",
            )
        ],
        follow_up_questions=list(EXAMPLES),
    )


def _metric_noun(metric: str, fallback: str) -> str:
    names = {
        "snowfall_day_pct": "snow days",
        "freeze_day_pct": "freeze days",
        "heat_day_pct": "95 F days",
        "extreme_heat_day_pct": "extreme heat days",
        "heavy_rain_day_pct": "heavy-rain days",
        "very_heavy_rain_day_pct": "very-heavy-rain days",
    }
    return names.get(metric, fallback.lower())


def _metric_threshold(metric: str) -> str:
    text = {
        "snowfall_day_pct": "A snow day is daily snowfall >= 1 cm.",
        "freeze_day_pct": "A freeze day has a minimum of 32 F or lower.",
        "heat_day_pct": "A heat day has a maximum of at least 95 F.",
        "extreme_heat_day_pct": "An extreme heat day has a maximum of at least 100 F.",
        "heavy_rain_day_pct": "A heavy-rain day has at least 1 inch of precipitation.",
        "very_heavy_rain_day_pct": "A very heavy-rain day has at least 2 inches of precipitation.",
    }
    return text.get(metric, "The count uses the stated daily threshold.")


HURRICANE_DRIVERS = {"hurricane_score", "hurricane_baseline_score", "tropical_alert_score", "coastal_modifier"}


def _display_amounts(report: HubReport, hazards: list[str], total: float) -> tuple[list[str], list[float]]:
    labels = []
    amounts = []
    hidden = 0.0
    for driver in _ranked_drivers(report, hazards):
        if driver.metric_name in HURRICANE_DRIVERS and not report.hurricane_published and "hurricane" not in hazards:
            hidden += driver.contribution
            continue
        labels.append(f"{_short_driver(driver)} {_driver_value(driver)}")
        amounts.append(driver.contribution)
    if amounts and hidden:
        amounts[0] += hidden
    if not amounts and hidden:
        labels = ["remaining scored inputs"]
        amounts = [total]
    return labels, amounts


def _allocate_tenths(amounts: list[float], total: float) -> list[float]:
    if not amounts:
        return []
    target = int(round(round(float(total), 1) * 10))
    scaled = [float(amount) * 10 for amount in amounts]
    floors = [int(math.floor(value + 1e-9)) for value in scaled]
    leftover = target - sum(floors)
    by_fraction = sorted(range(len(scaled)), key=lambda index: (scaled[index] - floors[index], amounts[index]), reverse=True)
    if leftover > 0:
        for step in range(leftover):
            floors[by_fraction[step % len(by_fraction)]] += 1
    elif leftover < 0:
        low_first = list(reversed(by_fraction))
        for step in range(-leftover):
            floors[low_first[step % len(low_first)]] -= 1
    return [value / 10 for value in floors]


def _contributor_sentence(report: HubReport, hazards: list[str], total: float) -> str:
    labels, amounts = _display_amounts(report, hazards, total)
    if not labels:
        return ""
    shown = _allocate_tenths(amounts, total)
    parts = [f"{label} → {points:.1f} of the {_n(total)} points" for label, points in zip(labels, shown)]
    return f"Contributors, largest first: {_join(parts)}. These points sum to {_n(total)}."


def _short_driver(driver) -> str:
    return DRIVER_PLAIN.get(driver.metric_name, driver.metric_name.replace("_", " ")).removeprefix("the ")


BASELINE_METRIC = {
    "winter": "winter_baseline_score",
    "flood": "flood_baseline_score",
    "hurricane": "hurricane_baseline_score",
    "heat": "heat_baseline_score",
    "severe_storm": "severe_baseline_score",
}

OBSERVED_METRICS = {
    "flood": ("heavy_rain_days_pct", "very_heavy_rain_days_pct", "flood_alert_score"),
    "hurricane": ("tropical_alert_score", "coastal_modifier"),
    "winter": ("snow_days_pct", "freeze_days_pct", "winter_alert_score"),
    "heat": ("heat_days_pct", "extreme_heat_days_pct", "heat_alert_score"),
    "severe_storm": ("high_wind_days_pct", "severe_alert_score"),
}


def _find_driver(report: HubReport, hazard: str, name: str):
    hazards = ["overall"] if hazard == "overall" else [hazard]
    for driver in _ranked_drivers(report, hazards):
        if driver.metric_name == name:
            return driver
    return None


def _driver_value(driver) -> str:
    if driver.value is None:
        return "n/a"
    if str(driver.metric_name).endswith("_pct"):
        return f"{_n(driver.value)}%"
    return _n(driver.value)


def _baseline_sentence(left: HubReport, right: HubReport, hazard: str) -> str:
    metric = BASELINE_METRIC.get(hazard)
    if metric is None:
        return ""
    own = _find_driver(left, hazard, metric)
    other = _find_driver(right, hazard, metric)
    if own is None or other is None or own.value is None or other.value is None:
        return ""
    label = HAZARD_LABEL[hazard]
    if abs(own.value - other.value) <= 1.0:
        return (
            f"The FEMA {label} baseline does not separate them: {_place(left)} is {_n(own.value)} "
            f"({_n(own.contribution)} points) and {_place(right)} is {_n(other.value)} ({_n(other.contribution)} points)."
        )
    return (
        f"{_place(left)} FEMA {label} baseline is {_n(own.value)} ({_n(own.contribution)} points) and "
        f"{_place(right)} FEMA {label} baseline is {_n(other.value)} ({_n(other.contribution)} points)."
    )


def _separator_sentence(left: HubReport, right: HubReport, hazard: str) -> str:
    best = None
    for name in OBSERVED_METRICS.get(hazard, ()):
        own = _find_driver(left, hazard, name)
        other = _find_driver(right, hazard, name)
        if own is None or other is None:
            continue
        gap = abs((own.value or 0) - (other.value or 0))
        if best is None or gap > best[0]:
            best = (gap, own, other)
    if best and best[0] > 0:
        _gap, own, other = best
        return (
            f"What separates them is {_short_driver(own)}: {_place(left)} {_driver_value(own)} "
            f"({_n(own.contribution)} points) versus {_place(right)} {_driver_value(other)} ({_n(other.contribution)} points)."
        )
    if left.active_events or right.active_events:
        left_alerts = ", ".join(left.active_events) or "none"
        right_alerts = ", ".join(right.active_events) or "none"
        return f"Active NWS alerts: {_place(left)} {left_alerts}; {_place(right)} {right_alerts}."
    return ""


def _hub_line(leader: HubReport, other: HubReport, hazards: list[str], seen: set[str], resolved: ResolvedIntent) -> str:
    score = _n(_rank_score(resolved, other))
    name = _mention(other, seen)
    lead_score = _n(_rank_score(resolved, leader))
    if score == lead_score:
        return f"{name} is effectively tied with {_mention(leader, seen)} at {score}."
    driver = _driver_words(other, hazards or ["overall"], resolved.excluded_hazards, limit=2)
    if resolved.safety_framing:
        lead = _mention(leader, seen)
        if driver:
            return f"{name} is riskier than {lead} at {score}, mainly on {driver}."
        return f"{name} is riskier than {lead} at {score}."
    if driver:
        return f"{name} is lower at {score}, mainly on {driver}."
    return f"{name} is lower at {score}."


def _reason_driver(leader: HubReport, other: HubReport, hazards: list[str], resolved: ResolvedIntent) -> str:
    if resolved.excluded_hazards:
        _leader_total, leader_parts = _renormalized(leader, resolved.excluded_hazards)
        _other_total, other_parts = _renormalized(other, resolved.excluded_hazards)
        leader_map = {name: contribution for name, _score, contribution in leader_parts}
        other_map = {name: contribution for name, _score, contribution in other_parts}
        best = None
        for name, left in leader_map.items():
            right = other_map.get(name, 0.0)
            gap = right - left if resolved.safety_framing else left - right
            if best is None or gap > best[0]:
                best = (gap, HAZARD_LABEL[name])
        if best and best[0] > 0.05:
            return best[1]
        return ""
    leader_drivers = {driver.metric_name: driver for driver in _ranked_drivers(leader, hazards or ["overall"])}
    other_drivers = {driver.metric_name: driver for driver in _ranked_drivers(other, hazards or ["overall"])}
    best_driver = None
    for metric_name, left in leader_drivers.items():
        if metric_name in HURRICANE_DRIVERS and not leader.hurricane_published and "hurricane" not in hazards:
            continue
        right = other_drivers.get(metric_name)
        if right is None:
            continue
        gap = right.contribution - left.contribution if resolved.safety_framing else left.contribution - right.contribution
        if best_driver is None or gap > best_driver[0]:
            best_driver = (gap, _short_driver(right if resolved.safety_framing else left))
    if best_driver and best_driver[0] > 0.05:
        return best_driver[1]
    return ""


def _driver_words(report: HubReport, hazards: list[str], excluded: list[str] | None = None, limit: int = 2) -> str:
    if excluded:
        _total, parts = _renormalized(report, excluded)
        names = [HAZARD_LABEL[name] for name, _score, _contribution in parts if name not in excluded]
        return _join(names[:limit])
    names = []
    for driver in _ranked_drivers(report, hazards):
        if driver.metric_name in HURRICANE_DRIVERS and not report.hurricane_published and "hurricane" not in hazards:
            continue
        names.append(_short_driver(driver))
        if len(names) >= limit:
            break
    return _join(names)


def _driver_point_phrase(
    report: HubReport,
    hazards: list[str],
    total: float,
    excluded: list[str] | None = None,
    limit: int = 2,
) -> str:
    labels, _scores, shown, total = _contribution_rows(report, hazards, total, excluded or [])
    bits = [f"{label} ({points:.1f})" for label, points in list(zip(labels, shown))[:limit]]
    if not bits:
        return ""
    return f"mainly {_join(bits)} of {_n(total)}"


def _contribution_rows(
    report: HubReport,
    hazards: list[str],
    total: float,
    excluded: list[str],
) -> tuple[list[str], list[float | None], list[float], float]:
    """Driver labels, the hazard score behind each one, displayed contribution points, and the total."""
    if excluded:
        total, parts = _renormalized(report, excluded)
        shown = _allocate_tenths([part[2] for part in parts], total)
        kept = [(name, score, points) for (name, score, _contribution), points in zip(parts, shown) if name not in excluded]
        return (
            [HAZARD_LABEL[name] for name, _score, _points in kept],
            [float(score) for _name, score, _points in kept],
            [points for _name, _score, points in kept],
            total,
        )
    drivers = []
    hidden = 0.0
    for driver in _ranked_drivers(report, hazards):
        if driver.metric_name in HURRICANE_DRIVERS and not report.hurricane_published and "hurricane" not in hazards:
            hidden += driver.contribution
            continue
        drivers.append(driver)
    if not drivers:
        return [], [], [], total
    amounts = [driver.contribution for driver in drivers]
    if hidden:
        amounts[0] += hidden
    shown = _allocate_tenths(amounts, total)
    return (
        [_short_driver(driver) for driver in drivers],
        [None if driver.value is None else float(driver.value) for driver in drivers],
        shown,
        total,
    )


def _subject_report(resolved: ResolvedIntent, ordered: list[HubReport]) -> HubReport | None:
    if not ordered:
        return None
    if resolved.explain_gap and len(resolved.hub_ids) > 1:
        for report in ordered:
            if report.hub_id == resolved.hub_ids[1]:
                return report
    return ordered[0]


def _required_driver_phrase(resolved: ResolvedIntent, report: HubReport) -> str:
    if resolved.intent != "explain_hub" or resolved.explain_gap:
        return ""
    if not re.search(r"\bwhy\b", resolved.question or "", re.I):
        return ""
    total = report.focus_score(resolved.hazards)
    return _driver_point_phrase(report, resolved.hazards or ["overall"], total, resolved.excluded_hazards)


def _driver_score_traps(report: HubReport, resolved: ResolvedIntent) -> list[dict]:
    total = report.focus_score(resolved.hazards) if resolved.intent == "explain_hub" else _rank_score(resolved, report)
    labels, scores, shown, _total = _contribution_rows(report, resolved.hazards or ["overall"], total, resolved.excluded_hazards)
    traps = []
    for label, score, points in zip(labels, scores, shown):
        if label not in {"winter", "flood", "hurricane", "heat", "severe storm"} or score is None:
            continue
        if abs(float(score) - points) <= 0.2:
            continue
        traps.append({"name": label, "score": round(float(score), 1), "points": points})
    return traps


def _gap_answer(resolved: ResolvedIntent, reports: list[HubReport]) -> AnswerResult:
    by_id = {report.hub_id: report for report in reports}
    if len(resolved.hub_ids) < 2 or resolved.hub_ids[0] not in by_id or resolved.hub_ids[1] not in by_id:
        return clarification_answer("I don't have both hubs from the previous ranking.")
    first = by_id[resolved.hub_ids[0]]
    second = by_id[resolved.hub_ids[1]]
    if _rank_score(resolved, second) > _rank_score(resolved, first):
        leader, other = second, first
    else:
        leader, other = first, second
    seen: set[str] = set()
    text = _finish_text(
        resolved,
        [leader, other],
        [_gap_sentence(leader, other, resolved.hazards, seen, resolved), _scores_line(resolved, [leader, other])],
    )
    return _pack(resolved, [leader, other], text, [], _followups_for(resolved, [leader, other]))


def _displayed_points(report: HubReport, hazards: list[str], excluded: list[str], label: str) -> float | None:
    """The same tenth shown in the breakdown for this driver, not the unrounded contribution."""
    focus = hazards or ["overall"]
    total = _rank_score_for(report, hazards, excluded)
    labels, _scores, shown, _total = _contribution_rows(report, focus, total, excluded)
    for name, points in zip(labels, shown):
        if name == label:
            return points
    return None


def _rank_score_for(report: HubReport, hazards: list[str], excluded: list[str]) -> float:
    if excluded:
        return _renormalized(report, excluded)[0]
    return report.focus_score(hazards)


_GAP_SPEAK = {
    "winter": ("winter", "Winter", None),
    "flood": ("flood", "Flood", None),
    "hurricane": ("hurricane", "Hurricane", None),
    "heat": ("heat", "Heat", None),
    "severe storm": ("severe storms", "Severe storm", None),
    "freeze-day rate": ("freezing days", "Freeze", "freeze_days_pct"),
    "snow-day rate": ("snow days", "Snow", "snow_days_pct"),
    "heavy-rain rate": ("heavy rain", "Heavy rain", "heavy_rain_days_pct"),
    "very-heavy-rain rate": ("very heavy rain", "Very heavy rain", "very_heavy_rain_days_pct"),
    "high-wind rate": ("high wind", "High wind", "high_wind_days_pct"),
    "95 f day rate": ("days reaching 95 F", "Heat days", "heat_days_pct"),
    "100 f day rate": ("extreme heat days", "Extreme heat", "extreme_heat_days_pct"),
}


def _gap_sentence(leader: HubReport, other: HubReport, hazards: list[str], seen: set[str], resolved: ResolvedIntent) -> str:
    """The gap is the reason. Scores stay in the later Scores line."""
    name = other.name
    lead = leader.name
    if _n(_rank_score(resolved, other)) == _n(_rank_score(resolved, leader)):
        return f"{name} is effectively tied with {lead}."
    gap = _main_gap(leader, other, hazards, resolved)
    if gap is None:
        return f"{name} trails {lead}."
    label, left_points, right_points = gap
    left_shown = _displayed_points(leader, hazards, resolved.excluded_hazards, label)
    right_shown = _displayed_points(other, hazards, resolved.excluded_hazards, label)
    if left_shown is not None:
        left_points = left_shown
    if right_shown is not None:
        right_points = right_shown
    because, noun, metric = _GAP_SPEAK.get(label, (label, label[:1].upper() + label[1:] if label else label, None))
    sentences = [
        f"{name} trails {lead} mainly because of {because}.",
        f"{noun} contributes {right_points:.1f} vs {left_points:.1f} points.",
    ]
    if metric:
        counted = _two_count_sentence(leader, other, metric)
        if counted:
            sentences.append(counted)
    return " ".join(sentences)


def _two_count_sentence(leader: HubReport, other: HubReport, metric_name: str) -> str:
    spec = _COUNT_FIELDS.get(metric_name)
    if spec is None:
        return ""
    count_name, noun = spec
    year = "2025" if str(leader.window_start).startswith("2025") or "2025" in leader.window_label else leader.window_label
    left = int(getattr(leader.metrics, count_name) or 0)
    right = int(getattr(other.metrics, count_name) or 0)
    return f"{leader.name} had {left} {noun} in {year} and {other.name} had {right}."


def _main_gap(leader: HubReport, other: HubReport, hazards: list[str], resolved: ResolvedIntent) -> tuple[str, float, float] | None:
    if resolved.excluded_hazards:
        _leader_total, leader_parts = _renormalized(leader, resolved.excluded_hazards)
        _other_total, other_parts = _renormalized(other, resolved.excluded_hazards)
        leader_map = {name: contribution for name, _score, contribution in leader_parts}
        other_map = {name: contribution for name, _score, contribution in other_parts}
        best = None
        for hazard_name, left in leader_map.items():
            if hazard_name in resolved.excluded_hazards:
                continue
            right = other_map.get(hazard_name, 0.0)
            gap = left - right
            if best is None or gap > best[0]:
                best = (gap, HAZARD_LABEL[hazard_name], left, right)
        if best and best[0] > 0.05:
            return best[1], best[2], best[3]
        return None
    leader_drivers = {driver.metric_name: driver for driver in _ranked_drivers(leader, hazards or ["overall"])}
    other_drivers = {driver.metric_name: driver for driver in _ranked_drivers(other, hazards or ["overall"])}
    best_driver = None
    for metric_name, left in leader_drivers.items():
        if metric_name in HURRICANE_DRIVERS and not leader.hurricane_published and "hurricane" not in hazards:
            continue
        hazard_name = metric_name.removesuffix("_score")
        if hazard_name in resolved.excluded_hazards:
            continue
        right = other_drivers.get(metric_name)
        if right is None:
            continue
        gap = left.contribution - right.contribution
        if best_driver is None or gap > best_driver[0]:
            best_driver = (gap, _short_driver(left), left.contribution, right.contribution)
    if best_driver and best_driver[0] > 0.05:
        return best_driver[1], best_driver[2], best_driver[3]
    return None


def _public_driver(driver) -> dict:
    raw = driver.public()
    if raw["value"] is not None:
        raw["value"] = _one_decimal(raw["value"])
    raw["contribution"] = _one_decimal(raw["contribution"])
    return raw


def _skip_note(resolved: ResolvedIntent, report: HubReport, note: str) -> bool:
    low = note.lower()
    asked = (resolved.question or "").lower()
    asked_hurricane = "hurricane" in resolved.hazards or "hurricane" in asked
    if "hurricane" in low and not asked_hurricane and not report.hurricane_published:
        return True
    if ("coastal-flood" in low or "coastal flood" in low) and "coastal" not in asked and not asked_hurricane:
        return True
    return False


def _repeats_question(suggestion: str, asked: str) -> bool:
    if not asked:
        return False

    def norm(text: str) -> str:
        return " ".join(re.sub(r"[^a-z0-9 ]", " ", text.lower()).split())

    left = norm(suggestion)
    right = norm(asked)
    if not left or not right:
        return False
    if left in right or right in left:
        return True
    stop = {"the", "a", "an", "of", "to", "in", "on", "for", "and", "hub", "region", "is", "are"}
    left_tokens = {token for token in left.split() if token not in stop}
    right_tokens = {token for token in right.split() if token not in stop}
    return bool(left_tokens) and left_tokens <= right_tokens


def _mentions_unrelated_hub(suggestion: str, reports: list[HubReport]) -> bool:
    allowed = {report.name.lower() for report in reports}
    allowed.update(report.hub_id.lower() for report in reports)
    for report in reports:
        for hub in HUBS:
            if hub.region == report.region:
                allowed.add(hub.name.lower())
                allowed.add(hub.hub_id.lower())
    low = suggestion.lower()
    for hub in HUBS:
        if hub.name.lower() in low and hub.name.lower() not in allowed:
            return True
    return False


def _filter_followups(resolved: ResolvedIntent, reports: list[HubReport], questions: list[str]) -> list[str]:
    names = [report.name.lower() for report in reports]
    kept = []
    seen = set()
    for question in questions:
        low = question.lower()
        if low in seen:
            continue
        if _repeats_question(question, resolved.question) or _repeats_question(question, resolved.prior_question):
            continue
        if _mentions_unrelated_hub(question, reports):
            continue
        if resolved.intent == "explain_hub" and names and f"why is {names[0]}" in low:
            continue
        if resolved.intent == "metric_lookup" and resolved.metric == "snowfall_day_pct" and "snow" in low:
            continue
        if resolved.intent == "rank_hubs" and "most exposed" in low:
            continue
        if resolved.intent == "compare_hubs" and low.startswith("compare") and " on " not in low:
            if names and all(name.split()[0] in low for name in names[:2]):
                continue
        seen.add(low)
        kept.append(question)
    return kept


def _why_sentence(report: HubReport, hazards: list[str], subject: str = "The score", limit: int = 3) -> str:
    drivers = _ranked_drivers(report, hazards)[:limit]
    if not drivers:
        return ""
    return f"{subject} is led by {_join([_driver_phrase(driver) for driver in drivers])}."


def _investment_sentence(report: HubReport) -> str:
    """One or two suggestions from the largest overall drivers. No costs and no new numbers."""
    hazards: list[str] = []
    for driver in _ranked_drivers(report, ["overall"]):
        name = str(driver.metric_name).removesuffix("_score")
        if name in HAZARD_LABEL and name != "overall" and name not in hazards:
            hazards.append(name)
        if len(hazards) == 3:
            break
    phrases: list[str] = []
    if "hurricane" in hazards and "flood" in hazards:
        phrases.append("hurricane and flood protection")
        hazards = [name for name in hazards if name not in {"hurricane", "flood"}]
    elif "hurricane" in hazards:
        phrases.append("hurricane protection")
        hazards = [name for name in hazards if name != "hurricane"]
    elif "flood" in hazards:
        phrases.append("flood protection")
        hazards = [name for name in hazards if name != "flood"]
    readiness = {"winter": "winter readiness", "heat": "heat readiness", "severe_storm": "severe-storm readiness"}
    for name in hazards:
        if len(phrases) >= 2:
            break
        phrases.append(readiness.get(name, f"{HAZARD_LABEL[name]} readiness"))
    if not phrases:
        return ""
    return f"Based on those risk drivers, I'd prioritize {_join(phrases[:2])}."


def _ranked_drivers(report: HubReport, hazards: list[str]):
    pool = []
    chosen = hazards or ["overall"]
    for hazard in chosen:
        key = "overall" if hazard == "overall" else hazard
        if key in report.hazards:
            pool.extend(report.hazards[key].drivers)
    return sorted(pool, key=lambda driver: driver.contribution, reverse=True)


def _driver_phrase(driver) -> str:
    name = DRIVER_PLAIN.get(driver.metric_name, driver.metric_name.replace("_", " "))
    if driver.value is None:
        return f"{name} ({_n(driver.contribution)} points)"
    if str(driver.metric_name).endswith("_pct"):
        shown = f"{_n(driver.value)}%"
    else:
        shown = _n(driver.value)
    return f"{name} at {shown} ({_n(driver.contribution)} points)"


def _scope_sentence(resolved: ResolvedIntent, reports: list[HubReport]) -> str:
    window = reports[0].window_label
    names = _join([_place(report) for report in reports])
    if resolved.region and not resolved.hub_ids:
        place = f"{resolved.region} hubs in this set ({names})"
    else:
        place = names
    areas = list(dict.fromkeys(report.region for report in reports))
    if resolved.intent == "metric_lookup":
        related = METRIC_HAZARD.get(resolved.metric or "", "")
        topic = f"the requested day count and {HAZARD_LABEL.get(related, 'overall disruption')}"
    else:
        topic = _focus_label(resolved.hazards)
    return f"Scope: {place}; {_join(areas)}; {topic}; {window}."


def _assumption_sentence(resolved: ResolvedIntent, reports: list[HubReport]) -> str:
    focus = set(resolved.hazards)
    if resolved.intent == "metric_lookup" and resolved.metric:
        focus.add(METRIC_HAZARD.get(resolved.metric, ""))
        focus.discard("")
    definition = _definition(focus)
    if resolved.inherited and resolved.detail is None:
        return f"Same rules as the previous turn. {definition} The score is a 0-100 index, not a probability."
    return f"{definition} The score is a 0-100 index, not a probability."


def _definition(focus: set[str]) -> str:
    bits = []
    if "winter" in focus:
        bits.append("a snow day is daily snowfall >= 1 cm and a freeze day has a low of 32 F or below")
    if "flood" in focus:
        bits.append("heavy rain is at least 1 inch in a day, and the flood baseline uses the higher of the inland and coastal FEMA scores")
    if "hurricane" in focus:
        bits.append("the hurricane coastal modifier is 100 only for Miami, Houston, and New York/Newark")
    if "heat" in focus:
        bits.append("a heat day reaches at least 95 F and an extreme heat day reaches at least 100 F")
    if "severe_storm" in focus:
        bits.append("the severe-storm baseline averages the FEMA tornado, lightning, hail, and strong-wind scores")
    if not bits:
        return "Weather-day rates use the stated window."
    return " ".join(bit[0].upper() + bit[1:] + "." for bit in bits)


def _uncertainty_sentence(resolved: ResolvedIntent, reports: list[HubReport]) -> str:
    window = reports[0].window_label if reports else "the selected year"
    if resolved.intent == "metric_lookup":
        text = f"Uncertainty: this count is only {window}, one year of reanalysis, not a climate normal or a station log."
    else:
        text = (
            f"Uncertainty: weather days are a single year ({window}), NWS shows only alerts active now, "
            "and FEMA is a static county snapshot rather than a live forecast."
        )
    if any(report.weather_fallback for report in reports):
        return "Uncertainty: live historical weather was unavailable, so these day counts are an offline stand-in for one year, not a climate normal."
    return text


def _compact_sources(reports: list[HubReport], inherited: bool = False) -> str:
    weather = "an offline climatology stand-in" if any(report.weather_fallback for report in reports) else "Open-Meteo archive"
    lead = "Sources, same as the previous turn" if inherited else "Sources"
    return f"{lead}: {weather}, NWS active alerts, and the FEMA National Risk Index snapshot."


_INTERNAL_LABELS = (
    "freeze-day rate",
    "snow-day rate",
    "high-wind rate",
    "heavy-rain rate",
    "very-heavy-rain rate",
    "95 f day rate",
    "100 f day rate",
    "fema winter baseline",
    "fema flood baseline",
    "fema hurricane baseline",
    "fema heat baseline",
    "fema severe-storm baseline",
    "severe-storm baseline",
    "coastal surge flag",
)


def _internal_label_problem(text: str, skeleton: str, evidence: dict | None) -> str | None:
    """Default answers stay in plain language. A requested breakdown may use the technical names."""
    if (evidence or {}).get("lock_breakdown"):
        return None
    spoken = text.split("Contributors, largest first", 1)[0].lower()
    allowed = (skeleton or "").lower()
    for label in _INTERNAL_LABELS:
        if label in spoken and label not in allowed:
            return f"internal label {label}"
    return None


_OVERCLAIMS = (
    "significant portion",
    "significant share",
    "large portion of the year",
    "more significantly",
    "contributes more significantly",
)


def _answer_shape_problem(text: str, skeleton: str, evidence: dict | None) -> str | None:
    """Reject a changed claim. Sentence order, the legend, and source wording are not claims."""
    del evidence
    if not (skeleton or "").strip():
        return None
    spoken = text.split("Contributors, largest first", 1)[0]
    allowed = skeleton.lower()
    low = spoken.lower()
    for phrase in _OVERCLAIMS:
        if phrase in low and phrase not in allowed:
            return f"overclaim {phrase}"
    if "most exposed" in low and "most exposed" not in allowed:
        return "calls a hub most exposed"
    if _scores_clause(skeleton) is None and _scores_clause(spoken) is not None:
        clause = _scores_clause(spoken) or ""
        if not re.search(r"\d", clause):
            return "empty scores line"
    return None


def _scores_clause(text: str) -> str | None:
    match = re.search(r"Scores:(.*?)(?=\s+Sources:|\s+Contributors,|$)", text, re.S)
    if match is None:
        return None
    return match.group(1).strip()


def _before_scores(text: str) -> str:
    return text.split("Scores:", 1)[0]


def _score_tokens(clause: str) -> list[tuple[str, str]]:
    body = re.sub(r"\(higher = more exposed\)", "", clause, flags=re.I).strip().rstrip(".")
    tokens = []
    for piece in body.split(","):
        match = re.search(r"(.+?)\s+(\d+(?:\.\d+)?)\s*$", piece.strip())
        if match:
            tokens.append((" ".join(match.group(1).lower().split()), match.group(2)))
    return tokens


def _numbers_in(text: str) -> set[float]:
    return {float(raw) for raw in re.findall(r"\d+(?:\.\d+)?", text or "")}


def _ranking_score_numbers(evidence: dict | None) -> set[float]:
    found: set[float] = set()
    for row in (evidence or {}).get("rankings") or []:
        for key in ("focus_score", "overall_score"):
            if row.get(key) is not None:
                found.add(round(float(row[key]), 1))
    return found


def _prepare_draft(text: str, skeleton: str) -> str:
    """Keep the skeleton's Scores and Sources lines. Do not cut numbers out of the reason."""
    cleaned = (text or "").strip()
    if "Scores:" not in skeleton and re.search(r"(?i)\bScores:", cleaned):
        cleaned = re.sub(r"(?i)\s*Scores:.*?(?=(?:\s+Sources:)|\s*$)", "", cleaned, flags=re.S).strip()
    if "Scores:" in skeleton and not re.search(r"(?i)\bScores:", cleaned):
        scores = re.search(r"Scores:.*?(?=\s+Sources:|$)", skeleton, re.S)
        if scores:
            cleaned = f"{cleaned.rstrip()} {scores.group(0).strip()}".strip()
    if not re.search(r"(?i)\bSources\s*:", cleaned):
        match = re.search(r"Sources:.*", skeleton)
        if match:
            cleaned = f"{cleaned.rstrip()} {match.group(0).strip()}".strip()
    cleaned = _drop_legend_from_reason(cleaned, skeleton)
    return _own_investment_sentence(cleaned, skeleton)


_NUMBER_ONES = {
    "zero": 0,
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
    "thirteen": 13,
    "fourteen": 14,
    "fifteen": 15,
    "sixteen": 16,
    "seventeen": 17,
    "eighteen": 18,
    "nineteen": 19,
}
_NUMBER_TENS = {
    "twenty": 20,
    "thirty": 30,
    "forty": 40,
    "fifty": 50,
    "sixty": 60,
    "seventy": 70,
    "eighty": 80,
    "ninety": 90,
}


def _drop_legend_from_reason(text: str, skeleton: str) -> str:
    """The legend belongs on the Scores line only, and only on the first one."""
    parts = re.split(r"(?i)(?=\bScores\s*:)", text, maxsplit=1)
    reason = re.sub(r"\s*\(\s*higher\s*=\s*more exposed\s*\)", "", parts[0], flags=re.I)
    reason = re.sub(r"\s+([.,;])", r"\1", reason)
    reason = re.sub(r"[ \t]{2,}", " ", reason)
    if len(parts) == 1:
        return reason.strip()
    scores = parts[1]
    skeleton_scores = re.split(r"(?i)(?=\bScores\s*:)", skeleton, maxsplit=1)
    legend_on_scores = len(skeleton_scores) > 1 and re.search(r"higher\s*=\s*more exposed", skeleton_scores[1], re.I)
    if not legend_on_scores:
        scores = re.sub(r"\s*\(\s*higher\s*=\s*more exposed\s*\)", "", scores, flags=re.I)
    return f"{reason.rstrip()} {scores.lstrip()}".strip()


def _drop_priority_clause(body: str) -> str:
    """Remove an investment sentence the user did not ask for."""
    body = re.sub(
        r"(?i)(?:^|(?<=[.!?]\s))\s*(?:Based on those risk drivers,\s*)?I'?d prioritize\b[^.?!]*[.?!]?",
        "",
        body,
    )
    body = re.sub(
        r"(?i),?\s*(?:and\s+)?(?:based on those risk drivers,\s*)?I'?d prioritize\b[^.?!]*",
        "",
        body,
    )
    body = re.sub(r"\s{2,}", " ", body).strip().rstrip(",;")
    if body and body[-1] not in ".!?":
        body += "."
    return body


def _own_investment_sentence(text: str, skeleton: str) -> str:
    """Keep the recommendation only when this question asked where to invest or what to strengthen."""
    match = re.search(r"Based on those risk drivers, I'd prioritize [^.]+\.", skeleton)
    tail_match = re.search(r"(?i)\b(?:Scores|Sources)\s*:.*", text, flags=re.S)
    body = text[: tail_match.start()] if tail_match else text
    tail = tail_match.group(0).strip() if tail_match else ""
    if not match:
        body = _drop_priority_clause(body)
        return f"{body} {tail}".strip()
    sentence = match.group(0)
    tail_match = re.search(r"(?i)\b(?:Scores|Sources)\s*:.*", text, flags=re.S)
    body = text[: tail_match.start()] if tail_match else text
    tail = tail_match.group(0).strip() if tail_match else ""
    body = _drop_priority_clause(body)
    if sentence not in body:
        body = f"{body} {sentence}".strip()
    return f"{body} {tail}".strip()


def _spell_numbers(text: str) -> str:
    """'seventeen' and 'four point seven' count as the same facts as 17 and 4.7."""
    ones = "|".join(sorted(_NUMBER_ONES, key=len, reverse=True))
    tens = "|".join(sorted(_NUMBER_TENS, key=len, reverse=True))

    def tens_ones(match: re.Match[str]) -> str:
        return str(_NUMBER_TENS[match.group(1).lower()] + _NUMBER_ONES[match.group(2).lower()])

    spoken = re.sub(rf"\b({tens})[-\s]({ones})\b", tens_ones, text or "", flags=re.I)

    def point(match: re.Match[str]) -> str:
        left = match.group(1).lower()
        right = match.group(2).lower()
        if left in _NUMBER_ONES:
            whole = _NUMBER_ONES[left]
        elif left in _NUMBER_TENS:
            whole = _NUMBER_TENS[left]
        else:
            whole = int(left)
        if right in _NUMBER_ONES:
            return f"{whole}.{_NUMBER_ONES[right]}"
        if right in _NUMBER_TENS:
            return f"{whole}.{_NUMBER_TENS[right]:02d}"
        return f"{whole}.{int(right)}"

    spoken = re.sub(
        rf"\b(\d+|{tens}|{ones})(?:-|\s+)point(?:-|\s+)(\d+|{tens}|{ones})\b",
        point,
        spoken,
        flags=re.I,
    )

    def word(match: re.Match[str]) -> str:
        token = match.group(0).lower()
        return str(_NUMBER_ONES[token] if token in _NUMBER_ONES else _NUMBER_TENS[token])

    return re.sub(rf"\b(?:{tens}|{ones})\b", word, spoken, flags=re.I)


def _visible_numbers(text: str) -> str:
    """17 and seventeen are the same count. 4.7% and 4.7 percent are the same percent."""
    cleaned = _normalize_degree(text or "")
    cleaned = re.sub(r"[\u00ad\u200b\u200c\u200d\u2060\ufeff]", "", cleaned)
    cleaned = re.sub(r"%", " percent", cleaned)
    return _spell_numbers(cleaned)


def _fact_numbers(text: str) -> list[float]:
    found = []
    for raw in re.findall(r"\d+(?:\.\d+)?", _visible_numbers(text)):
        value = float(raw)
        if value in {0, 1, 2, 3, 4, 5, 10, 32, 40, 95, 100, 365} or 2020 <= value <= 2027:
            continue
        found.append(value)
    return found


def _unmatched_numbers(needed: list[float], got: list[float]) -> list[float]:
    pool = list(got)
    missing = []
    for value in needed:
        for index, other in enumerate(pool):
            if abs(value - other) <= 0.1 + 1e-9:
                pool.pop(index)
                break
        else:
            missing.append(value)
    return missing


def _missing_numbers(needed_text: str, got_text: str) -> bool:
    return bool(_unmatched_numbers(_fact_numbers(needed_text), _fact_numbers(got_text)))


def _name_forms(name: str) -> list[str]:
    short = name.split("/")[0].strip()
    return [name] if short == name else [name, short]


def _mentioned_hubs(text: str) -> list[str]:
    low = (text or "").lower()
    found = []
    for hub in sorted(HUBS, key=lambda item: len(item.name), reverse=True):
        if any(re.search(rf"\b{re.escape(form.lower())}\b", low) for form in _name_forms(hub.name)):
            found.append(hub.name)
    return found


def _says_trails(text: str, trailer: str, leader: str) -> bool:
    for left in _name_forms(trailer):
        for right in _name_forms(leader):
            if re.search(rf"\b{re.escape(left)} trails {re.escape(right)}\b", text):
                return True
    return False


def _fact_problem(text: str, skeleton: str) -> str | None:
    """Same hubs and the same counts, percents, scores, and contribution points. Wording may change."""
    spoken = text.split("Contributors, largest first", 1)[0]
    base = skeleton.split("Contributors, largest first", 1)[0]
    needed = _fact_numbers(base)
    got = _fact_numbers(spoken)
    missing = _unmatched_numbers(needed, got)
    if missing:
        return f"missing fact {missing[0]:g}"
    extra = [value for value in got if not any(abs(value - item) <= 0.1 + 1e-9 for item in needed)]
    if extra:
        return f"extra fact {extra[0]:g}"
    base_hubs = set(_mentioned_hubs(base))
    draft_hubs = set(_mentioned_hubs(spoken))
    if base_hubs - draft_hubs:
        return f"missing hub {sorted(base_hubs - draft_hubs)[0]}"
    if draft_hubs - base_hubs:
        return f"extra hub {sorted(draft_hubs - base_hubs)[0]}"
    leaders = {leader for trailer, leader in ((left, right) for left in base_hubs for right in base_hubs if _says_trails(base, left, right))}
    for trailer in draft_hubs:
        for leader in draft_hubs:
            if trailer in leaders and _says_trails(spoken, trailer, leader):
                return f"leader trails: {trailer}"
    return None


def _draft_is_usable(text: str, evidence: dict | None, scores: list[ScoreItem], skeleton: str = "") -> bool:
    return _draft_rejection(text, evidence, scores, skeleton) is None


def _draft_rejection(text: str, evidence: dict | None, scores: list[ScoreItem], skeleton: str = "") -> str | None:
    notice = str((evidence or {}).get("required_notice") or "")
    if notice and notice not in text:
        return "missing required window notice"
    if not notice and WINDOW_NOTICE in text:
        return "window notice was not asked for"
    if (evidence or {}).get("unsupported_window") and _UNSUPPORTED_WINDOW_WORD.search(text):
        return "attributes yearly values to an unsupported window"
    if (skeleton or "").strip():
        return _fact_problem(text, skeleton)
    label_problem = _internal_label_problem(text, skeleton, evidence)
    if label_problem:
        return label_problem
    shape_problem = _answer_shape_problem(text, skeleton, evidence)
    if shape_problem:
        return shape_problem
    phrase = str((evidence or {}).get("required_driver_phrase") or "")
    phrase_in_skeleton = bool(phrase) and not _missing_numbers(phrase, skeleton or "")
    if phrase and (not (skeleton or "").strip() or phrase_in_skeleton) and _missing_numbers(phrase, text):
        return "why answer omitted the top contributors"
    if (skeleton or "").strip():
        fact_problem = _fact_problem(text, skeleton)
        if fact_problem:
            return fact_problem
    clause_problem = _mainly_clause_problem(text, evidence)
    if clause_problem:
        return clause_problem
    for trap in (evidence or {}).get("driver_score_traps") or []:
        name = re.escape(str(trap.get("name") or ""))
        if not name:
            continue
        for match in re.finditer(rf"\b{name}\b(?:\s+(?!\d)\w+){{0,4}}\s*\(?\s*(\d+(?:\.\d+)?)", text, re.I):
            value = float(match.group(1))
            score = float(trap["score"])
            points = float(trap["points"])
            if abs(value - score) <= 0.1 + 1e-9 and abs(value - points) > 0.15:
                return f"driver cites the {trap['name']} hazard score"
    if evidence is None and not scores:
        return None
    allowed: set[float] = set()
    if evidence is not None:
        _collect_numbers(evidence, allowed)
    for item in scores:
        _collect_numbers(item.model_dump(), allowed)
    _collect_numbers(_normalize_degree(skeleton), allowed)
    text = _normalize_degree(text)
    if not allowed and evidence is None:
        return None
    for raw in re.findall(r"\d+(?:\.\d+)?", text):
        value = float(raw)
        if value in {0, 1, 2, 3, 4, 5, 10, 32, 40, 95, 100, 365} or 2020 <= value <= 2027:
            continue
        if any(abs(value - known) <= 0.1 + 1e-9 for known in allowed):
            continue
        return f"number {raw} is not in the evidence"
    return None


_MAINLY_CLAUSE = re.compile(
    r"mainly\s+.+?\(([-+]?\d+(?:\.\d+)?)\)\s+and\s+.+?\(([-+]?\d+(?:\.\d+)?)\)\s+of\s+(\d+(?:\.\d+)?)",
    re.I,
)


def _mainly_clause_problem(text: str, evidence: dict | None) -> str | None:
    """Parentheses in 'mainly X (a) and Y (b) of TOTAL' are contribution points, and a + b stays within the total."""
    clauses = list(_MAINLY_CLAUSE.finditer(text))
    if not clauses:
        return None
    breakdown = str((evidence or {}).get("breakdown") or "")
    arrows = [float(number) for number in re.findall(r"→\s*(-?\d+(?:\.\d+)?)", breakdown)]
    hazard_scores: list[float] = []
    for row in (evidence or {}).get("rankings") or []:
        hazard_scores.extend(float(value) for value in (row.get("hazard_scores") or {}).values())
        for driver in row.get("drivers") or []:
            metric = str(driver.get("metric_name") or "")
            if metric.endswith("_score") and driver.get("value") is not None:
                hazard_scores.append(float(driver["value"]))
    for match in clauses:
        left = float(match.group(1))
        right = float(match.group(2))
        total = float(match.group(3))
        if left - total > 0.05 or right - total > 0.05 or left + right - total > 0.05:
            return "contributor points exceed the total"
        for value in (left, right):
            matches_points = any(abs(value - arrow) <= 0.05 + 1e-9 for arrow in arrows)
            matches_score = any(abs(value - score) <= 0.1 + 1e-9 for score in hazard_scores)
            if matches_score and not matches_points:
                return "mainly clause cites a hazard score"
            if arrows and not matches_points:
                return "mainly clause is not a contribution from the breakdown"
    return None


def _collect_numbers(value, found: set[float]) -> None:
    if isinstance(value, bool) or value is None:
        return
    if isinstance(value, (int, float)):
        found.add(round(float(value), 2))
        found.add(round(float(value), 1))
        found.add(float(round(value)))
        return
    if isinstance(value, str):
        for raw in re.findall(r"\d+(?:\.\d+)?", value):
            number = float(raw)
            found.add(round(number, 2))
            found.add(round(number, 1))
        return
    if isinstance(value, dict):
        for item in value.values():
            _collect_numbers(item, found)
        return
    if isinstance(value, list):
        for item in value:
            _collect_numbers(item, found)


def _source_sentence(sources: list[SourceRef]) -> str:
    if not sources:
        return "No sources were attached."
    bits = [f"{source.name} ({source.source_type}, retrieved {source.retrieved_at})" for source in sources]
    return "Sources: " + "; ".join(bits) + "."


def _name_list(reports: list[HubReport]) -> str:
    names = [report.name for report in reports]
    if len(names) <= 2:
        return _join(names)
    return ", ".join(names[:-1]) + " and " + names[-1]


def _join(items: list[str]) -> str:
    if not items:
        return ""
    if len(items) == 1:
        return items[0]
    if len(items) == 2:
        return f"{items[0]} and {items[1]}"
    return ", ".join(items[:-1]) + ", and " + items[-1]


def _n(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"{value:.1f}"


def _unique(items: list[str]) -> list[str]:
    seen = set()
    output = []
    for item in items:
        if not item or item in seen:
            continue
        seen.add(item)
        output.append(item)
    return output
