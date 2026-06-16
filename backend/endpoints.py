from __future__ import annotations

from oddish.config import Settings

# API containers are warm and long-lived (min_containers >= 1).  Reuse pooled
# connections rather than opening a fresh one per request.  pool_pre_ping=True,
# pool_recycle=300, and statement_cache_size=0 are already set in the engine
# for Supavisor transaction-mode compatibility.
#
# Connection budget is bounded by Supabase's two limits: the pooler's max
# *client* connections (1500 on the 2XL tier) and the transaction-mode *pool
# size* — the real Postgres backends behind it (100, within the 380
# max_connections). pool_size + max_overflow is sized to API_CONCURRENCY_MAX so
# a fully-loaded container never has requests blocking on SQLAlchemy pool
# checkout (the prior 4-conn pool vs 8 inputs caused checkout waits that looked
# like DB latency under load).
#
# Client-connection budget (worst case):
#   API:     64 containers × (pool_size 2 + max_overflow 1) = up to 192
#   Workers: WORKER_MAX_CONTAINERS(512) × ~2 (1 SQLAlchemy + 1 asyncpg) ≈ 1024
#   Total ≈ 1216 — ~81% of the 1500 client cap. Concurrent *execution* is gated
#   by the 100-backend transaction pool, not these client counts.
#
# API_CONCURRENCY_MAX was lowered 8->3 (with API_MAX_CONTAINERS raised 24->64)
# to shrink the OOM blast radius; the pool is resized to match so the budget
# above is unchanged (64 × 3 == 24 × 8 == 192 API client connections).
Settings.db_use_null_pool = False
Settings.db_pool_size = 2
Settings.db_pool_max_overflow = 1

import modal

from modal_app import (
    API_BUFFER_CONTAINERS,
    API_CONCURRENCY_MAX,
    API_CONCURRENCY_TARGET,
    API_CPU,
    API_MAX_CONTAINERS,
    API_MEMORY_MB,
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
    cpu=API_CPU,
    memory=API_MEMORY_MB,
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
