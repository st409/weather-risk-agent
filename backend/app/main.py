"""FastAPI application."""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.chat import router as chat_router
from app.api.hubs import router as hubs_router
from app.db.repository import init_db, seed_reference_data
from app.risk.engine import recompute_stale_scores


def create_app() -> FastAPI:
    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        init_db()
        seed_reference_data()
        recompute_stale_scores()
        yield

    app = FastAPI(title="Weather Risk Agent", version="0.1.0", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://127.0.0.1:5173", "http://localhost:5173"],
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(chat_router, prefix="/api")
    app.include_router(hubs_router, prefix="/api")

    @app.get("/")
    def root() -> dict:
        return {
            "service": "weather-risk-agent",
            "ui": "http://127.0.0.1:5173",
            "health": "/api/health",
            "docs": "/docs",
        }

    return app


app = create_app()
