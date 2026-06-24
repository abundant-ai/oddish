"""Request idempotency for task-sweep submission.

A retried ``POST /tasks/sweep`` must not create a second set of trials. The
client stamps each submission with an ``Idempotency-Key`` header derived from a
stable digest of the request body; the server records the key, fingerprints the
request, and replays the stored response (or rejects a conflicting reuse) on
retry.

This module is intentionally framework- and storage-agnostic: the concrete
record store lives in the cloud backend (``backend/``) and is injected into the
sweep core, so the open-source ``oddish`` package never imports backend code.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Protocol, runtime_checkable

# Logical route name recorded alongside the key so the same key may be reused on
# unrelated endpoints in the future without colliding.
SWEEP_ROUTE = "POST /tasks/sweep"

# How long a recorded key is honoured. After this window the key is pruned and a
# fresh submission with the same key runs again (matches Stripe's 24h rule).
IDEMPOTENCY_TTL = timedelta(hours=24)

# Recorded lifecycle states. Stored as plain text (with a CHECK constraint)
# rather than a Postgres enum to keep the migration cleanly reversible.
STATUS_IN_PROGRESS = "in_progress"
STATUS_COMPLETED = "completed"

# Bounded retries when the insert/prune race with a concurrent retry. Each loss
# re-reads the winning row instead of spinning forever or creating a duplicate.
_RESERVE_MAX_ATTEMPTS = 5


def _canonical_digest(value: Any) -> str:
    """Return a stable SHA-256 hex digest of *value*.

    Keys are sorted and separators fixed so logically identical payloads hash
    identically regardless of dict ordering or whitespace.
    """
    blob = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode(
        "utf-8"
    )
    return hashlib.sha256(blob).hexdigest()


def compute_sweep_idempotency_key(payload: Mapping[str, Any]) -> str:
    if "registry_auth" in payload:
        payload = {k: v for k, v in payload.items() if k != "registry_auth"}
    return _canonical_digest(payload)


def hash_idempotency_key(raw_key: str) -> str:
    return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()


def compute_request_hash(submission: Any) -> str:
    payload = submission.model_dump(mode="json")
    payload.pop("registry_auth", None)
    return _canonical_digest(payload)


@dataclass(frozen=True)
class StoredIdempotencyRecord:
    """A previously recorded submission, as seen by the dedup policy."""

    request_hash: str
    status: str
    response_json: dict | None
    expires_at: datetime


class IdempotencyConflict(Exception):
    """A key cannot be honoured: still in progress, or reused with a new body.

    The sweep core maps this to an HTTP 409 response.
    """


class IdempotencyReplay(Exception):
    """A completed key was retried; carries the stored response to return."""

    def __init__(self, response_json: dict) -> None:
        super().__init__("idempotent replay")
        self.response_json = response_json


@runtime_checkable
class IdempotencyStore(Protocol):
    """Persistence primitives for the dedup policy.

    Implemented by the backend against its ``submission_idempotency`` table and
    injected into :func:`reserve_idempotency_slot`. All methods operate on the
    caller's transaction.
    """

    async def get(
        self, org_id: str, route: str, key_hash: str
    ) -> StoredIdempotencyRecord | None: ...

    async def begin(
        self,
        org_id: str,
        route: str,
        key_hash: str,
        request_hash: str,
        now: datetime,
        expires_at: datetime,
    ) -> bool:
        """Atomically insert an ``in_progress`` row.

        ``created_at`` is anchored to ``now`` (the same instant ``expires_at``
        is derived from) so the TTL invariant holds exactly. Returns ``True`` if
        this caller won the slot, ``False`` if a row for
        ``(org_id, route, key_hash)`` already exists (unique-insert-wins).
        """
        ...

    async def complete(
        self, org_id: str, route: str, key_hash: str, response_json: dict
    ) -> None:
        """Mark the slot completed and store its response for replay."""
        ...

    async def discard(
        self, org_id: str, route: str, key_hash: str, now: datetime
    ) -> None:
        """Hard-delete the row only if it is expired (``expires_at <= now``).

        Scoping the delete to the stale row keeps a concurrent retry from
        removing a fresh row it just inserted for the same key.
        """
        ...


@dataclass(frozen=True)
class Reservation:
    """Result of winning an idempotency slot; the caller must complete it."""

    key_hash: str


async def reserve_idempotency_slot(
    store: IdempotencyStore,
    *,
    org_id: str,
    route: str,
    raw_key: str,
    request_hash: str,
    now: datetime,
) -> Reservation:
    """Reserve the slot for ``raw_key`` or raise to short-circuit the request.

    Outcomes:

    * fresh key (or expired and pruned) -> insert ``in_progress`` and return a
      :class:`Reservation`; the caller does the work and calls
      :meth:`IdempotencyStore.complete`.
    * completed key, same request -> :class:`IdempotencyReplay` (no re-execution).
    * completed/in-progress key, different request -> :class:`IdempotencyConflict`.
    * another request still in progress for the same body -> :class:`IdempotencyConflict`.

    Concurrency-safe: the insert is unique-insert-wins, and pruning an expired
    record deletes only that stale row -- never a fresh row a concurrent retry
    just inserted. After losing the insert race we re-read the winning row and
    replay / conflict against it rather than creating a duplicate.
    """
    key_hash = hash_idempotency_key(raw_key)
    expires_at = now + IDEMPOTENCY_TTL

    for _ in range(_RESERVE_MAX_ATTEMPTS):
        # Unique-insert-wins: the first writer creates the row; everyone else
        # loses the race atomically at the DB and inspects the winner below.
        if await store.begin(org_id, route, key_hash, request_hash, now, expires_at):
            return Reservation(key_hash=key_hash)

        existing = await store.get(org_id, route, key_hash)
        if existing is None:
            # Pruned between our failed insert and this read; try to claim again.
            continue

        if existing.expires_at <= now:
            # Stale: prune only the expired row (scoped so a fresh row a
            # concurrent retry inserted is never deleted), then claim again.
            await store.discard(org_id, route, key_hash, now)
            continue

        # A live row owns the slot -- decide without creating anything.
        if existing.request_hash != request_hash:
            raise IdempotencyConflict(
                "This Idempotency-Key was already used with a different request."
            )
        if existing.status == STATUS_IN_PROGRESS:
            raise IdempotencyConflict(
                "A submission with this Idempotency-Key is still in progress."
            )
        # Completed with a matching request fingerprint -> replay the result.
        raise IdempotencyReplay(existing.response_json or {})

    # Insert and prune kept racing past the retry budget; treat the contending
    # writer as an in-progress duplicate rather than creating a second time.
    raise IdempotencyConflict(
        "A submission with this Idempotency-Key is already in progress."
    )
