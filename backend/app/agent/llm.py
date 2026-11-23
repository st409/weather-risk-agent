"""Schema-checked model calls.

The model may draft an intent or a paragraph. It does not calculate scores.
If the key is missing, or the payload fails schema validation twice, callers
keep the deterministic result.
"""

from __future__ import annotations

import json
import re

from pydantic import BaseModel, ValidationError

from app.agent.schemas import (
    ANSWER_JSON_SCHEMA,
    INTENT_JSON_SCHEMA,
    AnswerDraft,
    IntentResult,
)
from app.config import llm_disabled, openai_api_key, openai_model

INTENT_SYSTEM = """You convert one chat turn about weather disruption at a fixed set of US logistics hubs into JSON.
Do not compute scores, rankings, or percentages.
Use only hub ids from the list in the user message.
The user message includes previous_turn from this same chat. Read it before you classify.
If the new message continues that turn and does not name new hubs, set intent to follow_up and leave hub_ids empty.
A follow-up such as "What about floods only?" or "ומה לגבי שיטפונות בלבד?" keeps the previous hubs and changes only the hazard the user just named.
If a place is outside the hub list, set needs_clarification to true and name the place in clarifying_question.
Set needs_clarification to false when the request can be answered from the hub list."""

ANSWER_SYSTEM = """You rewrite a factual skeleton into a short logistics briefing.
Keep a normal answer to 2 or 3 plain sentences, then the scores line, then the one-line source clause ("Sources: Open-Meteo 2025 weather, FEMA NRI, NWS alerts." or the trailing-365 version when that window is named).
If the question says "which hubs" or "rank", sentence 1 names every hub on the Scores line, uses plain city names, and has no digits. If it says "which hub", sentence 1 names one hub and has no digits.
Sentence 2 gives only the one or two concrete facts from the skeleton, such as a day count or a plain FEMA or alert fact. Do not use internal names such as "freeze-day rate", "FEMA winter baseline", "high-wind rate", or "severe-storm baseline". Do not say "significant portion", "significant share", or "more significantly". Do not say "most exposed" unless the skeleton says it.
Copy every count, percent, score, and contribution point from the skeleton. Write those numbers as digits: 14, not fourteen, and 99, not ninety-nine. You may reorder sentences, use synonyms, write 32°F for 32 F, or write "percent" for "%". Do not add numbers. Copy the skeleton's "Scores:" line, including "(higher = more exposed)" only when that Scores line has it. Do not repeat "(higher = more exposed)" in the reason. If the skeleton has no Scores line, do not write one. Prefer to keep a score number on that Scores line. If the skeleton says "Based on those risk drivers, I'd prioritize", keep that as its own sentence and do not attach it to the previous sentence or add a cost. If the skeleton does not say that, do not write "I'd prioritize". The lower score trails; never say the leader trails.
When a hazard was removed, do not explain the result with that hazard. Keep the remaining drivers the skeleton names.
When the skeleton explains a gap, start with who trails whom, then copy "contributes X vs Y". Do not lead with the scores. Those points are contributions, not 0-100 hazard scores. A score is not a probability.
If the skeleton says two hubs are effectively tied, keep "effectively tied" and do not call either one lower. If it says a hub is riskier, keep "is riskier" and do not call it lower.
If the skeleton includes "Only calendar year 2025 and trailing 365 days are supported, so this is for calendar year 2025", copy that sentence unchanged. Never say those yearly figures belong to a month or a custom range. Do not add that sentence when the skeleton omits it.
Do not write a "Contributors, largest first" breakdown. The server appends that after your text.
If the skeleton is a day count, keep every count, percent, threshold, and source, and write them as digits, for example "17 days, 4.7%". 32°F and 32 F are the same threshold. Do not add a risk score or a winter-score breakdown.
For a plural ranking, every sentence uses the plain city names from the skeleton. Do not write "hub (Region region)" anywhere in that answer. Sentence 1 names every hub on the Scores line, not only the first two. On other answers, the first mention of a hub is "<Name> hub (<Region> region)". After that, use only the hub name.
Use only numbers that appear in the skeleton or the evidence. Do not invent hubs, drivers, or scores. A snow day is daily snowfall >= 1 cm.
Return follow_up_questions as an empty list. The server supplies them."""


