"""Rule-based intent parsing and follow-up memory.

Python fills missing hubs, region, and hazards from the last turn.
If that cannot be done safely, the agent asks one clarifying question.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.risk.hubs import ALIASES, HUB_BY_ID, OUTSIDE_PLACES, REGIONS, hub_names

HAZARDS = ("hurricane", "flood", "heat", "severe_storm", "winter")
METRICS = {
    "snowfall_day_pct",
    "freeze_day_pct",
    "heat_day_pct",
    "extreme_heat_day_pct",
    "heavy_rain_day_pct",
    "very_heavy_rain_day_pct",
}
WINDOWS = {"last_calendar_year", "trailing_365", "last_winter"}

_HAZARD_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("hurricane", re.compile(r"\b(hurricanes?|tropical(?:\s+storm)?|storm surge)\b|הוריקן", re.I)),
    ("flood", re.compile(r"\b(floods?|flooding|heavy rain)\b|שיטפונ", re.I)),
    ("heat", re.compile(r"\b(heat|heatwave|heat wave)\b|חום", re.I)),
    ("severe_storm", re.compile(r"\b(severe storms?|tornadoes?|thunderstorms?|hail)\b|טורנדו|סופ[הת] קש", re.I)),
    ("winter", re.compile(r"\b(winter|snowfall|snow|freezing|freeze|blizzard|ice)\b|חורף|שלג", re.I)),
)
_REGION_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("Midwest", re.compile(r"\b(midwestern|midwest)\b", re.I)),
    ("Northeast", re.compile(r"\b(northeast|north\s+east)\b", re.I)),
    ("Mountain", re.compile(r"\b(rocky mountains|rockies|mountain west|mountain region|mountain)\b", re.I)),
    ("West", re.compile(r"\b(west coast|the west)\b", re.I)),
    ("South", re.compile(r"\b(southeast|gulf coast|the south|south)\b", re.I)),
)


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
    text = " ".join(message.strip().split())
    low = text.lower()
    occupied = [False] * len(low)
    hub_ids = _mark_aliases(low, ALIASES, occupied)
    unknown = _first_outside(low, occupied)
    region = _find_region(low)
    hazards = [name for name, pattern in _HAZARD_PATTERNS if pattern.search(low)]
    metric = _metric(low)
    window = _window(low)
    why = bool(re.search(r"\b(why|explain|drivers?|how come)\b", low))
    asks_why_top = bool(why and not hub_ids and re.search(r"\b(top|first|highest|that one)\b", low))
    metric_q = bool(re.search(r"\b(percent|percentage|how many days|share of days)\b", low))
    compare = bool(re.search(r"\b(compare|comparison|versus|vs\.?|between)\b", low))
    rank = bool(re.search(r"\b(which|most|least|ranking|rank|highest|lowest|exposed|riskiest|safest|safer)\b", low))
    followish = bool(re.search(r"\b(what about|how about|instead|the top one|the first one|that one)\b|מה לגבי|ומה לגבי", low)) or (
        (" only" in f" {low}" or "בלבד" in low) and not compare and not rank and not why and not metric_q
    )
    asks_safer = bool(re.search(r"\b(safer|safest)\b", low))
    detail = _detail_request(low)

    if asks_why_top:
        intent = "explain_hub"
    elif why:
        intent = "explain_hub"
    elif metric_q:
        intent = "metric_lookup"
    elif compare or (len(hub_ids) >= 2 and not rank):
        intent = "compare_hubs"
    elif rank or (region and not hub_ids):
        intent = "rank_hubs"
    elif detail and not hub_ids and not compare and not rank and not why and not metric_q:
        intent = "follow_up"
    elif followish and not hub_ids:
        intent = "follow_up"
    elif len(hub_ids) == 1:
        intent = "explain_hub"
    elif len(hub_ids) >= 2:
        intent = "compare_hubs"
    else:
        intent = "unknown"

    return ParsedMessage(
        intent=intent,
        hub_ids=hub_ids,
        region=region,
        hazards=hazards,
        metric=metric if metric_q else None,
        time_window=window,
        region_explicit=region is not None,
        hazards_explicit=bool(hazards),
        asks_why_top=asks_why_top,
        asks_safer=asks_safer,
        unknown_place=unknown,
        detail=detail,
    )


def resolve_intent(parsed: ParsedMessage, context: SessionContext | None) -> ResolvedIntent:
    context = context or SessionContext()
    if parsed.unknown_place:
        return _clarify(
            f"I don't cover {parsed.unknown_place}. This demo only scores {hub_names()}.",
            parsed,
        )
    if parsed.asks_why_top:
        return _why_top(parsed, context)
    if parsed.asks_safer:
        return _safer(parsed, context)

    parsed = _maybe_inherit(parsed, context)
    if parsed.intent in {"unknown", "follow_up"}:
        return _clarify(
            "I can rank these hubs, compare two of them, look up a weather percentage, or explain a score. What should I look up?",
            parsed,
        )
    problem = _slot_problem(parsed)
    if problem:
        return _clarify(problem, parsed)
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
        return _clarify(
            "Safer here means a lower weather-disruption score, not an emergency judgment. Which hubs or region should I compare?",
            parsed,
        )
    return ResolvedIntent("rank_hubs", hubs, region, hazards, None, window, False, None, True, inherited, parsed.detail)


def _maybe_inherit(parsed: ParsedMessage, context: SessionContext) -> ParsedMessage:
    if parsed.intent != "follow_up" or not context.last_intent:
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
            hubs = list(context.hub_ids or context.last_result_hub_ids)
        elif region is not None and not parsed.hub_ids:
            hubs = []
        intent = "rank_hubs"
    elif intent == "explain_hub":
        if not hubs:
            hubs = list((context.hub_ids or context.last_result_hub_ids)[:1])
        intent = "explain_hub"
    elif intent == "metric_lookup":
        if parsed.hazards_explicit and not parsed.metric:
            intent = "explain_hub"
        else:
            intent = "metric_lookup"
        if not hubs:
            hubs = list((context.hub_ids or context.last_result_hub_ids)[:1])
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
    if parsed.intent == "explain_hub" and len(parsed.hub_ids) > 1:
        parsed.intent = "compare_hubs"
        return None
    if parsed.intent == "metric_lookup" and len(parsed.hub_ids) != 1:
        return "Which hub should I calculate that for?"
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


def _detail_request(low: str) -> str | None:
    if re.search(r"\b(sources?|citations?)\b|מקורות", low):
        return "sources"
    if re.search(r"\b(assumptions?|limitations?|definitions?)\b|הנחות", low):
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


def _find_region(low: str) -> str | None:
    best: tuple[int, str] | None = None
    for name, pattern in _REGION_PATTERNS:
        match = pattern.search(low)
        if match and (best is None or match.start() < best[0]):
            best = (match.start(), name)
    return None if best is None else best[1]


def _metric(low: str) -> str | None:
    if re.search(r"\b(snowfall|snow)\b", low):
        return "snowfall_day_pct"
    if re.search(r"\bfreez", low):
        return "freeze_day_pct"
    if re.search(r"\b(100|extreme heat)\b", low):
        return "extreme_heat_day_pct"
    if re.search(r"\b(95|heat)\b", low):
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
