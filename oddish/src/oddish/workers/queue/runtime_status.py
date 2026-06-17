"""Persisted heartbeat/status for the out-of-band queue components.

The dispatcher (``poll_queue``) and reconciler (``reconcile_queue_state``)
run in their own scheduled Modal containers and otherwise only emit
``console.print`` lines that vanish into the Modal log stream. That made
them invisible to operators -- you could not tell from the dashboard
whether the last poll spawned anything, or whether the reconcile sweep
deadlocked / timed out.

Each component upserts a single row into ``queue_runtime_status`` keyed by
``component`` with a small JSON ``payload`` summary and ``updated_at``. The
admin dashboard reads these back to render "last poll 12s ago, spawned 41
workers" and "last reconcile reaped 3 zombies, no errors". Writes are
best-effort: a status-write failure must never break the dispatch or
reconcile loop, so callers should wrap calls defensively (and this module
swallows its own errors as a second line of defence).
"""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import text

from oddish.db import get_session
from oddish.workers.queue.shared import console


__all__ = [
    "DISPATCHER_COMPONENT",
    "RECONCILER_COMPONENT",
    "record_queue_runtime_status",
    "get_queue_runtime_statuses",
]


DISPATCHER_COMPONENT = "dispatcher"
RECONCILER_COMPONENT = "reconciler"


async def record_queue_runtime_status(component: str, payload: dict[str, Any]) -> None:
    """Best-effort upsert of one component's latest status row."""
    try:
        encoded = json.dumps(payload, default=str)
    except (TypeError, ValueError):
        encoded = "{}"

    try:
        async with get_session() as session:
            await session.execute(
                text(
                    """
                    INSERT INTO queue_runtime_status (component, updated_at, payload)
                    VALUES (:component, NOW(), CAST(:payload AS JSONB))
                    ON CONFLICT (component) DO UPDATE
                    SET updated_at = NOW(),
                        payload = EXCLUDED.payload
                    """
                ),
                {"component": component, "payload": encoded},
            )
    except Exception as exc:  # noqa: BLE001 - status writes are never critical
        console.print(
            f"[yellow]Failed to record queue runtime status "
            f"({component}): {exc}[/yellow]"
        )


def _coerce_payload(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError):
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


async def get_queue_runtime_statuses(session) -> dict[str, dict[str, Any]]:
    """Read all component status rows as ``{component: {updated_at, payload}}``.

    Returns an empty mapping if the table does not exist yet (pre-migration)
    so the admin endpoint degrades gracefully instead of 500ing.
    """
    try:
        rows = (
            await session.execute(
                text(
                    """
                    SELECT component, updated_at, payload
                    FROM   queue_runtime_status
                    """
                )
            )
        ).all()
    except Exception as exc:  # noqa: BLE001 - tolerate missing table / transient errors
        console.print(f"[yellow]Failed to read queue runtime status: {exc}[/yellow]")
        return {}

    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        result[row.component] = {
            "updated_at": row.updated_at,
            "payload": _coerce_payload(row.payload),
        }
    return result
