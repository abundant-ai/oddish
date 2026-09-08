"""Attempt-scoped gateway credentials, reusing worker_jobs' token lifecycle."""

from __future__ import annotations

import os
from datetime import timedelta
from urllib.parse import urlsplit

from sqlalchemy import update

from oddish.config import (
    to_anthropic_api_model_id,
    anthropic_hdo_bare_model_id,
)
from oddish.db import get_session
from oddish.db.models import WorkerJobModel, WorkerJobStatus, utcnow
from oddish.workers.queue.job_tokens import mint_token
from oddish.workers.queue.model_capacity import configured_pools

GATEWAY_MODEL = "claude-sonnet-5"


def is_gateway_trial(
    *,
    kind: str,
    agent: str,
    model: str | None,
    byok_env: dict | None,
    harbor_config: dict,
) -> bool:
    return (
        kind in {"qa", "audit", "qa_eval"}
        and agent == "claude-code"
        and to_anthropic_api_model_id(anthropic_hdo_bare_model_id(model or ""))
        == GATEWAY_MODEL
        and not byok_env
        and harbor_config.get("variant_id") != "ephemeral"
        and not (harbor_config.get("agent_config") or {}).get("import_path")
    )


async def mint_gateway_env(worker_job_id: str, attempt: int) -> dict[str, str]:
    if not configured_pools():
        raise RuntimeError("QA model routing is enabled without a usable verified pool")
    base_url = os.environ.get("ODDISH_QA_MODEL_GATEWAY_URL", "").rstrip("/")
    parsed = urlsplit(base_url)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(
            "ODDISH_QA_MODEL_GATEWAY_URL must be the dedicated gateway HTTPS URL"
        )
    base_url += "/qa-model"
    raw, digest = mint_token()
    async with get_session() as session:
        result = await session.execute(
            update(WorkerJobModel)
            .where(
                WorkerJobModel.id == worker_job_id,
                WorkerJobModel.attempts == attempt,
                WorkerJobModel.status == WorkerJobStatus.RUNNING,
            )
            .values(
                job_token_hash=digest,
                job_token_expires_at=utcnow() + timedelta(hours=12),
                job_token_revoked_at=None,
            )
        )
        if result.rowcount != 1:
            raise RuntimeError(
                "Cannot issue QA model credentials for an inactive worker attempt"
            )
    return {
        "ODDISH_QA_MODEL_ROUTED": "1",
        "ANTHROPIC_BASE_URL": base_url,
        "ANTHROPIC_API_KEY": f"{worker_job_id}.{raw}",
        "ANTHROPIC_AUTH_TOKEN": "",
        "CLAUDE_FORCE_OAUTH": "0",
        "CLAUDE_CODE_OAUTH_TOKEN": "",
        "CLAUDE_CODE_USE_BEDROCK": "",
        "AWS_BEARER_TOKEN_BEDROCK": "",
        "CLAUDE_CODE_USE_VERTEX": "",
        "ANTHROPIC_MODEL": GATEWAY_MODEL,
        "CLAUDE_CODE_SUBAGENT_MODEL": GATEWAY_MODEL,
        "ANTHROPIC_DEFAULT_HAIKU_MODEL": GATEWAY_MODEL,
        "ANTHROPIC_DEFAULT_SONNET_MODEL": GATEWAY_MODEL,
        "ANTHROPIC_DEFAULT_OPUS_MODEL": GATEWAY_MODEL,
        "ANTHROPIC_DEFAULT_FABLE_MODEL": GATEWAY_MODEL,
    }