def llm_enabled() -> bool:
    return bool(openai_api_key()) and not llm_disabled()


def extract_json(text: str) -> str:
    raw = (text or "").strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?", "", raw, flags=re.I).strip()
        raw = re.sub(r"```$", "", raw).strip()
    return raw


def schema_retries(load_text, model_cls: type[BaseModel]) -> tuple[BaseModel | None, int]:
    """Try twice. load_text receives the previous validation error, or None."""
    error: Exception | None = None
    for attempt in range(2):
        text = load_text(error)
        try:
            return model_cls.model_validate_json(extract_json(text)), attempt + 1
        except (ValidationError, json.JSONDecodeError) as exc:
            error = exc
    return None, 2


def intent_user_message(message: str, previous_turn: str, hub_catalog: str) -> str:
    return (
        f"Hubs:\n{hub_catalog}\n\n"
        f"previous_turn:\n{previous_turn}\n\n"
        f"User message:\n{message}"
    )


def answer_user_message(question: str, read_only_scores: list, evidence: dict, skeleton: str, previous_turn: dict) -> str:
    payload = {
        "question": question,
        "previous_turn": previous_turn,
        "detail_requested": evidence.get("detail"),
        "read_only_scores": read_only_scores,
        "factual_skeleton": skeleton,
        "evidence": evidence,
    }
    return "Rewrite factual_skeleton. Do not add numbers that are not in it.\n\n" + json.dumps(payload, ensure_ascii=True)


def request_intent(message: str, previous_turn: str, hub_catalog: str) -> tuple[IntentResult | None, str | None]:
    parsed, note = _request(
        INTENT_SYSTEM,
        intent_user_message(message, previous_turn, hub_catalog),
        "intent_result",
        INTENT_JSON_SCHEMA,
        IntentResult,
    )
    return parsed, note  # type: ignore[return-value]


def request_answer(
    question: str,
    read_only_scores: list,
    evidence: dict,
    skeleton: str,
    previous_turn: dict,
) -> tuple[AnswerDraft | None, str | None]:
    parsed, note = _request(
        ANSWER_SYSTEM,
        answer_user_message(question, read_only_scores, evidence, skeleton, previous_turn),
        "answer_draft",
        ANSWER_JSON_SCHEMA,
        AnswerDraft,
    )
    return parsed, note  # type: ignore[return-value]


def _request(system: str, user: str, schema_name: str, schema: dict, model_cls: type[BaseModel]):
    if not llm_enabled():
        return None, None
    try:
        from openai import OpenAI
    except Exception as exc:
        return None, f"language model client unavailable ({exc.__class__.__name__})"
    client = OpenAI(api_key=openai_api_key(), timeout=45.0, max_retries=1)
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]

    def load_text(error: Exception | None) -> str:
        if error is not None:
            messages.append(
                {
                    "role": "user",
                    "content": f"Schema validation failed: {error}. Return corrected JSON only.",
                }
            )
        try:
            completion = client.chat.completions.create(
                model=openai_model(),
                messages=messages,
                response_format={
                    "type": "json_schema",
                    "json_schema": {"name": schema_name, "strict": True, "schema": schema},
                },
            )
        except Exception as exc:
            raise RuntimeError(exc.__class__.__name__) from exc
        text = completion.choices[0].message.content or ""
        messages.append({"role": "assistant", "content": text})
        return text

    try:
        parsed, _attempts = schema_retries(load_text, model_cls)
    except RuntimeError as exc:
        return None, f"language model request failed ({exc})"
    if parsed is None:
        return None, "schema validation failed twice"
    return parsed, None
