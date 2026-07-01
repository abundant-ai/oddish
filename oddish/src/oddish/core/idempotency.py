from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Protocol, runtime_checkable

from pydantic import SecretStr

from oddish.registry_auth import DOCKER_HUB_AUTH_KEY, normalize_registry_host

# Logical route name recorded alongside the key so the same key may be reused on
# unrelated endpoints in the future without colliding.
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


def _secret_value(value: Any) -> str:
    if isinstance(value, SecretStr):
        return value.get_secret_value()
    if hasattr(value, "get_secret_value"):
        return str(value.get_secret_value())
    return str(value or "")


def _registry_auth_fingerprints(raw: Any) -> list[dict[str, str]]:
    if not raw:
        return []
    items = raw if isinstance(raw, list) else [raw]
    fingerprints: list[dict[str, str]] = []
    for item in items:
        if hasattr(item, "model_dump"):
            registry = getattr(item, "registry", "docker.io")
            username = getattr(item, "username", "")
            token = getattr(item, "token", "")
        else:
            registry = item.get("registry", "docker.io")
            username = item.get("username", "")
            token = item.get("token") or item.get("password") or ""
        host = normalize_registry_host(str(registry))
        auth_key = DOCKER_HUB_AUTH_KEY if host == "docker.io" else host
        fingerprints.append(
            {
                "registry": auth_key,
                "username": str(username).strip(),
                "token_sha256": hashlib.sha256(
                    _secret_value(token).encode("utf-8")
                ).hexdigest(),
            }
        )
    return sorted(fingerprints, key=lambda item: (item["registry"], item["username"]))


def _payload_with_registry_auth_fingerprint(payload: dict[str, Any]) -> dict[str, Any]:
    if "registry_auth" not in payload:
        return payload
    payload = dict(payload)
    payload["registry_auth"] = _registry_auth_fingerprints(payload.get("registry_auth"))
    return payload


def compute_sweep_idempotency_key(payload: Mapping[str, Any]) -> str:
    return _canonical_digest(_payload_with_registry_auth_fingerprint(dict(payload)))


def hash_idempotency_key(raw_key: str) -> str:
    return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()


def compute_request_hash(submission: Any) -> str:
    payload = submission.model_dump(mode="json")
    # Drop an absent github_id so an honest retry that never sent it hashes the
    # same as the original (linkage idempotency guard).
    if payload.get("github_id") is None:
        payload.pop("github_id", None)
    # Fingerprint registry creds so equivalent secrets replay instead of leaking
    # raw tokens into the request hash.
    if hasattr(submission, "registry_auth"):
        payload["registry_auth"] = _registry_auth_fingerprints(
            getattr(submission, "registry_auth", None)
        )
    return _canonical_digest(payload)


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
