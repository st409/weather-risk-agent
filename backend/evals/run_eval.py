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
    if expect.get("answer_regex") and not re.search(expect["answer_regex"], text, re.S):
        errors.append("answer regex failed")
    evidence = body.get("evidence") or {}
    if expect.get("no_internal_labels"):
        spoken = text.split("Contributors, largest first", 1)[0].lower()
        for label in (
            "freeze-day rate",
            "snow-day rate",
            "high-wind rate",
            "heavy-rain rate",
            "fema winter baseline",
            "fema flood baseline",
            "fema hurricane baseline",
            "severe-storm baseline",
            "coastal surge flag",
        ):
            if label in spoken:
                errors.append(f"internal label {label}")
    if expect.get("names_top_hubs"):
        opening = _opening_sentence(text)
        rows = (evidence.get("rankings") or [])[:3]
        named = [row for row in rows if str(row.get("name") or "") and str(row["name"]) in opening]
        needed = min(2, len(rows))
        if len(named) < needed:
            errors.append("opening does not name the top hubs")
    if expect.get("opening_has_no_digits"):
        opening = _opening_sentence(text)
        if re.search(r"\d", opening):
            errors.append(f"opening sentence has a digit: {opening}")
    if expect.get("one_sources_line"):
        if len(re.findall(r"(?i)\bSources\s*:", text)) != 1 or re.search(r"(?i)\bSource\s*:", text):
            errors.append("expected exactly one Sources line")
    if expect.get("no_fallback"):
        visible = text + " " + " ".join(answer.get("uncertainties") or []) + " " + " ".join(answer.get("assumptions") or [])
        if "did not return a valid briefing" in visible.lower():
            errors.append("answer fell back to the skeleton")
    if expect.get("scores_are_final"):
        pieces = re.split(r"Scores:", text, maxsplit=1)
        if len(pieces) != 2 or text.lower().count("scores:") != 1:
            errors.append("scores line must appear once")
        else:
            line = re.split(r"Sources:|Contributors,", pieces[1], maxsplit=1)[0]
            if not re.search(r"\d", line):
                errors.append("empty scores line")
            for number in re.findall(r"\d+\.\d+", line):
                if number in pieces[0]:
                    errors.append(f"score {number} appears before the scores line")
            if expect.get("scores_match_prose"):
                score_body = re.sub(r"\(higher = more exposed\)", "", line, flags=re.I)
                for piece in score_body.split(","):
                    named = re.search(r"(.+?)\s+\d", piece)
                    if named and named.group(1).strip() not in pieces[0]:
                        errors.append(f"scores lists {named.group(1).strip()} but the prose does not")
    if expect.get("scores_last"):
        match = re.search(r"Scores:.*?(?=\s+Sources:|\s+Based on|\s+Contributors,|$)", text, re.S)
        if match is None:
            errors.append("missing scores line")
        else:
            after = text[match.end() :].strip()
            if after and not after.startswith(("Sources:", "Based on")) and "Contributors, largest first" not in after:
                errors.append("text after the scores line")
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
    for needle in expect.get("followups_contains", []):
        if needle.lower() not in followups:
            errors.append(f"follow-up missing {needle}")
    if expect.get("has_breakdown") and (not evidence.get("breakdown") or not evidence.get("details")):
        errors.append("missing breakdown/details")
    breakdown = evidence.get("breakdown") or ""
    for needle in expect.get("breakdown_contains", []):
        if needle.lower() not in breakdown.lower():
            errors.append(f"breakdown missing {needle}")
    for needle in expect.get("breakdown_excludes", []):
        if needle.lower() in breakdown.lower():
            errors.append(f"breakdown unexpectedly mentions {needle}")
    if expect.get("contributors_sum"):
        blob = text if "These points sum to" in text else breakdown
        errors.extend(_contributor_errors(blob))
    if expect.get("scores_are_renormalized"):
        errors.extend(_renormalized_score_errors(answer, expect.get("excluded_hazards") or []))
    driver_line = re.search(r"(?:The main drivers are|Mainly|mainly) ([^.]+)\.", text, re.I)
    for needle in expect.get("driver_line_excludes", []):
        if driver_line is None:
            errors.append("missing main drivers")
            break
        if needle.lower() in driver_line.group(1).lower():
            errors.append(f"driver line names {needle}")
    if expect.get("parenthetical_are_contributions") or expect.get("mainly_clause_is_contributions"):
        errors.extend(_mainly_clause_errors(text, evidence.get("breakdown") or ""))
    if expect.get("lower_hubs_use_own_drivers"):
        errors.extend(_own_driver_errors(text, evidence.get("breakdown") or ""))
    if expect.get("gap_matches_breakdown"):
        errors.extend(_gap_match_errors(text, evidence.get("breakdown") or ""))
    if expect.get("answer_excludes_regex") and re.search(expect["answer_excludes_regex"], text):
        errors.append("answer matched a forbidden pattern")
    rankings = (body.get("evidence") or {}).get("rankings") or []
    if expect.get("rankings_count") is not None and len(rankings) != expect["rankings_count"]:
        errors.append(f"rankings {len(rankings)} != {expect['rankings_count']}")
    if expect.get("hub_count") is not None and len(intent.get("hub_ids") or []) != expect["hub_count"]:
        errors.append(f"hub count {len(intent.get('hub_ids') or [])} != {expect['hub_count']}")
    return errors


def _opening_sentence(text: str) -> str:
    body = text.strip()
    notice = "Only calendar year 2025 and trailing 365 days are supported, so this is for calendar year 2025."
    if body.startswith(notice):
        body = body[len(notice) :].strip()
    return re.split(r"(?<=[.!?])\s+", body, maxsplit=1)[0]


