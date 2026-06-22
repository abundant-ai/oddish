"""Job-scoped credential bundles (design spec §5.4 / §6.6).

A claimed trial can be issued a short-lived, **least-privilege** credential
bundle instead of the worker reading the blanket ``oddish-prod`` secret:

* the **model API key(s) for this job's provider only** — a Claude job's bundle
  carries the Anthropic key, never the OpenAI/Gemini keys — injected into the
  agent env in place of the blanket key set;
* an **S3 write prefix** that scopes oddish's *own* trial-artifact uploads to
  this trial's prefix — an oddish-side check in ``StorageClient`` (defense in
  depth against a path-traversal escape on that upload path). It does **not**
  cover Harbor's or other tools' uploads (they use their own credentials); full
  cross-path write isolation would need IAM/STS-scoped S3 credentials (§14.2);
* a random **token** whose SHA-256 hash is persisted on the ``worker_jobs`` row
  (the raw token is never stored) as the revocable credential handle, revoked on
  terminal status.

This module is pure (no DB / network). The gated wiring lives in the trial
handler (issue the bundle, inject the scoped model env, revoke on terminal) and
``StorageClient`` (enforce the prefix on oddish's trial uploads), behind
``settings.job_scoped_tokens_enabled`` (default off; dual-read hedge: bundle if
present, else the blanket secret).
"""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Mapping

DEFAULT_TTL_SECONDS = 14400  # 4h: covers queue wait + run; trial timeouts are under this


@dataclass(frozen=True)
class JobCredentialBundle:
    """The credentials handed to a worker for one claimed job."""

    model_env: Mapping[str, str]
    s3_write_prefix: str
    expires_at: datetime


def mint_token() -> tuple[str, str]:
    """Return ``(raw_token, token_hash)``; only the hash is ever persisted."""
    raw = secrets.token_urlsafe(32)
    return raw, hash_token(raw)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def s3_write_prefix_for(trial_id: str) -> str:
    """The oddish S3 prefix that oddish's trial-artifact uploads are scoped to.

    Mirrors ``StorageClient._trial_prefix`` so the scope matches where artifacts
    are actually uploaded (``tasks/{task_id}/trials/{trial_id}/``).
    """
    task_id, sep, maybe_index = trial_id.rpartition("-")
    if sep and maybe_index.isdigit() and task_id:
        return f"tasks/{task_id}/trials/{trial_id}/"
    return f"trials/{trial_id}/"


def authorize_s3_key(key: str, prefix: str) -> bool:
    """True iff ``key`` is within the job's authorized write ``prefix``."""
    return bool(key) and key.startswith(prefix)


# Providers whose key env is assembled by ``settings.get_openai_agent_env``.
_OPENAI_FAMILY = {"openai", "azure", "azure_openai"}


def scoped_model_env(*, agent: str, model: str | None, settings: Any) -> dict[str, str]:
    """Least-privilege model env for the job's provider only.

    Resolves the provider via ``settings.get_provider_for_trial`` and returns
    just that provider's key env — never the full blanket key set. Unknown
    providers return ``{}`` so the caller's dual-read falls back to the blanket
    secret rather than shipping an empty credential.
    """
    provider = (settings.get_provider_for_trial(agent, model) or "").lower()

    if provider in _OPENAI_FAMILY:
        return {k: v for k, v in settings.get_openai_agent_env(model=model).items() if v}
    if provider in ("anthropic", "claude"):
        key = getattr(settings, "anthropic_api_key", None)
        return {"ANTHROPIC_API_KEY": key} if key else {}
    if provider == "bedrock":
        # Bedrock authenticates with AWS creds, not a single API key; scoping
        # those needs STS (a future enhancement). Carry only the routing flag;
        # the worker's dual-read keeps the ambient AWS creds for the rest.
        return {"CLAUDE_CODE_USE_BEDROCK": "1"}
    if provider == "gemini":
        key = getattr(settings, "gemini_api_key", None)
        return {"GEMINI_API_KEY": key} if key else {}
    return {}


def merge_agent_env(
    bundle: "JobCredentialBundle | None",
    probe_env: Mapping[str, str] | None,
) -> dict[str, str] | None:
    """Combine the job-scoped model env with any probe env for the agent.

    The agent receives the bundle's scoped model key(s) plus any probe creds;
    probe creds win on the (non-overlapping) off chance of a key clash. Returns
    ``None`` when there is nothing to inject, preserving the
    ``extra_agent_env: dict | None`` contract.
    """
    if bundle is None:
        # No bundle (the common, flag-off case): pass the caller's env through
        # unchanged so behavior is byte-for-byte identical.
        return dict(probe_env) if probe_env is not None else None
    merged = dict(bundle.model_env)
    if probe_env:
        merged.update(probe_env)
    return merged or None


def build_bundle(
    *,
    agent: str,
    model: str | None,
    trial_id: str,
    settings: Any,
    now: datetime,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
) -> tuple[JobCredentialBundle, str]:
    """Assemble a job-scoped bundle and its persistable token hash.

    Returns the bundle (model env + S3 prefix the worker uses) and the token
    hash (the revocable handle persisted on the row). The raw token is hashed
    and discarded -- it is not held by the worker today (in-process verification
    is not wired; the worker writes state to Postgres directly).
    """
    _, token_hash = mint_token()
    bundle = JobCredentialBundle(
        model_env=scoped_model_env(agent=agent, model=model, settings=settings),
        s3_write_prefix=s3_write_prefix_for(trial_id),
        expires_at=now + timedelta(seconds=ttl_seconds),
    )
    return bundle, token_hash
