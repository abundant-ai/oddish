from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from oddish.config import settings
from oddish.db import close_database_connections


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
    """Prepare lightweight API container resources.

    Hosted environments should rely on Alembic migrations, not runtime
    `metadata.create_all()`. Avoiding a startup-time DB handshake keeps the
    ASGI app from hard-failing when the Supabase pooler is briefly unavailable.
    """
    Path(settings.harbor_jobs_dir).mkdir(parents=True, exist_ok=True)

    yield

    try:
        await close_database_connections()
    except Exception:
        pass


def create_app() -> FastAPI:
    """Create and configure the FastAPI application with all routers."""
    api = FastAPI(
        title="Oddish Cloud",
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

    from api.routers import (
        admin,
        api_keys,
        clerk_webhooks,
        dashboard,
        github_webhooks,
        orgs,
        public,
        tasks,
        trials,
    )

    api.include_router(dashboard.router)
    api.include_router(orgs.router)
    api.include_router(api_keys.router)
    api.include_router(clerk_webhooks.router)
    api.include_router(github_webhooks.router)
    api.include_router(tasks.router)
    api.include_router(trials.router)
    api.include_router(public.router)
    api.include_router(admin.router)

    return api
