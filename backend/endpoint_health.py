"""Scheduled checks of explicitly configured platform model connections.

The database owns claims and incident transitions; provider calls hold no DB
connection. Admin reads only the small current-state table, never trial history.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from time import monotonic
from typing import Annotated, Literal
from urllib.parse import urlsplit
from uuid import uuid4

from openai import OpenAIError
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, model_validator
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from oddish.db import get_session, utcnow

log = logging.getLogger(__name__)
CHECK_INTERVAL = timedelta(minutes=15)
CONFIRM_INTERVAL = timedelta(minutes=1)
LEASE_DURATION = timedelta(minutes=3)
STALE_AFTER = timedelta(minutes=30)
REQUEST_TIMEOUT = 20
BATCH_SIZE = 10
CONCURRENCY = 5
EnvName = Annotated[str, Field(pattern=r"^[A-Z][A-Z0-9_]*$")]
Outcome = Literal["success", "failure", "monitor_error"]


class EndpointTarget(BaseModel):
    """Deployment configuration; values refer to existing runtime secrets."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    name: str = Field(min_length=1, max_length=100)
    model: str = Field(pattern=r"^[a-zA-Z0-9_-]+/.+", max_length=255)
    api_key_env: EnvName | None = None
    api_base_env: EnvName | None = None
    api_version_env: EnvName | None = None
    region: str | None = None
    max_tokens: int = Field(default=1024, ge=32, le=4096)

    @model_validator(mode="after")
    def require_credentials(self):
        if not self.model.startswith("bedrock/") and not self.api_key_env:
            raise ValueError("api_key_env is required outside Bedrock")
        if self.model.startswith("bedrock/") and (self.api_key_env or not self.region):
            raise ValueError("Bedrock uses runtime AWS credentials and requires region")
        if self.model.startswith("azure/") and not (
            self.api_base_env and self.api_version_env
        ):
            raise ValueError("Azure requires api_base_env and api_version_env")
        return self

    @property
    def id(self) -> str:
        # Labels do not identify connections. Changing a model, credential
        # reference, region, or endpoint reference creates a new history.
        identity = self.model_dump(exclude={"name", "max_tokens"})
        return hashlib.sha256(
            json.dumps(identity, sort_keys=True).encode()
        ).hexdigest()[:32]


def configured_targets() -> list[EndpointTarget]:
    targets = TypeAdapter(list[EndpointTarget]).validate_json(
        os.environ.get("ODDISH_ENDPOINT_MONITORS", "[]")
    )
    if len(targets) > 100:
        raise ValueError("At most 100 endpoint monitors are supported")
    if len({t.id for t in targets}) != len(targets):
        raise ValueError("Endpoint monitors must name distinct connections")
    return targets


def monitoring_enabled() -> bool:
    return (
        os.environ.get(
            "ODDISH_ENABLE_ENDPOINT_MONITORING",
            "true" if os.environ.get("MODAL_APP_NAME") == "oddish" else "false",
        ).lower()
        == "true"
    )


@dataclass(frozen=True)
class CheckResult:
    monitor_id: str
    outcome: Outcome
    checked_at: datetime
    latency_ms: int
    error: str | None = None
    status_code: int | None = None
    request_id: str | None = None


async def check_endpoint(target: EndpointTarget) -> CheckResult:
    import litellm

    start = monotonic()
    kwargs: dict = {"max_tokens": target.max_tokens}
    # No ambient model router, fallback list, or SDK retry may hide which
    # connection failed. A confirmation is a separate recorded check.
    try:
        for field, argument in (
            (target.api_key_env, "api_key"),
            (target.api_base_env, "api_base"),
            (target.api_version_env, "api_version"),
        ):
            if field:
                value = os.environ.get(field, "").strip()
                if not value:
                    return CheckResult(
                        target.id,
                        "monitor_error",
                        utcnow(),
                        0,
                        f"Missing {field} configuration.",
                    )
                kwargs[argument] = value
        if target.api_base_env:
            url = urlsplit(kwargs["api_base"])
            if (
                url.scheme != "https"
                or not url.hostname
                or url.username
                or url.password
                or url.query
                or url.fragment
            ):
                return CheckResult(
                    target.id,
                    "monitor_error",
                    utcnow(),
                    0,
                    "Endpoint must be HTTPS without credentials or query parameters.",
                )
        if target.region:
            kwargs["aws_region_name"] = target.region
        async with asyncio.timeout(REQUEST_TIMEOUT + 2):
            response = await litellm.acompletion(
                model=target.model,
                messages=[
                    {"role": "user", "content": "Say hello in one short sentence."}
                ],
                timeout=REQUEST_TIMEOUT,
                num_retries=0,
                caching=False,
                **kwargs,
            )
        content = response.choices[0].message.content
        ok = isinstance(content, str) and bool(content.strip())
        return CheckResult(
            target.id,
            "success" if ok else "failure",
            utcnow(),
            round((monotonic() - start) * 1000),
            None if ok else "Provider returned no text response.",
            request_id=str(response.id)[:200] if response.id else None,
        )
    except (
        OpenAIError,
        litellm.APIError,
        litellm.Timeout,
        litellm.APIConnectionError,
        TimeoutError,
    ) as exc:
        status = getattr(exc, "status_code", None)
        status = status if isinstance(status, int) else None
        explanations = {
            400: "Provider rejected the check request; inspect model configuration.",
            401: "Provider rejected this connection's credential.",
            403: "Credential is not permitted to use this model.",
            404: "Model or endpoint was not found for this credential.",
            429: "Provider rate limit or usage allowance exceeded.",
        }
        error = explanations.get(status, "Provider request failed.")
        if isinstance(exc, (litellm.Timeout, TimeoutError)):
            error = "Provider request timed out."
        elif status is not None and status >= 500:
            error = "Provider returned a server error."
        # Never persist raw exception text: providers can echo request secrets.
        request_id = getattr(exc, "request_id", None)
        return CheckResult(
            target.id,
            "failure",
            utcnow(),
            round((monotonic() - start) * 1000),
            error,
            status,
            request_id[:200] if isinstance(request_id, str) else None,
        )
    except Exception as exc:
        log.error(
            "Endpoint checker defect monitor=%s type=%s", target.id, type(exc).__name__
        )
        return CheckResult(
            target.id,
            "monitor_error",
            utcnow(),
            round((monotonic() - start) * 1000),
            "The monitor could not complete this check.",
        )


