"""Hub, score, and comparison endpoints. These do not go through the chat model."""

from __future__ import annotations

import hmac

from fastapi import APIRouter, Header, HTTPException

from app.config import admin_token, data_mode, llm_disabled, openai_api_key
from app.db import repository
from app.risk.engine import public_report, score_selection
from app.risk.hubs import HUB_BY_ID, hub_names
from app.risk.scoring import HAZARD_ORDER

router = APIRouter()


@router.get("/health")
def health() -> dict:
    return {
        "status": "ok",
        "data_mode": data_mode(),
        "llm_enabled": bool(openai_api_key()) and not llm_disabled(),
        "hub_count": len(HUB_BY_ID),
    }


@router.get("/hubs")
def get_hubs() -> dict:
    rows = repository.list_hubs()
    return {
        "hubs": [
            {
                "hub_id": row["hub_id"],
                "name": row["name"],
                "city": row["city"],
                "state": row["state"],
                "region": row["region"],
                "lat": row["lat"],
                "lon": row["lon"],
                "county": row["county"],
                "fips": row["fips"],
                "surge_exposed": bool(row["surge_exposed"]),
                "business_importance_weight": row["business_importance_weight"],
            }
            for row in rows
        ]
    }


@router.get("/hubs/{hub_id}/risk")
def get_hub_risk(hub_id: str) -> dict:
    if hub_id not in HUB_BY_ID:
        raise HTTPException(status_code=404, detail=f"Unknown hub '{hub_id}'. Known hubs: {hub_names()}.")
    selection = score_selection([hub_id], None, None)
    if not selection.reports:
        raise HTTPException(status_code=503, detail="Live weather for this hub is unavailable right now. No score was guessed.")
    payload = public_report(selection.reports[0])
    repository.save_score_snapshot(payload)
    return payload


@router.get("/compare")
def compare(hub_ids: str, hazards: str | None = None) -> dict:
    ids = [part.strip().lower() for part in hub_ids.split(",") if part.strip()]
    unknown = [hub_id for hub_id in ids if hub_id not in HUB_BY_ID]
    if unknown or len(ids) < 2:
        raise HTTPException(
            status_code=400,
            detail=f"Pass at least two known hub ids. Unknown: {unknown or 'none'}. Known hubs: {hub_names()}.",
        )
    hazard_list = [part.strip().lower() for part in (hazards or "").split(",") if part.strip()]
    bad = [hazard for hazard in hazard_list if hazard not in HAZARD_ORDER]
    if bad:
        raise HTTPException(status_code=400, detail=f"Unknown hazards: {bad}.")
    selection = score_selection(ids, None, None)
    # With hazards, hubs are ranked on the mean of just those hazards. Without, on the overall score.
    ranked = sorted(selection.reports, key=lambda report: report.focus_score(hazard_list), reverse=True)
    return {
        "hub_ids": ids,
        "hazards": hazard_list,
        "window_key": selection.window_key,
        "window_label": selection.window_label,
        "unavailable_hubs": selection.unavailable,
        "hubs": [{**public_report(report), "focus_score": round(report.focus_score(hazard_list), 2)} for report in ranked],
    }


@router.post("/admin/refresh-scores")
def refresh_scores(x_admin_token: str | None = Header(default=None)) -> dict:
    expected = admin_token()
    if not expected:
        raise HTTPException(status_code=404, detail="Not Found")
    if not x_admin_token or not hmac.compare_digest(x_admin_token.encode(), expected.encode()):
        raise HTTPException(status_code=403, detail="Invalid admin token.")
    selection = score_selection(None, None, None, force=True)
    payloads = []
    for report in selection.reports:
        payload = public_report(report)
        repository.save_score_snapshot(payload)
        payloads.append(
            {
                "hub_id": payload["hub_id"],
                "name": payload["name"],
                "overall_score": payload["overall_score"],
                "weather_fallback": payload["weather_fallback"],
            }
        )
    return {
        "window_key": selection.window_key,
        "window_label": selection.window_label,
        "refreshed": len(payloads),
        "unavailable_hubs": selection.unavailable,
        "scores": payloads,
    }
