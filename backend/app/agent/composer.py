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
        details: dict = {"metric": metric}
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
        reason = _draft_rejection(drafted.answer, evidence, base.scores)
        if reason:
            drafted = None
            failure_note = reason
    if drafted is None and failure_note:
        logger.warning("answer skeleton shown: %s", failure_note)
    if drafted is None:
        if failure_note:
            uncertainties.insert(0, "The language model did not return a valid briefing, so this wording comes from the scored evidence.")
        else:
            assumptions.append("Wording is assembled directly from the scored evidence.")
        return _lock_breakdown(
            base.model_copy(update={"assumptions": _unique(assumptions), "uncertainties": _unique(uncertainties)}),
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


def _rank_answer(resolved: ResolvedIntent, reports: list[HubReport]) -> AnswerResult:
    focus = resolved.hazards
    label = _focus_label(focus)
    if not reports:
        return clarification_answer("None of the hubs in this set match that region.")
    seen: set[str] = set()
    leader = reports[0]
    direct = _rank_verdict(resolved, leader, seen)
    lines = []
    if resolved.safety_framing:
        others = reports[1:3]
        if others:
            bits = [f"{_mention(other, seen)} is riskier at {_n(_rank_score(resolved, other))}" for other in others]
            lines.append(_join(bits) + ".")
    else:
        phrase = _driver_point_phrase(leader, focus or ["overall"], _rank_score(resolved, leader), resolved.excluded_hazards)
        if phrase:
            lines.append(phrase[0].upper() + phrase[1:] + ".")
        lines.extend(_hub_line(leader, other, focus, seen, resolved) for other in reports[1:3])
    text = _finish_text(resolved, reports, [direct, " ".join(lines)])
    findings = [f"{report.name} {label} score: {_n(_rank_score(resolved, report))}" for report in reports[:5]]
    driver = _top_driver(leader, focus)
    if driver:
        findings.append(f"Largest {label} driver for {leader.name}: {driver.metric_name} at {driver.value:.1f} (contribution {driver.contribution:.1f}).")
    return _pack(resolved, reports, text, findings, _followups_for(resolved, reports))


def _compare_answer(resolved: ResolvedIntent, reports: list[HubReport]) -> AnswerResult:
    focus = resolved.hazards or ["overall"]
    findings = []
    seen: set[str] = set()
    verdict, gaps = _compare_verdict(reports, focus, seen, resolved.safety_framing)
    for hazard in focus:
        for report in reports:
            label = HAZARD_LABEL[hazard]
            findings.append(f"{report.name} {label}: {_n(report.focus_score([hazard] if hazard != 'overall' else []))}")
    hazard_focus = [hazard for hazard in focus if hazard != "overall"]
    covered = f"Compared on {_join([HAZARD_LABEL[hazard] for hazard in focus])}." if len(focus) > 1 else ""
    reason = " ".join(
        part
        for part in (
            covered,
            _baselines_once(reports, hazard_focus, seen),
            _short_separator(reports, hazard_focus),
            " ".join(gaps),
        )
        if part
    )
    text = _finish_text(resolved, reports, [verdict, reason])
    return _pack(resolved, reports, text, findings[:8], _followups_for(resolved, reports))


def _explain_answer(resolved: ResolvedIntent, reports: list[HubReport]) -> AnswerResult:
    report = reports[0]
    focus = resolved.hazards
    seen: set[str] = set()
    if focus:
        score = report.focus_score(focus)
        label = _focus_label(focus)
        direct = f"The {label} score for {_mention(report, seen)} is {_n(score)}"
    else:
        score = report.overall_score
        label = "overall disruption"
        direct = f"The overall weather-disruption score for {_mention(report, seen)} is {_n(score)}"
    phrase = _driver_point_phrase(report, focus or ["overall"], score, resolved.excluded_hazards)
    direct = f"{direct}, {phrase}." if phrase else f"{direct}."
    text = _finish_text(resolved, [report], [direct])
    findings = [f"{HAZARD_LABEL[hazard].capitalize()} score: {_n(report.hazards[hazard].score)}" for hazard in ("winter", "flood", "hurricane", "severe_storm", "heat")]
    findings.insert(0, f"Overall score: {_n(report.overall_score)}")
    return _pack(resolved, [report], text, findings, _followups_for(resolved, [report]))


def _metric_answer(resolved: ResolvedIntent, reports: list[HubReport]) -> AnswerResult:
    report = reports[0]
    payload = metric_payload(report, resolved.metric or "")
    if payload is None or payload["value"] is None:
        return clarification_answer("I couldn't compute that percentage from the days on hand. Try snowfall, freeze, heat, or heavy rain.")
    value = payload["value"]
    noun = "snow days" if resolved.metric == "snowfall_day_pct" else payload["label"].lower()
    threshold = _metric_threshold(resolved.metric or "")
    source = "offline stand-in for the Open-Meteo archive" if report.weather_fallback else "Open-Meteo archive"
    seen: set[str] = set()
    direct = (
        f"{_mention(report, seen)} had {payload['numerator']} {noun} in {report.window_label}, "
        f"{value:.1f}% of {payload['denominator']} valid days. {threshold} Source: {source}."
    )
    text = _finish_text(resolved, [report], [direct])
    findings = [
        f"{payload['label']}: {value:.1f}%",
        f"{payload['numerator']} days met the threshold out of {payload['denominator']} valid days.",
        f"Window: {report.window_label}.",
    ]
    scores = [
        ScoreItem(
            hub_id=report.hub_id,
            hub_name=report.name,
            hazard=payload["name"],
            score=_one_decimal(value),
            unit="percent",
        )
    ]
    answer = _base(resolved, [report], text, findings, _followups_for(resolved, [report]))
    return answer.model_copy(update={"scores": scores})


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


def _basis(resolved: ResolvedIntent, reports: list[HubReport]) -> str:
    label = reports[0].window_label.lower() if reports else ""
    if resolved.time_window == "trailing_365" or "trailing 365" in label:
        return "Based on the trailing 365 days of weather data and FEMA risk data."
    return "Based on 2025 weather data and FEMA risk data."


def _finish_text(resolved: ResolvedIntent, reports: list[HubReport], sentences: list[str]) -> str:
    parts = [_window_notice(resolved), *sentences, _basis(resolved, reports)]
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
    return f"The FEMA {_join([HAZARD_LABEL[hazard] for hazard in identical])} baseline does not separate them."


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
    return f"What separates them is {_short_driver(best[1])}."


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
        names = ", ".join(report.name for report in reports)
        text = f"No hub has an active NWS alert. {names}: none."
    else:
        lines = [_alert_implications(active)]
        if quiet:
            lines.append("The rest, none: " + ", ".join(report.name for report in quiet) + ".")
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
    if resolved.intent != "explain_hub":
        return ""
    if not resolved.explain_gap and not re.search(r"\bwhy\b", resolved.question or "", re.I):
        return ""
    total = _rank_score(resolved, report) if resolved.explain_gap else report.focus_score(resolved.hazards)
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
    leader = by_id[resolved.hub_ids[0]]
    other = by_id[resolved.hub_ids[1]]
    seen: set[str] = set()
    text = _finish_text(resolved, [leader, other], [_gap_sentence(leader, other, resolved.hazards, seen, resolved)])
    return _pack(resolved, [leader, other], text, [], _followups_for(resolved, [leader, other]))


def _gap_sentence(leader: HubReport, other: HubReport, hazards: list[str], seen: set[str], resolved: ResolvedIntent) -> str:
    name = _mention(other, seen)
    lead = _mention(leader, seen)
    score = _n(_rank_score(resolved, other))
    lead_score = _n(_rank_score(resolved, leader))
    phrase = _driver_point_phrase(other, hazards or ["overall"], _rank_score(resolved, other), resolved.excluded_hazards)
    base = f"{name} is lower at {score} than {lead} at {lead_score}"
    if phrase:
        return f"{base}, {phrase}."
    return f"{base}."


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


def _draft_is_usable(text: str, evidence: dict | None, scores: list[ScoreItem], skeleton: str = "") -> bool:
    return _draft_rejection(text, evidence, scores) is None


def _draft_rejection(text: str, evidence: dict | None, scores: list[ScoreItem]) -> str | None:
    notice = str((evidence or {}).get("required_notice") or "")
    if notice and notice not in text:
        return "missing required window notice"
    if not notice and WINDOW_NOTICE in text:
        return "window notice was not asked for"
    if (evidence or {}).get("unsupported_window") and _UNSUPPORTED_WINDOW_WORD.search(text):
        return "attributes yearly values to an unsupported window"
    phrase = str((evidence or {}).get("required_driver_phrase") or "")
    if phrase and phrase not in text:
        return "why answer omitted the top contributors"
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
