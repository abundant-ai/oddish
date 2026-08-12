from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from starlette.types import ASGIApp

from observability import (
    instrument_fastapi,
    span as _otel_span,
)
from oddish.config import settings
from oddish.db import close_database_connections

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


async def _assert_quota_schema_or_force_off() -> None:
    from sqlalchemy import text

    from oddish.config import QuotaMode
    from oddish.db import get_session

    if settings.quota_mode == QuotaMode.OFF:
        return

    # Named so a deploy-before-migrate failure can say exactly what is absent.
    # oddish/ and backend/ migrate independently, so this window is real.
    schema_objects = (
        (
            "trials.billed_user_id",
            """
            SELECT EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name = 'trials'
                  AND column_name = 'billed_user_id'
            )
            """,
        ),
        (
            "quotas",
            """
            SELECT EXISTS (
                SELECT 1 FROM information_schema.tables
                WHERE table_name = 'quotas'
            )
            """,
        ),
        (
            "quota_bumps",
            """
            SELECT EXISTS (
                SELECT 1 FROM information_schema.tables
                WHERE table_name = 'quota_bumps'
            )
            """,
        ),
        (
            "org_quotas",
            """
            SELECT EXISTS (
                SELECT 1 FROM information_schema.tables
                WHERE table_name = 'org_quotas'
            )
            """,
        ),
    )

    missing: list[str] = []
    try:
        async with get_session() as session:
            for name, sql in schema_objects:
                if not await session.scalar(text(sql)):
                    missing.append(name)
    except Exception:
        logger.warning(
            "quota schema check skipped (DB unavailable at startup); "
            "quota_mode=%s left as-is",
            settings.quota_mode,
        )
        return

    if not missing:
        return

    missing_csv = ", ".join(missing)

    # Unmetered ENFORCE is worse than a down API. Fail closed unless the
    # operator opts into the old force-off degrade.
    if settings.quota_mode == QuotaMode.ENFORCE and not (
        settings.allow_quota_schema_degrade
    ):
        raise RuntimeError(
            "quota schema is incomplete "
            f"(missing: {missing_csv}); run the oddish and backend alembic "
            "migrations, or set ODDISH_ALLOW_QUOTA_SCHEMA_DEGRADE=1 to start "
            "with quota_mode=off"
        )

    logger.error(
        "metric=quota.schema_incomplete quota_mode=%s missing=%s "
        "forcing quota_mode=off; unmetered billing is disabled for this "
        "process (oddish and backend alembic trees migrate separately)",
        settings.quota_mode,
        missing_csv,
    )
    settings.quota_mode = QuotaMode.OFF


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
        await _assert_quota_schema_or_force_off()
        role_defaults_task = asyncio.create_task(_apply_role_defaults_bg())

        # Route the dashboard's whole-``trials``-table queue/pipeline slice
        # through a shared Modal Dict so a cold container reads a warm entry
        # instead of re-running the multi-second scan. Best-effort: falls back
        # to the process-local cache if the Modal Dict can't be reached.
        try:
            from dashboard_cache import install_modal_dashboard_cache

            install_modal_dashboard_cache()
        except Exception:
            logger.warning("dashboard shared cache setup skipped", exc_info=True)

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
        expose_headers=[
            "Server-Timing",
            "Oddish-Submit-Concurrency",
            "RateLimit",
            "RateLimit-Policy",
        ],
    )

    api.add_middleware(GZipMiddleware, minimum_size=500, compresslevel=1)

    from api.capacity_headers import capacity_header_middleware

    api.middleware("http")(capacity_header_middleware)

    from api.routers import (
        admin,
        api_keys,
        byok,
        clerk_webhooks,
        cost_excluded_keys,
        dashboard,
        documents,
        github_linkage,
        github_webhooks,
        imports,
        load,
        model_display_names,
        notifications,
        orgs,
        skills,
        public,
        slack,
        tags,
        tasks,
        trials,
    )

    api.include_router(dashboard.router)
    api.include_router(orgs.router)
    api.include_router(notifications.router)
    api.include_router(api_keys.router)
    api.include_router(byok.router)
    api.include_router(clerk_webhooks.router)
    api.include_router(github_linkage.router)
    api.include_router(github_webhooks.router)
    api.include_router(tasks.router)
    api.include_router(trials.router)
    api.include_router(imports.router)
    api.include_router(load.router)
    api.include_router(skills.router)
    api.include_router(documents.router)
    api.include_router(public.router)
    api.include_router(slack.router)
    api.include_router(admin.router)
    api.include_router(cost_excluded_keys.router)
    api.include_router(model_display_names.router)
    api.include_router(tags.router)

    return api


def create_asgi_app() -> ASGIApp:
    """Wrap the complete FastAPI stack, including its error middleware."""
    from api.request_metrics import BackendPhaseMetricsMiddleware

    return BackendPhaseMetricsMiddleware(create_app())
