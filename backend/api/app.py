from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

from oddish.config import settings
from oddish.db import close_database_connections
from oddish.timing import (
    add_server_timing_metric,
    elapsed_ms,
    format_server_timing,
    join_server_timing_headers,
    now,
)

logger = logging.getLogger(__name__)


async def _apply_role_defaults_bg() -> None:
    """Best-effort DB role configuration.

    Runs in the background so a slow pooler or a role without ALTER
    privilege doesn't block the API container's startup. Installs
    `idle_in_transaction_session_timeout` on the connecting role so
    orphaned transactions left by SIGKILLed workers get auto-killed by
    Postgres itself, which is the server-side half of the fix for the
    incidents where zombies held trials locks for hours.
    """
    try:
        from oddish.db.connection import apply_role_defaults

        result = await apply_role_defaults()
        logger.info("applied DB role defaults: %s", result)
    except Exception:
        logger.warning("could not apply DB role defaults", exc_info=True)


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

    role_defaults_task = asyncio.create_task(_apply_role_defaults_bg())

    yield

    role_defaults_task.cancel()
    try:
        await role_defaults_task
    except (asyncio.CancelledError, Exception):
        pass

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

    @api.middleware("http")
    async def add_server_timing_header(request: Request, call_next):
        request.state.server_timing_metrics = []
        started_at = now()
        response = await call_next(request)
        add_server_timing_metric(
            request,
            "backend_total",
            elapsed_ms(started_at),
            "Backend request total",
        )
        header = format_server_timing(request.state.server_timing_metrics)
        combined = join_server_timing_headers(response.headers.get("Server-Timing"), header)
        if combined:
            response.headers["Server-Timing"] = combined
        return response

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
