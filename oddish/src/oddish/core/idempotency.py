from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Protocol, runtime_checkable

SWEEP_ROUTE = "POST /tasks/sweep"
IDEMPOTENCY_TTL = timedelta(hours=24)

STATUS_IN_PROGRESS = "in_progress"
STATUS_COMPLETED = "completed"

_RESERVE_MAX_ATTEMPTS = 5


def _canonical_digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode(
            "utf-8"
        )
    ).hexdigest()


def compute_sweep_idempotency_key(payload: Mapping[str, Any]) -> str:
    return _canonical_digest(payload)


def hash_idempotency_key(raw_key: str) -> str:
    return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()


def compute_request_hash(submission: Any) -> str:
    data = submission.model_dump(mode="json")
    if data.get("github_id") is None:
        data.pop("github_id", None)
    return _canonical_digest(data)


@dataclass(frozen=True)
class StoredIdempotencyRecord:
    request_hash: str
    status: str
    response_json: dict | None
    expires_at: datetime


class IdempotencyConflict(Exception):
    pass


class IdempotencyReplay(Exception):
    def __init__(self, response_json: dict) -> None:
        super().__init__("idempotent replay")
        self.response_json = response_json


@runtime_checkable
class IdempotencyStore(Protocol):
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
    ) -> bool: ...

    async def complete(
        self, org_id: str, route: str, key_hash: str, response_json: dict
    ) -> None: ...

    async def discard(
        self, org_id: str, route: str, key_hash: str, now: datetime
    ) -> None: ...


@dataclass(frozen=True)
class Reservation:
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
    key_hash = hash_idempotency_key(raw_key)

    for _ in range(_RESERVE_MAX_ATTEMPTS):
        if await store.begin(
            org_id,
            route,
            key_hash,
            request_hash,
            now,
            now + IDEMPOTENCY_TTL,
        ):
            return Reservation(key_hash=key_hash)

        existing = await store.get(org_id, route, key_hash)
        if existing is None:
            continue

        if existing.expires_at <= now:
            await store.discard(org_id, route, key_hash, now)
            continue

        if existing.request_hash != request_hash:
            raise IdempotencyConflict(
                "This Idempotency-Key was already used with a different request."
            )
        if existing.status == STATUS_IN_PROGRESS:
            raise IdempotencyConflict(
                "A submission with this Idempotency-Key is still in progress."
            )
        raise IdempotencyReplay(existing.response_json or {})

    raise IdempotencyConflict(
        "A submission with this Idempotency-Key is already in progress."
    )