def alerts_enabled() -> bool:
    return (
        bool(os.environ.get("SLACK_EXPENSE_WEBHOOK_URL"))
        and os.environ.get(
            "ODDISH_ENABLE_SLACK_EXPENSE_NOTIFICATIONS",
            "true" if os.environ.get("MODAL_APP_NAME") == "oddish" else "false",
        ).lower()
        == "true"
    )


async def sync_targets(session: AsyncSession, targets: list[EndpointTarget]) -> None:
    # One statement for the whole configuration. Runtime secret values never
    # enter SQL. Rows removed from configuration stop being claimed immediately.
    await session.execute(
        text("""
        WITH configured AS (
            SELECT * FROM jsonb_to_recordset(CAST(:targets AS jsonb))
              AS x(id text, name text, model text, credential_ref text)
        ), upserted AS (
            INSERT INTO endpoint_monitors (id, name, model, credential_ref)
            SELECT id, name, model, credential_ref FROM configured
            ON CONFLICT (id) DO UPDATE SET name = excluded.name, enabled = true,
                next_check_at = CASE WHEN endpoint_monitors.enabled THEN endpoint_monitors.next_check_at ELSE now() END
            WHERE endpoint_monitors.name != excluded.name OR NOT endpoint_monitors.enabled
            RETURNING id
        )
        UPDATE endpoint_monitors SET enabled = false, lease_token = NULL, lease_until = NULL
        WHERE enabled AND id NOT IN (SELECT id FROM configured)
    """),
        {
            "targets": json.dumps(
                [
                    {
                        "id": t.id,
                        "name": t.name,
                        "model": t.model,
                        "credential_ref": t.api_key_env or "AWS runtime credentials",
                    }
                    for t in targets
                ]
            )
        },
    )


async def claim_checks(
    session: AsyncSession, now: datetime, monitor_ids: list[str]
) -> list[dict]:
    return [
        dict(row)
        for row in (
            await session.execute(
                text("""
        WITH due AS (
            SELECT id FROM endpoint_monitors
            WHERE enabled AND id = ANY(CAST(:ids AS text[])) AND next_check_at <= :now
              AND (lease_until IS NULL OR lease_until <= :now)
            ORDER BY next_check_at, id LIMIT :limit FOR UPDATE SKIP LOCKED
        )
        UPDATE endpoint_monitors m
           SET lease_token = :token, lease_until = :until
          FROM due WHERE m.id = due.id
        RETURNING m.*
    """),
                {
                    "now": now,
                    "ids": monitor_ids,
                    "until": now + LEASE_DURATION,
                    "token": str(uuid4()),
                    "limit": BATCH_SIZE,
                },
            )
        ).mappings()
    ]


