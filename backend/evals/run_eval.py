"""Deterministic evals. No model judge.

Usage (from backend/): python evals/run_eval.py
"""

from __future__ import annotations

import json
import os
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="wra-eval-")
    os.environ["DATABASE_PATH"] = str(Path(tmp) / "eval.db")
    os.environ["WEATHER_DATA_MODE"] = "fixture"
    os.environ["WEATHER_RISK_DISABLE_LLM"] = "1"

    from fastapi.testclient import TestClient

    from app.agent.schemas import AnswerResult, IntentResult
    from app.main import create_app

    cases = json.loads((Path(__file__).parent / "eval_set.json").read_text(encoding="utf-8"))
    failures = []
    with TestClient(create_app()) as client:
        for case in cases:
            kind = case.get("kind", "chat")
            if kind == "schema_fallback":
                errors = _schema_case()
            elif kind == "endpoints":
                errors = _endpoint_case(client)
            else:
                errors = _chat_case(client, case, AnswerResult, IntentResult)
            if errors:
                failures.append((case["id"], errors))
                print(f"FAIL  {case['id']}")
                for error in errors:
                    print(f"      {error}")
            else:
                print(f"PASS  {case['id']}")
    print(f"\n{len(cases) - len(failures)} passed, {len(failures)} failed")
    return 1 if failures else 0


def _chat_case(client, case, answer_model, intent_model) -> list[str]:
    session_id = None
    body = None
    for turn in case["turns"]:
        response = client.post("/api/chat", json={"session_id": session_id, "message": turn})
        if response.status_code != 200:
            return [f"status {response.status_code}"]
        body = response.json()
        session_id = body["session_id"]
        try:
            answer_model.model_validate(body["answer"])
            intent_model.model_validate(body["intent"])
        except Exception as exc:
            return [f"schema: {exc}"]
    return _check(case.get("expect") or {}, body)


def _check(expect: dict, body: dict) -> list[str]:
    errors = []
    answer = body["answer"]
    intent = body["intent"]
    text = answer["answer"]
    if "intent" in expect and intent["intent"] != expect["intent"]:
        errors.append(f"intent {intent['intent']} != {expect['intent']}")
    if "needs_clarification" in expect and intent["needs_clarification"] != expect["needs_clarification"]:
        errors.append("clarification flag mismatch")
    if "hub_ids" in expect and intent["hub_ids"] != expect["hub_ids"]:
        errors.append(f"hubs {intent['hub_ids']} != {expect['hub_ids']}")
    if "hazards" in expect and intent["hazards"] != expect["hazards"]:
        errors.append(f"hazards {intent['hazards']} != {expect['hazards']}")
    for needle in expect.get("answer_contains", []):
        if needle.lower() not in text.lower():
            errors.append(f"answer missing {needle}")
    for needle in expect.get("answer_excludes", []):
        if needle.lower() in text.lower():
            errors.append(f"answer unexpectedly mentions {needle}")
    if expect.get("answer_regex") and not re.search(expect["answer_regex"], text):
        errors.append("answer regex failed")
    if expect.get("has_assumptions") and not answer["assumptions"]:
        errors.append("no assumptions")
    if expect.get("has_sources") and not answer["sources"]:
        errors.append("no sources")
    if expect.get("has_uncertainties") and not answer["uncertainties"]:
        errors.append("no uncertainties")
    uncertainty_text = " ".join(answer.get("uncertainties") or []).lower()
    for needle in expect.get("uncertainties_exclude", []):
        if needle.lower() in uncertainty_text:
            errors.append(f"uncertainty mentions {needle}")
    hazards = {item["hazard"] for item in answer["scores"]}
    if expect.get("score_hazard") and expect["score_hazard"] not in hazards:
        errors.append(f"missing score hazard {expect['score_hazard']}")
    for hazard in expect.get("score_hazards", []):
        if hazard not in hazards:
            errors.append(f"missing score hazard {hazard}")
    if expect.get("metric_non_null"):
        metric = (body.get("evidence") or {}).get("metric") or {}
        if metric.get("value") is None:
            errors.append("metric value is null")
    if expect.get("has_drivers"):
        rankings = (body.get("evidence") or {}).get("rankings") or []
        if not rankings or not rankings[0].get("drivers"):
            errors.append("no drivers")
    followups = " ".join(answer.get("follow_up_questions") or []).lower()
    for needle in expect.get("followups_exclude", []):
        if needle.lower() in followups:
            errors.append(f"follow-up repeats {needle}")
    if expect.get("contributors_sum"):
        match = re.search(
            r"Contributors, largest first: (.+?)\. These points sum to (\d+(?:\.\d+)?)",
            text,
        )
        if not match:
            errors.append("missing full contributor list")
        else:
            parts = [float(number) for number in re.findall(r"\((-?\d+(?:\.\d+)?)\)", match.group(1))]
            total = float(match.group(2))
            if not parts or abs(sum(parts) - total) > 0.15:
                errors.append(f"contributors {sum(parts) if parts else 0} != {total}")
    return errors


def _endpoint_case(client) -> list[str]:
    errors = []
    hubs = client.get("/api/hubs")
    if hubs.status_code != 200 or len(hubs.json().get("hubs", [])) != 10:
        errors.append("GET /api/hubs failed")
    risk = client.get("/api/hubs/dallas/risk")
    if risk.status_code != 200 or risk.json().get("overall_score") is None:
        errors.append("GET /api/hubs/dallas/risk failed")
    compared = client.get("/api/compare", params={"hub_ids": "miami,houston", "hazards": "hurricane,flood"})
    if compared.status_code != 200:
        errors.append("GET /api/compare failed")
    else:
        ids = {hub["hub_id"] for hub in compared.json().get("hubs", [])}
        if ids != {"miami", "houston"}:
            errors.append("compare hubs mismatch")
    return errors


def _schema_case() -> list[str]:
    from app.agent.llm import schema_retries
    from app.agent.schemas import IntentResult

    parsed, attempts = schema_retries(lambda _error: "not-json", IntentResult)
    if parsed is not None or attempts != 2:
        return ["schema retry did not fail closed after two attempts"]
    return []


if __name__ == "__main__":
    raise SystemExit(main())
