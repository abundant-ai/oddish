from __future__ import annotations

import modal

from modal_app import (
    API_BUFFER_CONTAINERS,
    API_CONCURRENCY_MAX,
    API_CONCURRENCY_TARGET,
    API_MAX_CONTAINERS,
    API_MIN_CONTAINERS,
    MODEL_CONCURRENCY_DEFAULT,
    VOLUME_MOUNT_PATH,
    app,
    image,
    runtime_secrets,
    volume,
)
from cloud_policy import get_default_cloud_environment
from oddish.config import Settings, settings

def _configure_modal_settings() -> None:
    """Patch Oddish Settings ClassVars for Modal."""
    Settings.local_storage_dir = f"{VOLUME_MOUNT_PATH}/tasks"
    Settings.harbor_jobs_dir = f"{VOLUME_MOUNT_PATH}/harbor"
    Settings.harbor_environment = get_default_cloud_environment().value
    # Workers run separately in Modal (see backend/worker/)
    Settings.auto_start_workers = False
    # Keep API containers cheap in DB terms so request bursts scale with
    # container concurrency before they scale connection usage.
    Settings.db_pool_min_size = 0
    Settings.db_pool_max_size = 3
    Settings.db_pool_size = 2
    Settings.db_pool_max_overflow = 1
    settings.asyncpg_pool_min_size = 0
    settings.asyncpg_pool_max_size = 1
    settings.default_model_concurrency = MODEL_CONCURRENCY_DEFAULT


# Patch settings before importing settings-dependent modules.
_configure_modal_settings()

# Import app factory and routers after settings are patched
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
