from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

from observability import (
    instrument_fastapi,
    span as _otel_span,
)
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

    Wrapped in an ``app.startup.role_defaults`` span so the ALTER ROLE
    + pool-warmup queries (SELECT current_user, BEGIN, COMMIT) it
    triggers don't appear as orphaned spans on container cold start.
    """
    with _otel_span("app.startup.role_defaults"):
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

    Startup + shutdown work is wrapped in named spans so the file-system
    + DB activity they fan out to (``mkdir``, ``ALTER ROLE``, pool warmup,
    connection close) don't appear as orphan spans on container cold start
    / cycle.
    """
    with _otel_span("app.startup"):
        Path(settings.harbor_jobs_dir).mkdir(parents=True, exist_ok=True)
        role_defaults_task = asyncio.create_task(_apply_role_defaults_bg())

        # cc_chat orchestrator (chat feature). Guarded: if Daytona/Anthropic
        # secrets are absent (some envs), skip construction — the chat routes
        # return 503 via their _orch() guard.
        _api.state.chat_orchestrator = None
        try:
            _daytona_key = os.environ.get("DAYTONA_API_KEY")
            _anthropic_key = settings.anthropic_api_key
            if _daytona_key and _anthropic_key:
                from api.services.cc_chat.daytona_client import RealDaytonaClient
                from api.services.cc_chat.claude_code_runtime import ClaudeCodeRuntime
                from api.services.cc_chat.transcript_buffer import SessionTranscriptBuffer
                from api.services.cc_chat.orchestrator import ChatOrchestrator
                from api.services.cc_chat.restart_sweep import sweep_orphan_chat_sessions
                from oddish.config import api_base_url_for_modal_app
                from oddish.db import get_session
                from oddish.db.storage import get_storage_client

                _daytona = RealDaytonaClient(
                    api_key=_daytona_key,
                    snapshot=settings.cc_chat_daytona_snapshot or None,
                )
                # Explicit override wins; otherwise derive from the Modal app
                # identity so prod and PR previews resolve automatically.
                _chat_api_base_url = (
                    settings.public_api_base_url or api_base_url_for_modal_app()
                )
                _api.state.chat_orchestrator = ChatOrchestrator(
                    daytona=_daytona,
                    runtime=ClaudeCodeRuntime(),
                    transcript_buffer=SessionTranscriptBuffer(),
                    anthropic_api_key=_anthropic_key,
                    chat_auto_stop_minutes=settings.daytona_auto_stop_interval_mins,
                    chat_auto_delete_minutes=settings.daytona_auto_delete_interval_mins,
                    public_api_base_url=_chat_api_base_url,
                    blob_store=get_storage_client(),
                )
                await sweep_orphan_chat_sessions(
                    daytona=_daytona, db_session_factory=lambda: get_session()
                )
            else:
                logger.warning(
                    "cc_chat orchestrator not constructed: missing DAYTONA_API_KEY or ANTHROPIC_API_KEY"
                )
        except Exception:
            _api.state.chat_orchestrator = None
            logger.exception("cc_chat orchestrator construction failed")

    yield

    with _otel_span("app.shutdown"):
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
    """Create and configure the FastAPI application with all routers.

    ``configure_logfire()`` ran in ``api/__init__.py`` before any of
    our handler modules were imported, which is what lets
    ``logfire.install_auto_tracing`` actually patch ``api.routers`` /
    ``oddish.core`` / ``oddish.queue`` / ``oddish.workers``. Calling
    it again here would be a no-op (it's idempotent) but we leave
    it out for clarity.
    """
    api = FastAPI(
        title="Oddish Cloud",
        version="0.3.0",
        lifespan=lifespan,
    )

    instrument_fastapi(api)

    cors_origins = _get_cors_origins()
    api.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["Server-Timing"],
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
        combined = join_server_timing_headers(
            response.headers.get("Server-Timing"), header
        )
        if combined:
            response.headers["Server-Timing"] = combined
        return response

    from api.routers import (
        admin,
        api_keys,
        cc_chat,
        clerk_webhooks,
        dashboard,
        documents,
        github_webhooks,
        imports,
        orgs,
        probe_presets,
        skills,
        public,
        tags,
        tasks,
        trials,
    )

    api.include_router(cc_chat.router)
    api.include_router(dashboard.router)
    api.include_router(orgs.router)
    api.include_router(api_keys.router)
    api.include_router(clerk_webhooks.router)
    api.include_router(github_webhooks.router)
    api.include_router(tasks.router)
    api.include_router(trials.router)
    api.include_router(imports.router)
    api.include_router(probe_presets.router)
    api.include_router(skills.router)
    api.include_router(documents.router)
    api.include_router(public.router)
    api.include_router(admin.router)
    api.include_router(tags.router)

    return api
