"""Deterministic briefings. The model may replace the prose later; scores stay here."""

from __future__ import annotations

import re

from app.agent.schemas import AnswerDraft, AnswerResult, ScoreItem, SourceItem
from app.agent.session_context import ResolvedIntent
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


def deterministic_answer(resolved: ResolvedIntent, reports: list[HubReport]) -> AnswerResult:
    ordered = order_reports(reports, resolved.hazards, resolved.safety_framing)
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
    focus = list(resolved.hazards)
    if resolved.intent == "metric_lookup":
        related = METRIC_HAZARD.get(resolved.metric or "")
        if related:
            focus = [related]
    ordered = order_reports(reports, focus, resolved.safety_framing)
    metric = None
    if resolved.intent == "metric_lookup" and ordered and resolved.metric:
        metric = metric_payload(ordered[0], resolved.metric)
        if metric and metric.get("value") is not None:
            metric = {**metric, "value": round(float(metric["value"]), 1)}
    window = ordered[0] if ordered else None
    return {
        "window_label": window.window_label if window else "",
        "window_start": window.window_start if window else "",
        "window_end": window.window_end if window else "",
        "focus_hazards": focus,
        "safety_framing": resolved.safety_framing,
        "used_fallback_weather": any(report.weather_fallback for report in ordered),
        "rankings": [_ranking_row(report, focus) for report in ordered],
        "metric": metric,
        "detail": resolved.detail,
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
    if drafted is not None and not _draft_is_usable(drafted.answer, evidence, base.scores, base.answer):
        drafted = None
        failure_note = failure_note or (
            "The language model draft did not stay inside the scored evidence, so this wording comes from the scored evidence."
        )
    if drafted is None:
        if failure_note:
            uncertainties.insert(0, "The language model did not return a valid briefing, so this wording comes from the scored evidence.")
        else:
            assumptions.append("Wording is assembled directly from the scored evidence.")
        return base.model_copy(update={"assumptions": _unique(assumptions), "uncertainties": _unique(uncertainties)})
    answer = drafted.answer.strip()
    if len(answer) < 40:
        answer = base.answer
    return AnswerResult(
        answer=answer,
        key_findings=list(base.key_findings),
        scores=list(base.scores),
        assumptions=_unique(assumptions),
        uncertainties=_unique(uncertainties),
        sources=list(base.sources),
        follow_up_questions=list(base.follow_up_questions)[:4],
    )


def _rank_answer(resolved: ResolvedIntent, reports: list[HubReport]) -> AnswerResult:
    focus = resolved.hazards
    label = _focus_label(focus)
    scope = f"{resolved.region} hubs in this set" if resolved.region else "this hub set"
    if not reports:
        return clarification_answer("None of the hubs in this set match that region.")
    leader = reports[0]
    leader_score = leader.focus_score(focus)
    if resolved.safety_framing:
        direct = f"{_place(leader)} has the lowest {label} score among {scope}, at {_n(leader_score)}/100. Lower means less disruption exposure on this index."
    else:
        verb = "highest" if focus else "highest overall"
        direct = f"{_place(leader)} has the {verb} {label} score among {scope}, at {_n(leader_score)}/100."
    rest = [f"{_place(report)} {_n(report.focus_score(focus))}" for report in reports[1:4]]
    if rest:
        direct += " Next: " + ", ".join(rest) + "."
    why = _contributor_sentence(leader, focus or ["overall"], leader_score)
    gaps = [_lower_reason(leader, other, focus) for other in reports[1:3]]
    text = _compose(resolved, reports, direct, " ".join(part for part in [why, *gaps] if part))
    findings = [f"{report.name} {label} score: {_n(report.focus_score(focus))}" for report in reports[:5]]
    driver = _top_driver(leader, focus)
    if driver:
        findings.append(f"Largest {label} driver for {leader.name}: {driver.metric_name} at {driver.value:.1f} (contribution {driver.contribution:.1f}).")
    return _pack(resolved, reports, text, findings, _followups_for(resolved, reports))


def _compare_answer(resolved: ResolvedIntent, reports: list[HubReport]) -> AnswerResult:
    focus = resolved.hazards or ["overall"]
    findings = []
    sentences = []
    driver_bits = []
    for hazard in focus:
        scored = sorted(reports, key=lambda report: report.focus_score([hazard]), reverse=not resolved.safety_framing)
        left, right = scored[0], scored[-1]
        left_score = left.focus_score([hazard])
        right_score = right.focus_score([hazard])
        label = HAZARD_LABEL[hazard]
        if abs(left_score - right_score) <= 1.0:
            sentences.append(
                f"On {label}, {_place(left)} ({_n(left_score)}/100) and {_place(right)} ({_n(right_score)}/100) are practically tied."
            )
        elif resolved.safety_framing:
            sentences.append(f"On {label}, {_place(left)} is lower at {_n(left_score)}/100 versus {_place(right)} at {_n(right_score)}/100.")
        else:
            sentences.append(f"On {label}, {_place(left)} is higher at {_n(left_score)}/100 versus {_place(right)} at {_n(right_score)}/100.")
        driver_bits.append(_baseline_sentence(left, right, hazard))
        driver_bits.append(_separator_sentence(left, right, hazard))
        for report in reports:
            findings.append(f"{report.name} {label}: {_n(report.focus_score([hazard]))}")
    why = " ".join(part for part in driver_bits if part)
    text = _compose(resolved, reports, " ".join(sentences), why)
    return _pack(resolved, reports, text, findings[:8], _followups_for(resolved, reports))


def _explain_answer(resolved: ResolvedIntent, reports: list[HubReport]) -> AnswerResult:
    report = reports[0]
    focus = resolved.hazards
    if focus:
        score = report.focus_score(focus)
        label = _focus_label(focus)
        direct = f"The {label} score for {_place(report)} is {_n(score)}/100."
    else:
        score = report.overall_score
        label = "overall disruption"
        direct = f"The overall weather-disruption score for {_place(report)} is {_n(score)}/100."
    text = _compose(resolved, [report], direct, _contributor_sentence(report, focus or ["overall"], score))
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
    direct = (
        f"{_place(report)} had {payload['numerator']} {noun} in {report.window_label}, "
        f"{value:.1f}% of {payload['denominator']} valid days. {threshold} Source: {source}."
    )
    text = " ".join(part for part in (direct, _uncertainty_sentence(resolved, [report])) if part)
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
    return AnswerResult(
        answer=text,
        key_findings=findings[:6],
        scores=_score_items(resolved, reports),
        assumptions=_assumptions(resolved, reports),
        uncertainties=_uncertainties(resolved, reports),
        sources=_source_items(reports),
        follow_up_questions=_filter_followups(resolved, reports, followups)[:3],
    )


def _score_items(resolved: ResolvedIntent, reports: list[HubReport]) -> list[ScoreItem]:
    hazards = resolved.hazards or ["overall"]
    if resolved.intent == "explain_hub" and reports:
        hazards = ["overall", "winter", "flood", "hurricane", "severe_storm", "heat"]
    items: list[ScoreItem] = []
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
    assumption = _assumption_sentence(resolved, reports)
    uncertainty = _uncertainty_sentence(resolved, reports)
    sources = _compact_sources(reports, inherited=resolved.inherited)
    if resolved.detail == "sources":
        parts = [sources, direct, scope, uncertainty]
    elif resolved.detail == "assumptions":
        parts = [assumption, uncertainty, direct, scope]
    elif resolved.detail in {"methodology", "evidence"}:
        parts = [direct, why, assumption, uncertainty, scope]
    else:
        parts = [direct, why, scope, assumption, uncertainty, sources]
    return " ".join(part.strip() for part in parts if part and part.strip())


def _place(report: HubReport) -> str:
    return f"{report.name} hub ({report.region} region)"


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


def _contributor_sentence(report: HubReport, hazards: list[str], total: float) -> str:
    named = []
    for driver in _ranked_drivers(report, hazards):
        if driver.metric_name in HURRICANE_DRIVERS and not report.hurricane_published and "hurricane" not in hazards:
            continue
        named.append(driver)
    parts = [f"{_short_driver(driver)} ({_n(driver.contribution)})" for driver in named]
    shown = sum(round(driver.contribution, 1) for driver in named)
    other = round(round(total, 1) - shown, 1)
    if other != 0:
        parts.append(f"other factors: ({other:.1f})")
    if not parts:
        return ""
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


def _lower_reason(leader: HubReport, other: HubReport, hazards: list[str]) -> str:
    leader_drivers = {driver.metric_name: driver for driver in _ranked_drivers(leader, hazards or ["overall"])}
    other_drivers = {driver.metric_name: driver for driver in _ranked_drivers(other, hazards or ["overall"])}
    best = None
    for name, left in leader_drivers.items():
        if name in HURRICANE_DRIVERS and not leader.hurricane_published and "hurricane" not in hazards:
            continue
        right = other_drivers.get(name)
        if right is None:
            continue
        gap = left.contribution - right.contribution
        if best is None or gap > best[0]:
            best = (gap, left, right)
    score = _n(other.focus_score(hazards))
    if best and best[0] > 0.05:
        _gap, left, right = best
        return (
            f"{_place(other)} is lower at {score} because its {_short_driver(right)} contributes {_n(right.contribution)} "
            f"versus {_n(left.contribution)} for {_place(leader)}."
        )
    return f"{_place(other)} is lower at {score}."


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
        text += " Live historical weather was unavailable, so the day counts are an offline stand-in, not verified observations."
    gap = _climate_gap(reports[0]) if reports and (not resolved.hazards or "winter" in resolved.hazards) else None
    if gap and resolved.intent != "metric_lookup":
        text += " " + gap
    return text


def _compact_sources(reports: list[HubReport], inherited: bool = False) -> str:
    weather = "an offline climatology stand-in" if any(report.weather_fallback for report in reports) else "Open-Meteo archive"
    lead = "Sources, same as the previous turn" if inherited else "Sources"
    return f"{lead}: {weather}, NWS active alerts, and the FEMA National Risk Index snapshot."


def _draft_is_usable(text: str, evidence: dict | None, scores: list[ScoreItem], skeleton: str = "") -> bool:
    words = len(text.split())
    detail = (evidence or {}).get("detail")
    if words > (280 if detail else 220):
        return False
    if evidence and evidence.get("metric") and re.search(r"risk score|/100", text, re.I):
        return False
    if "Contributors, largest first" in skeleton:
        head = skeleton.split("These points sum to", 1)[0]
        for number in re.findall(r"\((-?\d+(?:\.\d+)?)\)", head):
            if number not in text:
                return False
    if evidence is None:
        return True
    allowed: set[float] = set()
    _collect_numbers(evidence, allowed)
    for item in scores:
        _collect_numbers(item.model_dump(), allowed)
    for raw in re.findall(r"\d+(?:\.\d+)?", text):
        value = float(raw)
        if value in {0, 1, 2, 3, 4, 5, 10, 32, 40, 95, 100} or 2020 <= value <= 2027:
            continue
        if any(abs(value - known) <= 0.15 for known in allowed):
            continue
        return False
    return True


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
