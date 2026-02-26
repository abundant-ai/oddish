from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from oddish.config import settings
from oddish.db import close_pool, get_pool, init_db


def _get_cors_origins() -> list[str]:
    """
    Get allowed CORS origins from environment.

    Set CORS_ALLOWED_ORIGINS as comma-separated list:
      CORS_ALLOWED_ORIGINS=https://app.example.com,https://staging.example.com

    Defaults to localhost origins for development.
    """
    env_origins = os.getenv("CORS_ALLOWED_ORIGINS", "")
    if env_origins:
        return [origin.strip() for origin in env_origins.split(",") if origin.strip()]

    # Default: localhost for development
    return [
        "http://localhost:3000",
        "http://127.0.0.1:3000",
    ]


@asynccontextmanager
async def lifespan(_api: FastAPI):
    """Initialize DB + pool in Modal web containers."""
    Path(settings.harbor_jobs_dir).mkdir(parents=True, exist_ok=True)
    Path(settings.local_storage_dir).mkdir(parents=True, exist_ok=True)

    await init_db()
    try:
        await get_pool()
    except Exception:
        # Don't block API startup if pool warmup fails.
        pass

    yield

    try:
        await close_pool()
    except Exception:
        pass


def create_app() -> FastAPI:
    """Create and configure the FastAPI application."""
    api = FastAPI(
        title="Oddish Cloud",
        description="Multi-tenant evaluation platform for Harbor tasks on Modal.",
        version="0.3.0",
        lifespan=lifespan,
    )

    cors_origins = _get_cors_origins()
    api.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @api.get("/health")
    async def health_check():
        """Simple health check endpoint (no auth required)."""
        return {"status": "healthy"}

    return api
