from __future__ import annotations

from oddish.config import Settings

# API containers handle many concurrent requests in a warm Modal container.
# Keep a conservative pool cap so bursts do not fan out into too many database
# connections across containers. Prepared statement caching is already disabled
# in the engine config for pooler compatibility.
Settings.db_use_null_pool = False
Settings.db_pool_size = 2
Settings.db_pool_max_overflow = 0

import modal

from modal_app import (
    API_BUFFER_CONTAINERS,
    API_CONCURRENCY_MAX,
    API_CONCURRENCY_TARGET,
    API_MAX_CONTAINERS,
    API_MIN_CONTAINERS,
    api_volumes,
    app,
    image,
    runtime_secrets,
)
from api.app import create_app

api = create_app()


@app.function(
    image=image,
    volumes=api_volumes,
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
