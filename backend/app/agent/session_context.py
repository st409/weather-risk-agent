"""Rule-based intent parsing and follow-up memory.

Python fills missing hubs, region, and hazards from the last turn.
If that cannot be done safely, the agent asks one clarifying question.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field

from app.risk.hubs import AIRPORT_CODES, ALIASES, HUB_BY_ID, HUBS, OUTSIDE_PLACES, REGIONS
from app.risk.windows import WINDOW_KEYS

HAZARDS = ("hurricane", "flood", "heat", "severe_storm", "winter")
METRICS = {
    "snowfall_day_pct",
    "freeze_day_pct",
    "heat_day_pct",
    "extreme_heat_day_pct",
    "heavy_rain_day_pct",
    "very_heavy_rain_day_pct",
}
WINDOWS = set(WINDOW_KEYS)

_HAZARD_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("hurricane", re.compile(r"\b(hurricanes?|tropical(?:\s+storm)?|storm surge)\b", re.I)),
    ("flood", re.compile(r"\b(floods?|flooding|heavy rain)\b", re.I)),
    ("heat", re.compile(r"\b(heat|heatwave|heat wave)\b", re.I)),
    ("severe_storm", re.compile(r"\b(severe storms?|tornadoes?|thunderstorms?|hail)\b", re.I)),
    ("winter", re.compile(r"\b(winter|snowfall|snow|freezing|freeze|blizzard|ice)\b", re.I)),
)
_REGION_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("Midwest", re.compile(r"\b(?:midwestern|midwest)\b", re.I)),
    ("Northeast", re.compile(r"\b(?:northeast|north\s+east)\b", re.I)),
    ("Mountain", re.compile(r"\b(?:rocky mountains|rockies|mountain west|mountain region|mountain)\b", re.I)),
    ("West", re.compile(r"\b(?:west coast|the west|west)\b", re.I)),
    ("South", re.compile(r"\b(?:southeast|gulf coast|the south|southern|south)\b", re.I)),
)
METRIC_HAZARD = {
    "snowfall_day_pct": "winter",
    "freeze_day_pct": "winter",
    "heat_day_pct": "heat",
    "extreme_heat_day_pct": "heat",
    "heavy_rain_day_pct": "flood",
    "very_heavy_rain_day_pct": "flood",
}


@dataclass
class SessionContext:
    hub_ids: list[str] = field(default_factory=list)
    region: str | None = None
    hazards: list[str] = field(default_factory=list)
    metric: str | None = None
    time_window: str | None = None
    last_intent: str | None = None
    last_result_hub_ids: list[str] = field(default_factory=list)
    last_user_message: str = ""
    offered_breakdown: bool = False
    focus_hub_id: str | None = None
    excluded_hazards: list[str] = field(default_factory=list)
    score_legend_shown: bool = False

    @classmethod
    def from_dict(cls, raw: dict | None) -> "SessionContext":
        raw = raw or {}
        return cls(
            hub_ids=[hub_id for hub_id in raw.get("hub_ids") or [] if hub_id in HUB_BY_ID],
            region=raw.get("region") if raw.get("region") in REGIONS else None,
            hazards=[hazard for hazard in raw.get("hazards") or [] if hazard in HAZARDS],
            metric=raw.get("metric") if raw.get("metric") in METRICS else None,
            time_window=raw.get("time_window") if raw.get("time_window") in WINDOWS else None,
            last_intent=raw.get("last_intent"),
            last_result_hub_ids=[hub_id for hub_id in raw.get("last_result_hub_ids") or [] if hub_id in HUB_BY_ID],
            last_user_message=str(raw.get("last_user_message") or "")[:500],
            offered_breakdown=bool(raw.get("offered_breakdown")),
            focus_hub_id=raw.get("focus_hub_id") if raw.get("focus_hub_id") in HUB_BY_ID else None,
            excluded_hazards=[hazard for hazard in raw.get("excluded_hazards") or [] if hazard in HAZARDS],
            score_legend_shown=bool(raw.get("score_legend_shown")),
        )

    def to_dict(self) -> dict:
        return {
            "hub_ids": self.hub_ids,
            "region": self.region,
            "hazards": self.hazards,
            "metric": self.metric,
            "time_window": self.time_window,
            "last_intent": self.last_intent,
            "last_result_hub_ids": self.last_result_hub_ids,
            "last_user_message": self.last_user_message,
            "offered_breakdown": self.offered_breakdown,
            "focus_hub_id": self.focus_hub_id,
            "excluded_hazards": self.excluded_hazards,
            "score_legend_shown": self.score_legend_shown,
        }

    def for_intent_parser(self) -> dict:
        """Previous turn handed to the intent model. It does not include scores."""
        return {
            "previous_user_message": self.last_user_message,
            "intent": self.last_intent,
            "hub_ids": self.hub_ids,
            "region": self.region,
            "hazards": self.hazards,
            "metric": self.metric,
            "time_window": self.time_window,
            "last_result_hub_ids": self.last_result_hub_ids,
        }


@dataclass
class ParsedMessage:
    intent: str = "unknown"
    hub_ids: list[str] = field(default_factory=list)
    region: str | None = None
    hazards: list[str] = field(default_factory=list)
    metric: str | None = None
    time_window: str | None = None
    region_explicit: bool = False
    hazards_explicit: bool = False
    asks_why_top: bool = False
    asks_safer: bool = False
    unknown_place: str | None = None
    inherited: bool = False
    detail: str | None = None
    excluded_hazards: list[str] = field(default_factory=list)
    ordinal_index: int | None = None
    unsupported_window: bool = False
    explain_gap: bool = False
    rank_gap: bool = False
    subject_hub_id: str | None = None
    asks_investment: bool = False
    text: str = ""


@dataclass
class ResolvedIntent:
    intent: str
    hub_ids: list[str]
    region: str | None
    hazards: list[str]
    metric: str | None
    time_window: str | None
    needs_clarification: bool
    clarifying_question: str | None
    safety_framing: bool = False
    inherited: bool = False
    detail: str | None = None
    question: str = ""
    prior_question: str = ""
    excluded_hazards: list[str] = field(default_factory=list)
    unsupported_window: bool = False
    explain_gap: bool = False
    rank_gap: bool = False
    subject_hub_id: str | None = None
    asks_investment: bool = False
    show_score_legend: bool = True

    def view(self) -> dict:
        return {
            "intent": self.intent if not self.needs_clarification else "unknown",
            "hub_ids": self.hub_ids,
            "region": self.region,
            "hazards": self.hazards,
            "metric": self.metric,
            "time_window": self.time_window,
            "needs_clarification": self.needs_clarification,
            "clarifying_question": self.clarifying_question,
        }


def parse_message(message: str) -> ParsedMessage:
    text = _fold_punctuation(" ".join(message.strip().split()))
    if _non_latin(text):
        return ParsedMessage(intent="unsupported_language")
    low = text.lower()
    if re.fullmatch(r"(?:hi|hello|hey|thanks|thank you|good morning|good afternoon)[.!]?", low):
        return ParsedMessage(intent="greeting")
    occupied = [False] * len(low)
    hub_ids = _mark_aliases(text, AIRPORT_CODES, occupied)
    hub_ids.extend(_mark_aliases(low, ALIASES, occupied))
    hub_ids.extend(_fuzzy_hubs(low, occupied, hub_ids))
    unknown = _first_outside(low, occupied)
    regions = _find_regions(low)
    region = regions[0] if len(regions) == 1 else None
    if len(regions) > 1:
        for hub in HUBS:
            if hub.region in regions and hub.hub_id not in hub_ids:
                hub_ids.append(hub.hub_id)
    excluded = _negated_hazards(low)
    hazards = [name for name, pattern in _HAZARD_PATTERNS if pattern.search(low) and name not in excluded]
    metric = _metric(low)
    window = _window(low)
    unsupported_window = _unsupported_window(low)
    if unsupported_window:
        window = "last_calendar_year"
    why = bool(re.search(r"\b(why|explain|drivers?|how come)\b", low))
    ordinal = _ordinal_index(low)
    asks_why_top = bool(why and not hub_ids and re.search(r"\b(top|first|highest|that one)\b", low))
    metric_q = _is_metric_question(low)
    compare = bool(re.search(r"\b(compare|comparison|versus|vs\.?|between)\b", low))
    investment = bool(re.search(r"\b(?:invest\w*|strengthen\w*|prioriti[sz]\w*)\b", low))
    rank = bool(re.search(r"\b(which|most|least|ranking|rank|highest|lowest|exposed|riskiest|safest|safer)\b", low))
    refers = _refers_back(low)
    followish = bool(refers) or (" only" in f" {low}" and not compare and not rank and not why and not metric_q)
    asks_safer = bool(re.search(r"\b(safer|safest)\b", low))
    detail = _detail_request(low)
    breakdown_ask = bool(
        re.search(r"\b(?:full score breakdown|score breakdown|full breakdown|breakdown|why exactly|show details)\b", low)
    )
    if hub_ids and re.search(r"\bhow is it calculated\b", low):
        breakdown_ask = True
    if breakdown_ask:
        detail = "breakdown"
    alerts = bool(re.search(r"\balerts?\b", low)) and not metric_q and not compare
    methodology = (
        detail in {"methodology", "sources", "assumptions"}
        and not breakdown_ask
        and not metric_q
        and not compare
        and not why
    )

    if re.fullmatch(r"(?:yes|yeah|yep|sure|ok|okay)[.!]?", low):
        intent = "follow_up"
    elif ordinal is not None and not hub_ids:
        intent = "explain_hub"
    elif asks_why_top:
        intent = "explain_hub"
        ordinal = 0
    elif breakdown_ask and not hub_ids and not compare and not rank and not metric_q:
        intent = "follow_up"
    elif methodology and not refers:
        intent = "methodology"
    elif alerts and not rank:
        intent = "alerts"
    elif _bare_why(low):
        intent = "follow_up"
    elif why and re.search(r"\bwhich\b", low) and not hub_ids:
        intent = "rank_hubs"
    elif why:
        intent = "explain_hub"
    elif metric_q:
        intent = "metric_lookup"
    elif investment and len(hub_ids) == 1:
        intent = "explain_hub"
    elif investment:
        intent = "rank_hubs"
    elif len(regions) > 1 or rank or (region and not hub_ids) or excluded:
        intent = "rank_hubs"
    elif compare or (len(hub_ids) >= 2 and not rank):
        intent = "compare_hubs"
    elif detail and not hub_ids and not compare and not rank and not why and not metric_q:
        intent = "follow_up"
    elif followish and not hub_ids:
        intent = "follow_up"
    elif _window(low) and not hub_ids and not metric_q and not compare and not rank and not why and not breakdown_ask:
        intent = "follow_up"
    elif len(hub_ids) == 1:
        intent = "explain_hub"
    elif len(hub_ids) >= 2:
        intent = "compare_hubs"
    elif not _weatherish(low) and not unknown:
        intent = "off_topic"
    else:
        intent = "unknown"

    return ParsedMessage(
        intent=intent,
        hub_ids=hub_ids,
        region=region,
        hazards=hazards,
        metric=metric if metric_q else None,
        time_window=window,
        region_explicit=bool(regions),
        hazards_explicit=bool(hazards),
        asks_why_top=asks_why_top,
        asks_safer=asks_safer,
        unknown_place=unknown,
        detail=detail,
        excluded_hazards=excluded,
        ordinal_index=ordinal,
        unsupported_window=unsupported_window,
        asks_investment=investment,
        text=text,
    )


def resolve_intent(parsed: ParsedMessage, context: SessionContext | None) -> ResolvedIntent:
    context = context or SessionContext()
    if parsed.intent in {"unsupported_language", "greeting", "off_topic", "methodology", "alerts"}:
        return _ok(parsed)
    if re.search(r"\bsimilar(?:ity)?\b", parsed.text, re.I):
        return _clarify(
            "Similarity isn't a supported comparison. Name two hubs and I can compare them.",
            parsed,
        )
    if parsed.unknown_place:
        return _clarify(
            f"I don't cover {parsed.unknown_place}. This assistant scores 10 regional hubs: Chicago, Minneapolis, Detroit, Denver, Dallas, Houston, Miami, Atlanta, New York/Newark, and Los Angeles.",
            parsed,
        )
    if parsed.intent == "follow_up" and _bare_yes(parsed.text) and context.offered_breakdown:
        parsed.detail = "breakdown"
    if parsed.ordinal_index is not None and not parsed.hub_ids:
        return _from_ordinal(parsed, context)
    if parsed.asks_why_top:
        return _why_top(parsed, context)
    if parsed.asks_safer:
        return _safer(parsed, context)

    parsed = _preserve_intent_for_window(parsed, context)
    parsed = _maybe_inherit(parsed, context)
    parsed = _fill_compare_partner(parsed, context)
    parsed = _fill_why_lower(parsed, context)
    parsed = _keep_metric_follow_up(parsed, context)
    parsed = _continue_bare_why(parsed, context)
    parsed = _relative_hub_follow_up(parsed, context)
    if parsed.intent in {"unknown", "follow_up"}:
        return _clarify(
            "I can rank these hubs, compare two of them, look up a weather percentage, or explain a score. What should I look up?",
            parsed,
        )
    problem = _slot_problem(parsed)
    if problem:
        return _clarify(problem, parsed)
    return _ok(parsed)


def _from_ordinal(parsed: ParsedMessage, context: SessionContext) -> ResolvedIntent:
    ordered = list(context.last_result_hub_ids or context.hub_ids)
    if not ordered:
        return _clarify("I don't have a previous ranking to point at. Which hub should I explain?", parsed)
    index = parsed.ordinal_index if parsed.ordinal_index is not None else 0
    if index < 0:
        index = len(ordered) - 1
    if index >= len(ordered):
        return _clarify(f"The previous result only has {len(ordered)} hubs.", parsed)
    parsed.intent = "explain_hub"
    parsed.region = None
    parsed.region_explicit = False
    if _asks_why_lower(parsed.text) and index != 0:
        parsed.explain_gap = True
        parsed.hub_ids = [ordered[0], ordered[index]]
        if not parsed.hazards_explicit:
            parsed.hazards = list(context.hazards)
    else:
        parsed.hub_ids = [ordered[index]]
        if not parsed.hazards_explicit:
            parsed.hazards = []
    if parsed.time_window is None:
        parsed.time_window = context.time_window
    parsed.inherited = True
    return _ok(parsed)


def _why_top(parsed: ParsedMessage, context: SessionContext) -> ResolvedIntent:
    ordered = context.last_result_hub_ids or context.hub_ids
    if not ordered:
        return _clarify("I don't have a previous result to point at. Which hub should I explain?", parsed)
    parsed.intent = "explain_hub"
    parsed.hub_ids = [ordered[0]]
    parsed.region = None
    parsed.region_explicit = False
    if not parsed.hazards_explicit:
        parsed.hazards = []
    if parsed.time_window is None:
        parsed.time_window = context.time_window
    parsed.inherited = True
    return _ok(parsed)


def _safer(parsed: ParsedMessage, context: SessionContext) -> ResolvedIntent:
    hubs = list(parsed.hub_ids)
    region = parsed.region
    inherited = False
    if not hubs and region is None and context.last_intent:
        inherited = True
        if context.last_intent == "compare_hubs" and (context.hub_ids or context.last_result_hub_ids):
            hubs = list(context.hub_ids or context.last_result_hub_ids)
        elif context.region:
            region = context.region
        elif context.last_result_hub_ids:
            hubs = list(context.last_result_hub_ids)
    hazards = list(parsed.hazards) if parsed.hazards_explicit else []
    window = parsed.time_window or (context.time_window if inherited else None)
    if len(hubs) >= 2:
        return ResolvedIntent("compare_hubs", hubs[:4], None, hazards, None, window, False, None, True, inherited, parsed.detail)
    if len(hubs) == 1 and region is None:
        name = HUB_BY_ID[hubs[0]].name
        return _clarify(
            f"Safer means a lower weather-disruption score. Which hub should I compare with {name}?",
            parsed,
        )
    if region is None and not hubs:
        return ResolvedIntent(
            "rank_hubs",
            [],
            None,
            hazards,
            None,
            window,
            False,
            None,
            True,
            False,
            parsed.detail,
        )
    return ResolvedIntent("rank_hubs", hubs, region, hazards, None, window, False, None, True, inherited, parsed.detail)


def _preserve_intent_for_window(parsed: ParsedMessage, context: SessionContext) -> ParsedMessage:
    """A month or custom range keeps the question already in progress and says so."""
    if not parsed.unsupported_window or not context.last_intent:
        return parsed
    if context.last_intent in {"greeting", "off_topic", "unsupported_language", "methodology", "alerts"}:
        return parsed
    low = parsed.text.lower()
    starts_new = bool(re.search(r"\b(why|explain|compare|versus|vs\.?|which|most|least|rank|ranking|safest|safer|between)\b", low))
    if parsed.intent == "metric_lookup" or (starts_new and parsed.intent != "follow_up"):
        return parsed
    intent = context.last_intent
    hubs = list(parsed.hub_ids)
    region = parsed.region
    if intent == "compare_hubs" and not hubs:
        hubs = list(context.hub_ids or context.last_result_hub_ids)
    elif intent == "rank_hubs":
        if not parsed.region_explicit:
            region = context.region
        if not hubs and region is None:
            hubs = list(context.hub_ids or context.last_result_hub_ids)
        elif region is not None and not parsed.hub_ids:
            hubs = []
    elif intent in {"explain_hub", "metric_lookup"} and not hubs:
        ranked = list(context.last_result_hub_ids or [])
        # why? and "and Chicago?" leave last_intent as explain_hub. The ranked set is still the conversation.
        if intent == "explain_hub" and len(ranked) >= 2:
            intent = "rank_hubs"
            hubs = ranked
            if not parsed.region_explicit:
                region = context.region
            parsed.metric = None
        else:
            hubs = list((context.hub_ids or ranked)[:1])
    parsed.intent = intent
    parsed.hub_ids = hubs
    parsed.region = region
    if not parsed.hazards_explicit:
        parsed.hazards = list(context.hazards)
    if parsed.metric is None and intent != "rank_hubs":
        parsed.metric = context.metric
    if intent == "rank_hubs":
        parsed.metric = None
    parsed.time_window = "last_calendar_year"
    parsed.unsupported_window = True
    parsed.inherited = True
    return parsed


def _maybe_inherit(parsed: ParsedMessage, context: SessionContext) -> ParsedMessage:
    if parsed.intent != "follow_up" or not context.last_intent:
        return parsed
    window_shift = bool(parsed.time_window) and not parsed.hub_ids
    if parsed.detail != "breakdown" and not _refers_back(parsed.text) and not window_shift:
        return parsed
    hubs = list(parsed.hub_ids)
    region = parsed.region
    hazards = list(parsed.hazards) if parsed.hazards_explicit else list(context.hazards)
    metric = parsed.metric or context.metric
    window = parsed.time_window or context.time_window
    intent = context.last_intent
    if intent == "compare_hubs":
        if not hubs:
            hubs = list(context.hub_ids or context.last_result_hub_ids)
        intent = "compare_hubs"
    elif intent == "rank_hubs":
        if not parsed.region_explicit:
            region = context.region
        if not hubs and region is None:
            saved = list(context.hub_ids or [])
            ranked = list(context.last_result_hub_ids or [])
            # A ranking follow-up keeps the whole ranked set, not one discussed hub.
            hubs = ranked if len(saved) <= 1 and len(ranked) > 1 else (saved or ranked)
        elif region is not None and not parsed.hub_ids:
            hubs = []
        intent = "rank_hubs"
    elif intent == "explain_hub":
        if not hubs:
            hubs = [context.focus_hub_id] if context.focus_hub_id else list((context.hub_ids or context.last_result_hub_ids)[:1])
        intent = "explain_hub"
    elif intent == "metric_lookup":
        if parsed.detail == "breakdown" or (parsed.hazards_explicit and not parsed.metric):
            intent = "explain_hub"
            metric = None
            if not parsed.hazards_explicit:
                hazard = METRIC_HAZARD.get(context.metric or "")
                if hazard:
                    hazards = [hazard]
                    parsed.hazards_explicit = True
        else:
            intent = "metric_lookup"
        if not hubs:
            hubs = [context.focus_hub_id] if context.focus_hub_id else list((context.hub_ids or context.last_result_hub_ids)[:1])
    parsed.intent = intent or "unknown"
    parsed.hub_ids = hubs
    parsed.region = region
    parsed.hazards = hazards
    parsed.metric = metric
    parsed.time_window = window
    parsed.inherited = True
    return parsed


def _slot_problem(parsed: ParsedMessage) -> str | None:
    if parsed.intent == "compare_hubs" and len(parsed.hub_ids) < 2:
        return "Which two hubs should I compare?"
    if parsed.intent == "explain_hub" and len(parsed.hub_ids) == 0:
        return "Which hub should I explain?"
    if (
        parsed.intent == "explain_hub"
        and len(parsed.hub_ids) > 1
        and not parsed.explain_gap
        and not parsed.subject_hub_id
    ):
        parsed.intent = "compare_hubs"
        return None
    if parsed.intent == "metric_lookup" and parsed.metric not in METRICS:
        return "Which percentage should I calculate: snowfall days, freeze days, heat days, or heavy-rain days?"
    return None


def _ok(parsed: ParsedMessage) -> ResolvedIntent:
    return ResolvedIntent(
        intent=parsed.intent,
        hub_ids=list(parsed.hub_ids),
        region=parsed.region,
        hazards=list(parsed.hazards),
        metric=parsed.metric,
        time_window=parsed.time_window,
        needs_clarification=False,
        clarifying_question=None,
        safety_framing=False,
        inherited=parsed.inherited,
        detail=parsed.detail,
        excluded_hazards=list(parsed.excluded_hazards),
        unsupported_window=parsed.unsupported_window,
        explain_gap=parsed.explain_gap,
        rank_gap=parsed.rank_gap,
        subject_hub_id=parsed.subject_hub_id,
        asks_investment=parsed.asks_investment,
    )


def _clarify(question: str, parsed: ParsedMessage) -> ResolvedIntent:
    return ResolvedIntent(
        intent="unknown",
        hub_ids=list(parsed.hub_ids),
        region=parsed.region,
        hazards=list(parsed.hazards),
        metric=parsed.metric,
        time_window=parsed.time_window,
        needs_clarification=True,
        clarifying_question=question,
    )


_PUNCT_FOLDS = str.maketrans(
    {
        "\u2018": "'",
        "\u2019": "'",
        "\u201a": "'",
        "\u201c": '"',
        "\u201d": '"',
        "\u201e": '"',
        "\u2013": "-",
        "\u2014": "-",
        "\u2212": "-",
        "\u00b0": " ",
        "\u00ba": " ",
        "\u00a0": " ",
    }
)
_NON_LATIN = re.compile(
    "["
    "\u0370-\u03ff"  # Greek
    "\u0400-\u04ff"  # Cyrillic
    "\u0590-\u05ff"  # Hebrew
    "\u0600-\u06ff"  # Arabic
    "\u0900-\u097f"  # Devanagari
    "\u3040-\u30ff"  # Hiragana and Katakana
    "\u4e00-\u9fff"  # CJK
    "\uac00-\ud7af"  # Hangul
    "]"
)


def _fold_punctuation(text: str) -> str:
    """Curly quotes, dashes, and a degree sign are still English."""
    return text.translate(_PUNCT_FOLDS)


def _non_latin(text: str) -> bool:
    """Hebrew, Arabic, and other non-Latin scripts. Accented Latin letters stay."""
    return bool(_NON_LATIN.search(text))


def _refers_back(low: str) -> bool:
    text = low.lower()
    return bool(
        re.search(
            r"\b(it|them|those|that hub|those hubs|the same|what about|how about|instead|the top one|the first one|the second one|the third one|the last one|full score breakdown|score breakdown|full breakdown|breakdown|why exactly|show details)\b|#\s*[123]\b",
            text,
        )
    )


def _weatherish(low: str) -> bool:
    return bool(
        re.search(
            r"\b(weather|risk|risks|hubs?|disrupt\w*|expos\w*|snow|rain|flood\w*|heat|hurricane\w*|winter|storms?|alerts?|scores?|sources?|method\w*|weights?|assumptions?|safer|safest|compare|rank\w*|window)\b",
            low,
        )
    )


def _negated_hazards(low: str) -> list[str]:
    verbs = r"ignore|without|excluding|except|exclude|excluded|drop|not counting|leave out"
    found = []
    for name, pattern in _HAZARD_PATTERNS:
        before = rf"\b(?:{verbs})\b(?:\W+\w+){{0,4}}\W*(?:{pattern.pattern})"
        after = rf"(?:{pattern.pattern})(?:\W+\w+){{0,3}}\W*\b(?:{verbs})\b"
        if re.search(before, low) or re.search(after, low):
            found.append(name)
    return found


def _bare_yes(text: str) -> bool:
    return bool(re.fullmatch(r"(?:yes|yeah|yep|sure|ok|okay)[.!]?", text.strip().lower()))


def _bare_why(text: str) -> bool:
    return bool(re.fullmatch(r"why\??[.!]?", text.strip().lower()))


def _continue_bare_why(parsed: ParsedMessage, context: SessionContext) -> ParsedMessage:
    """'why?' keeps the previous hubs, hazard, exclusion, or day count."""
    if not _bare_why(parsed.text) or not context.last_intent:
        return parsed
    parsed.inherited = True
    parsed.time_window = parsed.time_window or context.time_window
    parsed.excluded_hazards = list(context.excluded_hazards)
    if context.last_intent == "compare_hubs":
        ordered = list(context.last_result_hub_ids or context.hub_ids)
        if len(ordered) >= 2:
            parsed.intent = "explain_hub"
            parsed.explain_gap = True
            parsed.hub_ids = ordered[:2]
            parsed.hazards = list(context.hazards)
            parsed.hazards_explicit = True
            parsed.metric = None
            parsed.region = None
        return parsed
    if context.last_intent == "metric_lookup" and context.metric in METRICS:
        parsed.intent = "metric_lookup"
        parsed.metric = context.metric
        parsed.hazards = []
        parsed.hazards_explicit = False
        parsed.region = context.region
        parsed.region_explicit = bool(context.region)
        parsed.hub_ids = list(context.hub_ids or context.last_result_hub_ids)
        return parsed
    if context.last_intent == "rank_hubs":
        ordered = list(context.last_result_hub_ids or context.hub_ids)
        parsed.excluded_hazards = list(context.excluded_hazards)
        parsed.region = context.region
        parsed.region_explicit = bool(context.region)
        parsed.hazards = list(context.hazards)
        parsed.hazards_explicit = bool(context.hazards)
        parsed.metric = None
        if len(ordered) >= 2:
            parsed.intent = "explain_hub"
            parsed.explain_gap = True
            parsed.rank_gap = True
            parsed.hub_ids = ordered[:2]
        else:
            parsed.intent = "rank_hubs"
            parsed.hub_ids = ordered
        return parsed
    return parsed


def _relative_hub_follow_up(parsed: ParsedMessage, context: SessionContext) -> ParsedMessage:
    """'and Chicago?' keeps the ranked set and describes that hub inside it."""
    if context.last_intent not in {"rank_hubs", "compare_hubs", "explain_hub"}:
        return parsed
    if parsed.intent != "explain_hub" or len(parsed.hub_ids) != 1 or not _hub_only_follow_up(parsed):
        return parsed
    ordered = list(context.last_result_hub_ids or context.hub_ids)
    subject = parsed.hub_ids[0]
    if subject not in ordered or len(ordered) < 2:
        return parsed
    parsed.subject_hub_id = subject
    parsed.hub_ids = ordered
    parsed.inherited = True
    if not parsed.hazards_explicit:
        parsed.hazards = list(context.hazards)
        parsed.hazards_explicit = bool(parsed.hazards)
    if context.excluded_hazards:
        parsed.excluded_hazards = list(context.excluded_hazards)
    if not parsed.region_explicit:
        parsed.region = context.region
    if parsed.time_window is None:
        parsed.time_window = context.time_window
    return parsed


def _asks_why_lower(text: str) -> bool:
    low = text.lower()
    return bool(re.search(r"\bwhy\b", low) and re.search(r"\blower\b", low))


def _fuzzy_hubs(low: str, occupied: list[bool], already: list[str]) -> list[str]:
    """A close spelling of a hub name counts as naming that hub. 'Dalas' is Dallas."""
    choices = [(alias, hub_id) for alias, hub_id in ALIASES if " " not in alias and len(alias) >= 4]
    found: list[tuple[int, str]] = []
    seen = set(already)
    for match in re.finditer(r"[a-z]{4,}", low):
        if any(occupied[match.start() : match.end()]):
            continue
        token = match.group(0)
        best_id = None
        best_ratio = 0.0
        for alias, hub_id in choices:
            if abs(len(alias) - len(token)) > 2:
                continue
            ratio = difflib.SequenceMatcher(None, token, alias).ratio()
            if ratio > best_ratio:
                best_ratio = ratio
                best_id = hub_id
        if best_id is None or best_ratio < 0.8 or best_id in seen:
            continue
        seen.add(best_id)
        for index in range(match.start(), match.end()):
            occupied[index] = True
        found.append((match.start(), best_id))
    found.sort(key=lambda item: item[0])
    return [hub_id for _, hub_id in found]


def _ordinal_index(low: str) -> int | None:
    if re.search(r"#\s*1\b|\b1st\b|\bfirst one\b|\bthe top one\b|\btop one\b", low):
        return 0
    if re.search(r"#\s*2\b|\b2nd\b|\bsecond one\b", low):
        return 1
    if re.search(r"#\s*3\b|\b3rd\b|\bthird one\b", low):
        return 2
    if re.search(r"\blast one\b", low):
        return -1
    return None


def _unsupported_window(low: str) -> bool:
    if _window(low) in WINDOWS:
        return False
    if re.search(
        r"\b(january|february|march|april|june|july|august|september|october|november|december|jan|feb|mar|apr|jun|jul|aug|sep|sept|oct|nov|dec)\b",
        low,
    ):
        return True
    if re.search(r"\b(?:in|during|for)\s+may\b|\bmay\s+\d{4}\b", low):
        return True
    if re.search(r"\b(q[1-4]|this month|last month|custom range)\b", low):
        return True
    if re.search(r"\bfrom\s+\d{4}-\d{2}-\d{2}\b|\bbetween\s+\w+\s+\d{4}\b", low):
        return True
    return False


def _detail_request(low: str) -> str | None:
    if re.search(r"\b(fresh(?:ness)?|how recent|up to date|retrieved)\b", low) and re.search(
        r"\b(data|weather|fema|sources?)\b", low
    ):
        return "methodology"
    if re.search(r"\b(sources?|citations?)\b", low):
        return "sources"
    if re.search(r"\b(assumptions?|limitations?|definitions?)\b", low):
        return "assumptions"
    if re.search(r"\b(methodolog\w*|breakdown|weights?|how (?:is|are|do you).{0,40}(?:score|index|calculated|computed))\b", low):
        return "methodology"
    if re.search(r"\b(evidence|break ?down|what (?:are|were) the drivers|show (?:me )?(?:the )?(?:drivers|math))\b", low):
        return "evidence"
    return None


def _mark_aliases(low: str, aliases: tuple[tuple[str, str], ...], occupied: list[bool]) -> list[str]:
    found: list[tuple[int, str]] = []
    seen: set[str] = set()
    for alias, hub_id in sorted(aliases, key=lambda item: len(item[0]), reverse=True):
        for match in re.finditer(rf"\b{re.escape(alias)}\b", low):
            if any(occupied[match.start() : match.end()]):
                continue
            for index in range(match.start(), match.end()):
                occupied[index] = True
            if hub_id not in seen:
                seen.add(hub_id)
                found.append((match.start(), hub_id))
    found.sort(key=lambda item: item[0])
    return [hub_id for _, hub_id in found]


def _first_outside(low: str, occupied: list[bool]) -> str | None:
    best: tuple[int, str] | None = None
    for alias, label in sorted(OUTSIDE_PLACES, key=lambda item: len(item[0]), reverse=True):
        for match in re.finditer(rf"\b{re.escape(alias)}\b", low):
            if any(occupied[match.start() : match.end()]):
                continue
            if best is None or match.start() < best[0]:
                best = (match.start(), label)
    return None if best is None else best[1]


def _find_regions(low: str) -> list[str]:
    """Every region named in the message. Longer phrases win, so 'mountain west' stays Mountain."""
    spans: list[tuple[int, int, str]] = []
    for name, pattern in _REGION_PATTERNS:
        for match in pattern.finditer(low):
            spans.append((match.start(), match.end(), name))
    spans.sort(key=lambda item: (item[1] - item[0], -item[0]), reverse=True)
    occupied = [False] * len(low)
    chosen: list[tuple[int, str]] = []
    for start, end, name in spans:
        if any(occupied[start:end]):
            continue
        for index in range(start, end):
            occupied[index] = True
        chosen.append((start, name))
    chosen.sort()
    regions: list[str] = []
    for _start, name in chosen:
        if name not in regions:
            regions.append(name)
    return regions


def _is_metric_question(low: str) -> bool:
    """Day counts stay on the metric, including 'most freeze days' and 'more snow days'."""
    if re.search(r"\b(percent|percentage|how many(?:\s+\w+){0,3}\s+days|share of days)\b|%", low):
        return True
    if re.search(r"\b(?:snow|snowfall|freeze|freezing|extreme heat|heat|heavy[-\s]?rain)\s+days\b", low):
        return True
    if re.search(r"\b95\s*f\b", low) and re.search(r"\bdays\b", low):
        return True
    return False


def _fill_compare_partner(parsed: ParsedMessage, context: SessionContext) -> ParsedMessage:
    """One named hub plus a previous ranking compares that hub with the current leader."""
    if parsed.intent != "compare_hubs" or len(parsed.hub_ids) != 1:
        return parsed
    ordered = list(context.last_result_hub_ids or context.hub_ids)
    if not ordered:
        return parsed
    named = parsed.hub_ids[0]
    partner = ordered[1] if named == ordered[0] and len(ordered) > 1 else ordered[0]
    if partner == named:
        return parsed
    parsed.hub_ids = [partner, named]
    parsed.inherited = True
    if not parsed.hazards_explicit and context.hazards:
        parsed.hazards = list(context.hazards)
        parsed.hazards_explicit = True
    return parsed


def _fill_why_lower(parsed: ParsedMessage, context: SessionContext) -> ParsedMessage:
    """'Why is Atlanta lower?' keeps the leader and explains the gap, not Atlanta alone."""
    if parsed.explain_gap or not _asks_why_lower(parsed.text) or len(parsed.hub_ids) != 1:
        return parsed
    ordered = list(context.last_result_hub_ids or context.hub_ids)
    if not ordered:
        return parsed
    subject = parsed.hub_ids[0]
    leader = ordered[0]
    if subject == leader:
        return parsed
    parsed.intent = "explain_hub"
    parsed.explain_gap = True
    parsed.hub_ids = [leader, subject]
    parsed.inherited = True
    if not parsed.hazards_explicit:
        parsed.hazards = list(context.hazards)
    return parsed


def _keep_metric_follow_up(parsed: ParsedMessage, context: SessionContext) -> ParsedMessage:
    """'and Denver?' after a day count stays on that count."""
    if context.last_intent != "metric_lookup" or context.metric not in METRICS:
        return parsed
    if parsed.detail == "breakdown" or not _hub_only_follow_up(parsed):
        return parsed
    parsed.intent = "metric_lookup"
    parsed.metric = context.metric
    parsed.hazards = []
    parsed.hazards_explicit = False
    parsed.inherited = True
    if parsed.time_window is None:
        parsed.time_window = context.time_window
    return parsed


def _hub_only_follow_up(parsed: ParsedMessage) -> bool:
    if len(parsed.hub_ids) != 1:
        return False
    stripped = re.sub(r"[^a-z\s]", " ", parsed.text.lower())
    names = (*ALIASES, *((code.lower(), hub_id) for code, hub_id in AIRPORT_CODES))
    for alias, hub_id in names:
        if hub_id == parsed.hub_ids[0]:
            stripped = re.sub(rf"\b{re.escape(alias)}\b", " ", stripped)
    glue = {"and", "also", "too", "what", "about", "how", "now", "please", "the", "for", "a"}
    return not [word for word in stripped.split() if word not in glue]


def _metric(low: str) -> str | None:
    if re.search(r"\b(snowfall|snow)\b", low):
        return "snowfall_day_pct"
    if re.search(r"\bfreez", low):
        return "freeze_day_pct"
    if re.search(r"\b(?:100|extreme heat)\b", low):
        return "extreme_heat_day_pct"
    if re.search(r"\b95\s*f\b|\b(?:95|heat)\b", low):
        return "heat_day_pct"
    if re.search(r"\b(very heavy|2 inches|two inches)\b", low):
        return "very_heavy_rain_day_pct"
    if re.search(r"\b(rain|precip)\b", low):
        return "heavy_rain_day_pct"
    return None


def _window(low: str) -> str | None:
    if re.search(r"\b(last year|past year|previous year|last calendar year)\b", low):
        return "last_calendar_year"
    if re.search(r"\b(last winter|previous winter)\b", low):
        return "last_winter"
    if re.search(r"\b(trailing|last 365|last 12 months|past 12 months)\b", low):
        return "trailing_365"
    return None
