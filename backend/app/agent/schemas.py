"""JSON contracts for the API and for schema-checked model calls."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

IntentName = Literal[
    "rank_hubs",
    "compare_hubs",
    "metric_lookup",
    "explain_hub",
    "follow_up",
    "alerts",
    "methodology",
    "greeting",
    "off_topic",
    "unsupported_language",
    "unknown",
]
SourceType = Literal["live_api", "cached_api", "static_public_dataset"]


class IntentResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    intent: IntentName
    hub_ids: list[str] = Field(default_factory=list)
    region: str | None = None
    hazards: list[str] = Field(default_factory=list)
    metric: str | None = None
    time_window: str | None = None
    needs_clarification: bool = False
    clarifying_question: str | None = None


class ScoreItem(BaseModel):
    hub_id: str
    hub_name: str
    hazard: str
    score: float
    unit: str = "index"


class SourceItem(BaseModel):
    name: str
    source_type: SourceType
    retrieved_at: str
    uncertainty_note: str


class AnswerDraft(BaseModel):
    """What the model is allowed to return. Scores stay the values Python computed."""

    model_config = ConfigDict(extra="forbid")

    answer: str
    follow_up_questions: list[str] = Field(default_factory=list)


class AnswerResult(BaseModel):
    answer: str
    key_findings: list[str] = Field(default_factory=list)
    scores: list[ScoreItem] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    uncertainties: list[str] = Field(default_factory=list)
    sources: list[SourceItem] = Field(default_factory=list)
    follow_up_questions: list[str] = Field(default_factory=list)
    fallback: bool = False


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=500)
    session_id: str | None = Field(default=None, max_length=64)


INTENT_JSON_SCHEMA: dict = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "intent": {
            "type": "string",
            "enum": ["rank_hubs", "compare_hubs", "metric_lookup", "explain_hub", "follow_up", "unknown"],
        },
        "hub_ids": {"type": "array", "items": {"type": "string"}},
        "region": {"type": ["string", "null"]},
        "hazards": {"type": "array", "items": {"type": "string"}},
        "metric": {"type": ["string", "null"]},
        "time_window": {"type": ["string", "null"]},
        "needs_clarification": {"type": "boolean"},
        "clarifying_question": {"type": ["string", "null"]},
    },
    "required": [
        "intent",
        "hub_ids",
        "region",
        "hazards",
        "metric",
        "time_window",
        "needs_clarification",
        "clarifying_question",
    ],
}

ANSWER_JSON_SCHEMA: dict = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "answer": {"type": "string"},
        "follow_up_questions": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["answer", "follow_up_questions"],
}
