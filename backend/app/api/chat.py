"""Chat endpoint used by the UI."""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException

from app.agent.orchestrator import handle_chat
from app.agent.schemas import ChatRequest

logger = logging.getLogger(__name__)
router = APIRouter()


@router.post("/chat")
def post_chat(body: ChatRequest) -> dict:
    if not body.message or not body.message.strip():
        raise HTTPException(status_code=400, detail="message is required")
    try:
        return handle_chat(body.session_id, body.message)
    except Exception:
        logger.exception("chat failed")
        raise HTTPException(status_code=500, detail="The agent hit an unexpected error and did not guess a score.")