def _gap_match_errors(text: str, breakdown: str) -> list[str]:
    match = re.search(r"contributes (\d+(?:\.\d+)?) vs (\d+(?:\.\d+)?)", text)
    if match is None:
        return ["missing contribution gap"]
    arrows = [float(number) for number in re.findall(r"→\s*(-?\d+(?:\.\d+)?)", breakdown)]
    errors = []
    for raw in match.groups():
        value = float(raw)
        if not any(abs(value - arrow) <= 0.05 + 1e-9 for arrow in arrows):
            errors.append(f"gap {raw} is not a breakdown contribution")
    return errors


def _contributor_errors(blob: str) -> list[str]:
    if "other factors" in blob.lower():
        return ["other factors shown"]
    matches = re.findall(
        r"Contributors, largest first: (.+?)\. These points sum to (\d+(?:\.\d+)?)",
        blob,
    )
    if not matches:
        return ["missing full contributor list"]
    errors = []
    for chunk, total_text in matches:
        parts = [float(number) for number in re.findall(r"→\s*(-?\d+(?:\.\d+)?)", chunk)]
        total = float(total_text)
        if not parts or abs(sum(parts) - total) > 0.05:
            errors.append(f"contributors {sum(parts) if parts else 0} != {total}")
    return errors


def _renormalized_score_errors(answer: dict, excluded: list[str]) -> list[str]:
    grouped: dict[str, list[dict]] = {}
    for item in answer.get("scores") or []:
        grouped.setdefault(item["hub_id"], []).append(item)
    if not grouped:
        return ["no scores"]
    errors = []
    dropped = set(excluded)
    for hub, items in grouped.items():
        overall = next((item["score"] for item in items if item["hazard"] == "overall"), None)
        parts = [item for item in items if item["hazard"] != "overall"]
        if overall is None:
            errors.append(f"{hub} missing adjusted overall")
            continue
        if any(item["hazard"] in dropped for item in parts):
            errors.append(f"{hub} still scores an excluded hazard")
        if abs(sum(item["score"] for item in parts) - overall) > 0.05:
            errors.append(f"{hub} renormalized parts {sum(item['score'] for item in parts)} != {overall}")
    return errors


def _mainly_clause_errors(text: str, breakdown: str) -> list[str]:
    clauses = list(
        re.finditer(
            r"mainly\s+.+?\(([-+]?\d+(?:\.\d+)?)\)\s+and\s+.+?\(([-+]?\d+(?:\.\d+)?)\)\s+of\s+(\d+(?:\.\d+)?)",
            text,
            re.I,
        )
    )
    if not clauses:
        return ["missing mainly contribution clause"]
    arrows = [float(number) for number in re.findall(r"→\s*(-?\d+(?:\.\d+)?)", breakdown)]
    errors = []
    for match in clauses:
        left, right, total = (float(match.group(index)) for index in (1, 2, 3))
        if left - total > 0.05 or right - total > 0.05 or left + right - total > 0.05:
            errors.append(f"contributor points {left} and {right} exceed {total}")
        for value in (left, right):
            if arrows and not any(abs(value - arrow) <= 0.05 + 1e-9 for arrow in arrows):
                errors.append(f"{value} is not a breakdown contribution")
    return errors


def _own_driver_errors(text: str, breakdown: str) -> list[str]:
    matches = list(
        re.finditer(
            r"([A-Z][^.]{0,80}?) is (?:lower at \d+(?:\.\d+)?, mainly on|close behind, driven by) ([^.]+)\.",
            text,
        )
    )
    if len(matches) < 2:
        return ["missing own-driver lines for later hubs"]
    errors = []
    for match in matches:
        subject = match.group(1)
        named = [part.strip().lower() for part in re.split(r"\band\b|,", match.group(2)) if part.strip()]
        chunk = None
        for hub_match in re.finditer(
            r"(?:^|\. )([A-Za-z][^:]{0,40}): .*?Contributors, largest first: (.+?)\. These points sum",
            breakdown,
        ):
            hub_name = hub_match.group(1).strip()
            if hub_name.lower() in subject.lower():
                chunk = hub_match.group(2)
                break
        if chunk is None:
            errors.append(f"no contributor list for {subject}")
            continue
        labels = []
        for piece in re.split(r",\s*(?:and\s+)?|\s+and\s+", chunk):
            label = re.split(r"\s+\d", piece.strip())[0].strip().lower()
            if label:
                labels.append(label)
        if labels[: len(named)] != named:
            errors.append(f"{subject} says {named} but top contributors are {labels[:len(named)]}")
    return errors


def _parenthetical_contribution_errors(text: str, breakdown: str, rankings: list[dict]) -> list[str]:
    numbers = [float(number) for number in re.findall(r"\((\d+(?:\.\d+)?)\)", text)]
    if len(numbers) < 2:
        return ["missing contributor points"]
    arrows = [float(number) for number in re.findall(r"→\s*(-?\d+(?:\.\d+)?)", breakdown)]
    hazard_scores = []
    for row in rankings:
        hazard_scores.extend(float(value) for value in (row.get("hazard_scores") or {}).values())
    errors = []
    for number in numbers[:2]:
        if not any(abs(number - arrow) <= 0.05 + 1e-9 for arrow in arrows):
            errors.append(f"{number} is not a contribution")
        matches_score = any(abs(number - score) <= 0.1 + 1e-9 for score in hazard_scores)
        matches_points = any(abs(number - arrow) <= 0.05 + 1e-9 for arrow in arrows)
        if matches_score and not matches_points:
            errors.append(f"{number} cites a hazard score")
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
