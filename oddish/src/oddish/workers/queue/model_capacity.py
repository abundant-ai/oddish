"""Shared admission for model calls, independent of worker concurrency slots.

No transaction spans a provider call. Estimates remain charged for the bounded
request lifetime; completed calls retain actual usage for another 60 seconds.
That conservative window also covers output generated late in a long stream.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import timedelta
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, model_validator
from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert

from oddish.db import get_session
from oddish.db.models import (
    ModelRequestLeaseModel as Lease,
    ModelRequestPoolModel as Pool,
)

ROUTE_THRESHOLD = 0.65
REQUEST_TIMEOUT_SECONDS = 600
WINDOW_SECONDS = 60
# All decisions span the same configured pools. One short lock serializes the
# read + reserve operation across API replicas; it never protects network IO.
_ADMISSION_LOCK = 713651009


class ProviderPool(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    id: str = Field(min_length=1, max_length=128)
    # Operator-verified account + quota identity, NOT the API key identity.
    quota_group: str = Field(min_length=1)
    provider: Literal["anthropic", "bedrock"]
    model: str = Field(min_length=1)
    key_env: str = Field(pattern=r"^[A-Z][A-Z0-9_]*$")
    region: str | None = Field(default=None, pattern=r"^[a-z]{2}-[a-z]+-\d+$")
    requests_per_minute: int = Field(gt=0)
    input_tokens_per_minute: int | None = Field(default=None, gt=0)
    output_tokens_per_minute: int | None = Field(default=None, gt=0)
    tokens_per_minute: int | None = Field(default=None, gt=0)
    output_multiplier: int | None = Field(default=None, gt=0)
    # Account use not passing through this gateway. Bedrock does not expose
    # Anthropic remaining-token headers, so reserve that traffic explicitly.
    supported_betas: list[str] = Field(default_factory=list)
    external_load_fraction: float = Field(default=0, ge=0, lt=ROUTE_THRESHOLD)

    @model_validator(mode="after")
    def validate_accounting(self):
        if self.provider == "anthropic":
            if not self.input_tokens_per_minute or not self.output_tokens_per_minute:
                raise ValueError("Anthropic requires separate input/output limits")
            if self.tokens_per_minute or self.output_multiplier not in (None, 1):
                raise ValueError("Anthropic does not use Bedrock weighted tokens")
        elif (
            not self.region or not self.tokens_per_minute or not self.output_multiplier
        ):
            raise ValueError(
                "Bedrock requires its region, weighted token limit and output multiplier"
            )
        elif self.input_tokens_per_minute or self.output_tokens_per_minute:
            raise ValueError("Bedrock uses a combined weighted token limit")
        return self

    def load(self, requests: int, inputs: int, outputs: int) -> float:
        # PostgreSQL SUM(bigint) returns Decimal; utilization is a float at
        # this boundary so configured fractional reserves compose correctly.
        ratios = [float(requests) / self.requests_per_minute]
        if self.input_tokens_per_minute:
            ratios.append(float(inputs) / self.input_tokens_per_minute)
        if self.output_tokens_per_minute:
            ratios.append(float(outputs) / self.output_tokens_per_minute)
        if self.tokens_per_minute:
            ratios.append(
                float(inputs + (self.output_multiplier or 1) * outputs)
                / self.tokens_per_minute
            )
        return max(ratios)


def configured_pools() -> list[ProviderPool]:
    pools = TypeAdapter(list[ProviderPool]).validate_json(
        os.environ.get("ODDISH_QA_MODEL_POOLS", "[]")
    )
    for attr in ("id", "quota_group"):
        if len({getattr(p, attr) for p in pools}) != len(pools):
            raise ValueError(
                f"QA model pools must have distinct {attr}; shared accounts are one pool"
            )
    from oddish.config import to_anthropic_api_model_id

    if any(to_anthropic_api_model_id(p.model) != "claude-sonnet-5" for p in pools):
        raise ValueError("QA routing pools must serve the same Sonnet 5 model")
    available = [p for p in pools if os.environ.get(p.key_env)]
    # Distinct labels cannot turn identical credentials into independent pools.
    if len({os.environ[p.key_env] for p in available}) != len(available):
        raise ValueError("QA model pools contain duplicate credentials")
    return available


class CapacityUnavailable(Exception):
    """Caller should wait; no provider was contacted."""


@dataclass(frozen=True)
class Reservation:
    id: str
    pool: ProviderPool


async def _read_capacity(
    session, pools: list[ProviderPool], worker_job_id: str | None = None
):
    # One bounded query, rather than a database round trip per pool. Expired
    # reservations stop counting independently of garbage collection.
    rows = (
        (
            await session.execute(
                text("""
        WITH expired AS (
            DELETE FROM model_request_leases WHERE expires_at <= clock_timestamp()
        ), configured AS (SELECT unnest(CAST(:ids AS text[])) AS id)
        SELECT c.id, p.cooldown_until, p.observed_at, p.observed_load,
               count(l.id) AS requests,
               coalesce(sum(l.input_tokens), 0) AS inputs,
               coalesce(sum(l.output_tokens), 0) AS outputs,
               count(l.id) FILTER (WHERE l.active) AS active_requests,
               count(l.id) FILTER (WHERE l.active OR l.created_at >= p.observed_at) AS outstanding_requests,
               coalesce(sum(l.input_tokens) FILTER (WHERE l.active OR l.created_at >= p.observed_at), 0) AS outstanding_inputs,
               coalesce(sum(l.output_tokens) FILTER (WHERE l.active OR l.created_at >= p.observed_at), 0) AS outstanding_outputs,
               (SELECT pool_id FROM model_request_leases WHERE worker_job_id = :worker
                ORDER BY created_at DESC LIMIT 1) AS preferred,
               clock_timestamp() AS observed_now
          FROM configured c
          LEFT JOIN model_request_pools p ON p.id = c.id
          LEFT JOIN model_request_leases l ON l.pool_id = c.id AND l.expires_at > clock_timestamp()
         GROUP BY c.id, p.cooldown_until, p.observed_at, p.observed_load
    """),
                {"ids": [p.quota_group for p in pools], "worker": worker_job_id},
            )
        )
        .mappings()
        .all()
    )
    return {row["id"]: row for row in rows}


def projected_load(
    pool: ProviderPool, state, inputs: int, outputs: int, requests: int = 1
) -> float:
    load = pool.external_load_fraction + pool.load(
        state["requests"] + requests,
        state["inputs"] + inputs,
        state["outputs"] + outputs,
    )
    if state["observed_at"] and state["observed_at"] > state[
        "observed_now"
    ] - timedelta(seconds=WINDOW_SECONDS):
        # Include calls outstanding at/after the latest provider observation.
        # Some may already appear in its header: conservatively count them
        # again rather than let replicas spend the same unobserved headroom.
        load = max(
            load,
            state["observed_load"]
            + pool.load(
                state["outstanding_requests"] + requests,
                state["outstanding_inputs"] + inputs,
                state["outstanding_outputs"] + outputs,
            ),
        )
    return load


async def capacity_snapshot(pools: list[ProviderPool]) -> list[dict]:
    if not pools:
        return []
    async with get_session() as session:
        states = await _read_capacity(session, pools)
    return [
        {
            "pool_id": p.id,
            "provider": p.provider,
            "routing_load": projected_load(p, states[p.quota_group], 0, 0, 0),
            "active_requests": states[p.quota_group]["active_requests"],
            "reserved_or_recent_requests": states[p.quota_group]["requests"],
            "cooldown_until": states[p.quota_group]["cooldown_until"],
            "accepting_requests": projected_load(p, states[p.quota_group], 0, 0)
            <= ROUTE_THRESHOLD
            and not (
                states[p.quota_group]["cooldown_until"]
                and states[p.quota_group]["cooldown_until"]
                > states[p.quota_group]["observed_now"]
            ),
        }
        for p in pools
    ]


async def reserve_request(
    pools: list[ProviderPool],
    *,
    worker_job_id: str,
    input_tokens: int,
    output_tokens: int,
) -> Reservation:
    if not pools:
        raise CapacityUnavailable("No verified QA model pool is configured")
    async with get_session() as session:
        await session.execute(
            text("SELECT pg_advisory_xact_lock(:key)"), {"key": _ADMISSION_LOCK}
        )
        states = await _read_capacity(session, pools, worker_job_id)
        eligible = []
        for index, p in enumerate(pools):
            state = states[p.quota_group]
            if (
                state["cooldown_until"]
                and state["cooldown_until"] > state["observed_now"]
            ):
                continue
            projected = projected_load(p, state, input_tokens, output_tokens)
            if projected <= ROUTE_THRESHOLD:
                eligible.append(
                    (
                        p.quota_group != (state["preferred"] or pools[0].quota_group),
                        projected,
                        index,
                        p,
                    )
                )
        if not eligible:
            raise CapacityUnavailable(
                "All QA model pools are at their 65% routing budget"
            )
        pool = min(eligible, key=lambda row: row[:3])[3]
        now = states[pool.quota_group]["observed_now"]
        reservation = Reservation(uuid4().hex, pool)
        session.add(
            Lease(
                id=reservation.id,
                pool_id=pool.quota_group,
                worker_job_id=worker_job_id,
                created_at=now,
                expires_at=now
                + timedelta(seconds=REQUEST_TIMEOUT_SECONDS + WINDOW_SECONDS),
                active=True,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
            )
        )
    return reservation


async def settle_request(
    reservation: Reservation,
    *,
    usage: dict | None,
) -> None:
    async with get_session() as session:
        await session.execute(
            text("SELECT pg_advisory_xact_lock(:key)"), {"key": _ADMISSION_LOCK}
        )
        now = await session.scalar(select(func.clock_timestamp()))
        lease = await session.get(Lease, reservation.id)
        if lease is None or not lease.active:
            return
        # Unknown/interrupted outcomes retain their full estimate: a disconnect
        # is not evidence that the provider generated no billable output.
        if usage is not None:
            lease.input_tokens = max(0, int(usage.get("input_tokens", 0))) + max(
                0, int(usage.get("cache_creation_input_tokens", 0))
            )
            lease.output_tokens = max(0, int(usage.get("output_tokens", 0)))
        lease.active = False
        if usage is not None:
            lease.expires_at = now + timedelta(seconds=WINDOW_SECONDS)


async def observe_provider(
    pool_id: str, headers: dict[str, str], *, cooldown_seconds: int = 0
) -> None:
    """Apply response headers immediately, before streaming the response body."""
    async with get_session() as session:
        await session.execute(
            text("SELECT pg_advisory_xact_lock(:key)"), {"key": _ADMISSION_LOCK}
        )
        now = await session.scalar(select(func.clock_timestamp()))
        await session.execute(insert(Pool).values(id=pool_id).on_conflict_do_nothing())
        state = await session.get(Pool, pool_id)
        if cooldown_seconds:
            until = now + timedelta(seconds=cooldown_seconds)
            state.cooldown_until = max(state.cooldown_until or until, until)
        loads = []
        for resource in ("requests", "input-tokens", "output-tokens", "tokens"):
            prefix = f"anthropic-ratelimit-{resource}"
            try:
                limit = float(headers[f"{prefix}-limit"])
                remaining = float(headers[f"{prefix}-remaining"])
            except (KeyError, ValueError):
                continue
            if limit > 0:
                loads.append(max(0, 1 - remaining / limit))
        if loads:
            load = max(loads)
            state.observed_at, state.observed_load = now, load
