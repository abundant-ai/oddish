from __future__ import annotations

from oddish.config import Settings

# API containers handle many concurrent requests in a warm Modal container.
# Use fresh DB connections per request here so stale pooled connections do not
# get reused across requests and tiny QueuePool settings do not become a
# bottleneck for auth/dashboard traffic.
Settings.db_use_null_pool = True

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
