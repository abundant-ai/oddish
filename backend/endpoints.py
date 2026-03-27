from __future__ import annotations

from oddish.config import Settings

# API containers handle concurrent requests; use slightly larger DB pools
# than the single-job worker defaults.
Settings.db_pool_size = 2
Settings.db_pool_max_overflow = 1

import modal

from modal_app import (
    API_BUFFER_CONTAINERS,
    API_CONCURRENCY_MAX,
    API_CONCURRENCY_TARGET,
    API_MAX_CONTAINERS,
    API_MIN_CONTAINERS,
    VOLUME_MOUNT_PATH,
    app,
    image,
    runtime_secrets,
    volume,
)
from api.app import create_app
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

# Create the FastAPI application
api = create_app()

# Register all routers
api.include_router(dashboard.router)
api.include_router(orgs.router)
api.include_router(api_keys.router)
api.include_router(clerk_webhooks.router)
api.include_router(github_webhooks.router)
api.include_router(tasks.router)
api.include_router(trials.router)
api.include_router(public.router)
api.include_router(admin.router)


# =============================================================================
# Modal ASGI App
# =============================================================================


@app.function(
    image=image,
    volumes={VOLUME_MOUNT_PATH: volume},
    secrets=runtime_secrets,
    timeout=600,
    min_containers=API_MIN_CONTAINERS,
    buffer_containers=API_BUFFER_CONTAINERS,
    max_containers=API_MAX_CONTAINERS,
)
@modal.concurrent(
    target_inputs=API_CONCURRENCY_TARGET,
    max_inputs=API_CONCURRENCY_MAX,
)
@modal.asgi_app(label="api")
def api_app():
    """Single ASGI endpoint for all API routes."""
    return api
