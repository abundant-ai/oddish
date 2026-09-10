"""Dedicated ASGI application for long-lived QA model streams."""

from contextlib import asynccontextmanager

from fastapi import FastAPI

from api.request_metrics import BackendPhaseMetricsMiddleware
from api.routers.qa_model import router
from oddish.config import settings
from oddish.db import close_database_connections
from oddish.workers.queue.model_capacity import configured_pools


@asynccontextmanager
async def lifespan(app):
    if settings.qa_model_routing_enabled and not configured_pools():
        raise RuntimeError("QA model routing enabled without a usable provider pool")
    try:
        yield
    finally:
        await close_database_connections()


def create_qa_model_asgi_app():
    app = FastAPI(title="Oddish QA model gateway", lifespan=lifespan)
    app.include_router(router)
    return BackendPhaseMetricsMiddleware(app)
