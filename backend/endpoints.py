from __future__ import annotations

from oddish.config import Settings

# API containers are warm and long-lived (min_containers >= 1).  Reuse pooled
# connections rather than opening a fresh one per request.  pool_pre_ping=True,
# pool_recycle=300, and statement_cache_size=0 are already set in the engine
# for Supavisor transaction-mode compatibility.
#
# Connection budget (worst case):
#   Workers:  256 containers × 1 SQLAlchemy + 1 asyncpg = up to 512
#   API:      16 containers  × pool_size(2) idle         = 32  (at rest)
#             16 containers  × max_overflow(2) burst      = +32 (peak)
#   Total API peak: 64 — well under the NullPool worst-case of 128
#                   (API_CONCURRENCY_MAX=8 × 16 containers with NullPool)
#
# max_overflow=2 gives each container up to 4 simultaneous connections,
# enough for the dashboard parallel gather (primary session + experiments
# session) under 2 concurrent dashboard requests per container.
Settings.db_use_null_pool = False
Settings.db_pool_size = 2
Settings.db_pool_max_overflow = 2

import modal

from modal_app import (
    API_BUFFER_CONTAINERS,
    API_CONCURRENCY_MAX,
    API_CONCURRENCY_TARGET,
    API_MAX_CONTAINERS,
    API_MIN_CONTAINERS,
    API_WEBHOOK_LABEL,
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
@modal.asgi_app(label=API_WEBHOOK_LABEL)
def api_app():
    """Single ASGI endpoint for all API routes."""
    return api