async def save_results(
    session: AsyncSession,
    claimed: list[dict],
    results: list[CheckResult],
    *,
    alerts_enabled: bool,
) -> int:
    """Fence expired owners, then atomically record evidence and incident alerts."""
    claims = {row["id"]: row for row in claimed}
    updates, checks, alerts = [], {}, {}
    for result in results:
        old = claims[result.monitor_id]
        failures = old["consecutive_failures"]
        incident = old["incident_id"]
        opened_at = old["incident_opened_at"]
        transition = None
        if result.outcome == "failure":
            failures += 1
            if failures >= 2 and not incident:
                incident, opened_at = str(uuid4()), result.checked_at
                transition = "opened"
        elif result.outcome == "success":
            failures = 0
            if incident:
                transition = "recovered"
        else:
            # An internal defect cannot count toward a provider outage.
            failures = 0
        check_incident = incident
        if transition:
            import html

            name = html.escape(old["name"])
            detail = result.error or "A test completion succeeded."
            url = os.environ.get(
                "ODDISH_DASHBOARD_URL", "https://www.oddish.app"
            ).rstrip("/")
            alerts[result.monitor_id] = {
                "alert_key": f"endpoint:{incident}:{transition}",
                "payload": f"Endpoint {transition}: {name}\n{detail}\n{url}/admin?endpoint={result.monitor_id}",
                "claimed_at": result.checked_at.isoformat(),
            }
        if transition == "recovered":
            incident, opened_at = None, None
        delay = CONFIRM_INTERVAL if result.outcome != "success" else CHECK_INTERVAL
        # During an incident checks stay prompt; this also bounds recovery delay.
        updates.append(
            {
                "id": result.monitor_id,
                "alerts_enabled": alerts_enabled,
                "token": old["lease_token"],
                "outcome": result.outcome,
                "checked_at": result.checked_at.isoformat(),
                "last_success_at": result.checked_at.isoformat()
                if result.outcome == "success"
                else None,
                "error": result.error,
                "status_code": result.status_code,
                "failures": failures,
                "incident": incident,
                "opened_at": opened_at.isoformat() if opened_at else None,
                "next_check_at": (result.checked_at + delay).isoformat(),
            }
        )
        checks[result.monitor_id] = {
            **asdict(result),
            "checked_at": result.checked_at.isoformat(),
            "incident_id": check_incident,
            "claim_token": old["lease_token"],
        }
    if not updates:
        return 0
    accepted = list(
        (
            await session.execute(
                text("""
        UPDATE endpoint_monitors m SET
            last_outcome = v.outcome, last_checked_at = v.checked_at,
            alerts_enabled = v.alerts_enabled,
            last_success_at = coalesce(v.last_success_at, m.last_success_at),
            error = v.error, status_code = v.status_code,
            consecutive_failures = v.failures, incident_id = v.incident,
            incident_opened_at = v.opened_at, next_check_at = v.next_check_at,
            lease_token = NULL, lease_until = NULL
        FROM jsonb_to_recordset(CAST(:updates AS jsonb)) AS v(
            id text, token text, alerts_enabled boolean, outcome text, checked_at timestamptz,
            last_success_at timestamptz, error text, status_code integer,
            failures integer, incident text, opened_at timestamptz, next_check_at timestamptz
        )
        WHERE m.id = v.id AND m.enabled AND m.lease_token = v.token
        RETURNING m.id
    """),
                {"updates": json.dumps(updates)},
            )
        ).scalars()
    )
    if not accepted:
        return 0
    await session.execute(
        text("""
        INSERT INTO endpoint_checks
            (monitor_id, claim_token, checked_at, outcome, latency_ms, error, status_code, request_id, incident_id)
        SELECT monitor_id, claim_token, checked_at, outcome, latency_ms, error, status_code, request_id, incident_id
        FROM jsonb_to_recordset(CAST(:checks AS jsonb)) AS v(
            monitor_id text, claim_token text, checked_at timestamptz, outcome text,
            latency_ms integer, error text, status_code integer, request_id text, incident_id text)
    """),
        {"checks": json.dumps([checks[key] for key in accepted])},
    )
    pending = (
        [alerts[key] for key in accepted if key in alerts] if alerts_enabled else []
    )
    if pending:
        # Use the existing Slack outbox and sender, within THIS transaction.
        # record_alerts() currently opens a separate transaction of its own.
        await session.execute(
            text("""
            INSERT INTO slack_expense_alerts (alert_key, payload, claimed_at)
            SELECT alert_key, payload, claimed_at
            FROM jsonb_to_recordset(CAST(:alerts AS jsonb))
                AS v(alert_key text, payload text, claimed_at timestamptz)
            ON CONFLICT (alert_key) DO NOTHING
        """),
            {"alerts": json.dumps(pending)},
        )
    return len(accepted)


async def run_checks() -> int:
    targets = configured_targets()
    by_id = {target.id: target for target in targets}
    async with get_session() as session:
        await sync_targets(session, targets)
        claimed = await claim_checks(session, utcnow(), list(by_id))
    semaphore = asyncio.Semaphore(CONCURRENCY)

    async def probe(row):
        async with semaphore:
            return await check_endpoint(by_id[row["id"]])

    results = await asyncio.gather(*(probe(row) for row in claimed))
    async with get_session() as session:
        return await save_results(
            session,
            claimed,
            results,
            alerts_enabled=alerts_enabled(),
        )
