from __future__ import annotations

import modal
from harbor.models.environment_type import EnvironmentType

from modal_app import (
    MODEL_CONCURRENCY_DEFAULT,
    VOLUME_MOUNT_PATH,
    app,
    image,
    volume,
)
from oddish.config import Settings, settings


def _get_default_cloud_environment() -> str:
    return EnvironmentType.MODAL.value


def _configure_modal_settings() -> None:
    """Patch Oddish Settings ClassVars for Modal."""
    Settings.local_storage_dir = f"{VOLUME_MOUNT_PATH}/tasks"
    Settings.harbor_jobs_dir = f"{VOLUME_MOUNT_PATH}/harbor"
    Settings.harbor_environment = _get_default_cloud_environment()
    # Workers run separately in Modal (see backend/worker.py)
    Settings.auto_start_workers = False
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
    secrets=[modal.Secret.from_dotenv()],
    timeout=600,
    min_containers=4,
    max_containers=16,
)
@modal.asgi_app(label="api")
def api_app():
    """Single ASGI endpoint for all API routes."""
    return api
