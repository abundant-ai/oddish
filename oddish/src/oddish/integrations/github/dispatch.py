"""
GitHub repository_dispatch emitter for experiment completion.

Fires a single `POST /repos/{owner}/{repo}/dispatches` event when every
task linked to an experiment reaches a terminal state. Harbor-forge's
`oddish-complete` workflow consumes this event and renders the PR
results comment without polling.

------------------------------------------------------------------------
Wire format (must match harbor-forge's
`.github/workflows/oddish-complete.yml` receiver):

    POST /repos/{owner}/{repo}/dispatches
    {
      "event_type": "oddish-complete",
      "client_payload": {
        "experiment_id":   "<id>",
        "experiment_name": "<name>",
        "experiment_url":  "https://www.oddish.app/experiments/<id>",
        "public_url":      "https://www.oddish.app/share/<token>"  # or ""
        "pr":              "<pr_number>",
        "tier":            "<tier>"
      }
    }

Harbor-forge embeds, inside the `--github-meta` JSON it passes to
`oddish run`, a `dispatch` sub-key:

    {"pr":"...", "sha":"...", "tier":"full", "run_id":"...",
     "cap_min":"...",
     "dispatch":{"event_type":"oddish-complete","repo":"abundant-ai/harbor-forge-v2"}}

That blob is persisted as a JSON string on `TaskModel.tags["github_meta"]`
via the existing CLI/sweep plumbing (see GitHubMeta.from_tags) and is
read back out here.

------------------------------------------------------------------------
Auth model (v1 — single-tenant):

    GITHUB_DISPATCH_TOKEN          fine-grained PAT scoped ONLY to the
                                   target repo with Contents: read/write
                                   (the `/dispatches` endpoint needs
                                   Contents:write).
    GITHUB_DISPATCH_ALLOWED_REPOS  comma-separated allowlist of
                                   "<owner>/<repo>" values. We refuse to
                                   POST if `github_meta.dispatch.repo`
                                   isn't on this list — belt-and-
                                   suspenders even though the PAT is
                                   repo-scoped.

Migration path to multi-tenant GA: replace the env-PAT lookup in
`_get_dispatch_token_for_repo()` with a GitHub App installation token
minted per-org. Schema:

    orgs.github_app_installation_id  INTEGER NULL

The org owning the experiment is resolved from
`ExperimentModel.org_id`. The dispatch token is a short-lived
(`exp <= 10min`) installation token whose scope is locked to the org's
installation repos — this removes the need for the allowlist entirely
and lets each tenant configure their own dispatch targets via standard
GitHub App permissions.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
from dataclasses import dataclass
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from oddish.db import (
    ExperimentModel,
    TaskModel,
    TaskStatus,
    get_session,
    task_experiments,
    utcnow,
)

logger = logging.getLogger(__name__)

DASHBOARD_URL = os.getenv("ODDISH_DASHBOARD_URL", "https://www.oddish.app")
GITHUB_API_BASE = "https://api.github.com"

# Terminal task states. Mirrors the predicate in
# oddish/src/oddish/db/models.py:78-86. Trials and analyses can still be
# in flight after a task hits COMPLETED only via the cleanup/cancel
# paths, and verdict_handler.py:212,224 sets COMPLETED for both verdict
# SUCCESS and FAILED, so this set covers both success and failure.
_TERMINAL_TASK_STATUSES = frozenset({TaskStatus.COMPLETED, TaskStatus.FAILED})

# Retry budget for transient POST /dispatches failures (5xx, 429,
# network). 0.5s, 1s, 2s, 4s — total <= ~8s, well under the
# WORKER_TIMEOUT_SECONDS budget.
_RETRY_DELAYS_SECONDS: tuple[float, ...] = (0.5, 1.0, 2.0, 4.0)


@dataclass(frozen=True)
class DispatchTarget:
    """Parsed `tags["github_meta"]["dispatch"]` block."""

    event_type: str
    repo: str  # "<owner>/<name>"

    @classmethod
    def from_task_tags(cls, tags: dict | None) -> "DispatchTarget | None":
        if not tags:
            return None
        raw = tags.get("github_meta")
        if not raw:
            return None
        if isinstance(raw, str):
            try:
                meta = json.loads(raw)
            except json.JSONDecodeError:
                return None
        elif isinstance(raw, dict):
            meta = raw
        else:
            return None
        dispatch = meta.get("dispatch")
        if not isinstance(dispatch, dict):
            return None
        event_type = dispatch.get("event_type")
        repo = dispatch.get("repo")
        if not isinstance(event_type, str) or not event_type:
            return None
        if not isinstance(repo, str) or "/" not in repo:
            return None
        return cls(event_type=event_type.strip(), repo=repo.strip())


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------


def _get_dispatch_token_for_repo(repo: str) -> str | None:
    """Return the bearer token used to POST /repos/{repo}/dispatches.

    Prefers a dedicated GITHUB_DISPATCH_TOKEN, but falls back to the same
    GITHUB_TOKEN oddish already uses for its PR-comment notifier — so NO
    new credential is required as long as that token can write Contents on
    the target repo. (repository_dispatch needs Contents:write; comment
    posting only needs Issues:write, so a comments-only fine-grained token
    must have Contents:write added. A classic `repo`-scoped token already
    has both.)

    Migrate to a per-org GitHub App installation token for multi-tenant
    GA — the call sites already pass `repo`, so the signature is
    forward-compatible.
    """
    token = os.getenv("GITHUB_DISPATCH_TOKEN") or os.getenv("GITHUB_TOKEN")
    if not token:
        return None
    return token.strip()


def _allowed_repos() -> set[str]:
    raw = os.getenv("GITHUB_DISPATCH_ALLOWED_REPOS", "")
    return {entry.strip() for entry in raw.split(",") if entry.strip()}


def _is_repo_allowed(repo: str) -> bool:
    allowlist = _allowed_repos()
    if not allowlist:
        # Fail closed: an unset allowlist means no repo is allowed.
        # Operators must explicitly opt repos in.
        return False
    return repo in allowlist


# ---------------------------------------------------------------------------
# Experiment terminality check
# ---------------------------------------------------------------------------


async def _experiment_is_fully_terminal(
    session, experiment_id: str
) -> tuple[bool, list[TaskModel]]:
    """Return (all_terminal, tasks). Honours the soft-delete filter."""
    result = await session.execute(
        select(TaskModel)
        .join(task_experiments, task_experiments.c.task_id == TaskModel.id)
        .where(
            task_experiments.c.experiment_id == experiment_id,
            task_experiments.c.deleted_at.is_(None),
        )
    )
    tasks = list(result.scalars().all())
    if not tasks:
        # An experiment with zero linked tasks is degenerate; refuse to
        # fire so a manual `oddish run --experiment` against a fresh id
        # can't trigger a no-op dispatch.
        return False, tasks
    for task in tasks:
        if task.status not in _TERMINAL_TASK_STATUSES:
            return False, tasks
    return True, tasks


# ---------------------------------------------------------------------------
# Idempotency claim
# ---------------------------------------------------------------------------


async def _claim_dispatch(
    session,
    *,
    experiment_id: str,
    event_type: str,
    target_repo: str,
) -> bool:
    """Race-free single-fire guard.

    Returns True iff THIS caller is the first to insert the row. Other
    callers (re-fired verdict hooks, cleanup sweeps, manual retries) get
    False and skip the POST.
    """
    stmt = (
        pg_insert(_experiment_dispatch_log_table())
        .values(
            experiment_id=experiment_id,
            event_type=event_type,
            target_repo=target_repo,
            dispatched_at=utcnow(),
        )
        .on_conflict_do_nothing(index_elements=["experiment_id"])
        .returning(_experiment_dispatch_log_table().c.experiment_id)
    )
    result = await session.execute(stmt)
    row = result.first()
    await session.commit()
    return row is not None


def _experiment_dispatch_log_table():
    # Defined lazily so import cost stays out of cold start. The model is
    # intentionally not declared on Base — the migration owns its schema
    # and no ORM read path uses it (the INSERT above is raw enough that
    # SQLAlchemy core suffices).
    from sqlalchemy import Column, DateTime, MetaData, String, Table

    global _CACHED_TABLE
    try:
        return _CACHED_TABLE  # type: ignore[name-defined]
    except NameError:
        pass
    metadata = MetaData()
    _CACHED_TABLE = Table(
        "experiment_dispatch_log",
        metadata,
        Column("experiment_id", String(64), primary_key=True),
        Column("event_type", String(128), nullable=False),
        Column("target_repo", String(255), nullable=False),
        Column("dispatched_at", DateTime(timezone=True), nullable=False),
    )
    return _CACHED_TABLE


# ---------------------------------------------------------------------------
# Payload assembly
# ---------------------------------------------------------------------------


def _experiment_public_url(experiment: ExperimentModel) -> str:
    if experiment.public_token:
        return f"{DASHBOARD_URL}/share/{experiment.public_token}"
    return ""


def _resolve_dispatch_target(tasks: list[TaskModel]) -> DispatchTarget | None:
    """Walk the experiment's tasks and return the first valid dispatch block.

    All tasks created by one harbor-forge submission share identical
    github_meta (set once in run-full-trials.yml:309-352), so reading from
    any task is sufficient. We iterate so that a partially-migrated
    experiment (one task lacking the new sub-key) still resolves.
    """
    for task in tasks:
        target = DispatchTarget.from_task_tags(task.tags)
        if target:
            return target
    return None


def _resolve_meta_field(tasks: list[TaskModel], key: str) -> str:
    """Read a top-level field (pr, tier, ...) from github_meta of any task."""
    for task in tasks:
        tags = task.tags or {}
        raw = tags.get("github_meta")
        if isinstance(raw, str):
            try:
                meta = json.loads(raw)
            except json.JSONDecodeError:
                continue
        elif isinstance(raw, dict):
            meta = raw
        else:
            continue
        value = meta.get(key)
        if value is None:
            continue
        return str(value)
    return ""


def _build_client_payload(
    *,
    experiment: ExperimentModel,
    tasks: list[TaskModel],
) -> dict[str, Any]:
    return {
        "experiment_id": experiment.id,
        "experiment_name": experiment.name,
        "experiment_url": f"{DASHBOARD_URL}/experiments/{experiment.id}",
        "public_url": _experiment_public_url(experiment),
        "pr": _resolve_meta_field(tasks, "pr"),
        "tier": _resolve_meta_field(tasks, "tier"),
    }


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------


async def _post_dispatch(
    *,
    token: str,
    target_repo: str,
    event_type: str,
    client_payload: dict[str, Any],
) -> bool:
    """POST with bounded exponential backoff on 5xx/429/network errors.

    Returns True if GitHub accepted the dispatch (HTTP 204). Logs and
    returns False otherwise. The caller has already idempotency-claimed
    the row, so a False return means the dispatch is LOST — see
    cleanup.py's safety-net comment below.
    """
    body = {"event_type": event_type, "client_payload": client_payload}
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "oddish-dispatcher",
    }
    url = f"{GITHUB_API_BASE}/repos/{target_repo}/dispatches"

    async with httpx.AsyncClient(timeout=15.0) as client:
        last_status: int | None = None
        last_text: str | None = None
        for attempt, delay in enumerate((0.0, *_RETRY_DELAYS_SECONDS)):
            if delay:
                jitter = delay * (0.8 + random.random() * 0.4)
                await asyncio.sleep(jitter)
            try:
                response = await client.post(url, json=body, headers=headers)
            except httpx.HTTPError as exc:
                logger.warning(
                    "dispatch attempt %d to %s failed (network): %s",
                    attempt,
                    target_repo,
                    exc,
                )
                continue
            last_status = response.status_code
            last_text = response.text[:500]
            if response.status_code == 204:
                logger.info(
                    "dispatched event_type=%s to %s payload=%s",
                    event_type,
                    target_repo,
                    json.dumps(client_payload),
                )
                return True
            if response.status_code in (429,) or 500 <= response.status_code < 600:
                logger.warning(
                    "dispatch attempt %d to %s got %s; retrying",
                    attempt,
                    target_repo,
                    response.status_code,
                )
                continue
            # 4xx (non-429) is terminal — bad token, repo permissions,
            # missing repo, malformed payload. Log loudly and bail.
            logger.error(
                "dispatch to %s rejected: HTTP %s body=%s",
                target_repo,
                response.status_code,
                last_text,
            )
            return False

        logger.error(
            "dispatch to %s exhausted retries: last_status=%s body=%s",
            target_repo,
            last_status,
            last_text,
        )
        return False


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------


async def maybe_dispatch_experiment_complete(experiment_id: str) -> bool:
    """Fire repository_dispatch iff every task in the experiment is terminal.

    Idempotent: a successful fire writes a row keyed by `experiment_id`
    into `experiment_dispatch_log`; subsequent invocations (re-run
    verdicts, cleanup sweeps, manual retries) short-circuit on the
    `ON CONFLICT DO NOTHING`. Returns True iff THIS call posted the
    dispatch.
    """
    async with get_session() as session:
        experiment = await session.get(ExperimentModel, experiment_id)
        if experiment is None:
            logger.debug("dispatch: experiment %s not found", experiment_id)
            return False

        all_terminal, tasks = await _experiment_is_fully_terminal(
            session, experiment_id
        )
        if not all_terminal:
            return False

        target = _resolve_dispatch_target(tasks)
        if target is None:
            # No harbor-forge-shaped dispatch block on any task — this
            # experiment wasn't submitted with a dispatch target. That's
            # fine; CLI users get the existing PR-comment notifier.
            return False

        if not _is_repo_allowed(target.repo):
            logger.warning(
                "dispatch refused: repo %s not in GITHUB_DISPATCH_ALLOWED_REPOS",
                target.repo,
            )
            return False

        token = _get_dispatch_token_for_repo(target.repo)
        if not token:
            logger.warning(
                "dispatch refused: GITHUB_DISPATCH_TOKEN not configured "
                "(experiment=%s repo=%s)",
                experiment_id,
                target.repo,
            )
            return False

        # Claim BEFORE the network call so concurrent verdict hooks for
        # different tasks of the same experiment don't double-fire.
        claimed = await _claim_dispatch(
            session,
            experiment_id=experiment_id,
            event_type=target.event_type,
            target_repo=target.repo,
        )
        if not claimed:
            logger.info(
                "dispatch skipped: experiment %s already dispatched",
                experiment_id,
            )
            return False

        payload = _build_client_payload(experiment=experiment, tasks=tasks)

    # Network call happens OUTSIDE the DB session. The notifier module
    # (notifier.py:244-248) documents the size-1 pool deadlock that
    # nesting causes.
    posted = await _post_dispatch(
        token=token,
        target_repo=target.repo,
        event_type=target.event_type,
        client_payload=payload,
    )
    if not posted:
        # The claim row is sticky on purpose: the cleanup sweep
        # (oddish/workers/queue/cleanup.py) can scan terminal
        # experiments with claim rows where `dispatched_at` is older
        # than N minutes and the receiver workflow never updated the
        # sticky PR comment, and force a manual re-post via the
        # existing oddish-poll workflow_dispatch path. We do NOT delete
        # the claim row on POST failure because the alternative — auto-
        # delete and let the next verdict hook retry — risks an
        # unbounded duplicate-dispatch storm if GitHub is degraded.
        return False
    return True


async def maybe_dispatch_for_task(task_id: str) -> None:
    """Wrapper invoked from the verdict post-success hook.

    `worker_job_single_job.py:546-555` calls the hook with `subject_id`
    (the task_id for VERDICT jobs). We walk M2M to find every
    experiment this task belongs to and run the all-terminal check
    against each — a task can live in multiple experiments per the
    M2M comment at db/models.py:189-217, but in practice harbor-forge
    submissions are 1:1.
    """
    try:
        async with get_session() as session:
            task = await session.get(TaskModel, task_id)
            if task is None:
                return
            experiment_ids = [exp.id for exp in (task.experiments or [])]
        for experiment_id in experiment_ids:
            try:
                await maybe_dispatch_experiment_complete(experiment_id)
            except Exception as exc:
                # Hook failures must never fail the verdict — the
                # post-success hook contract (worker_job_single_job.py:
                # 551-555) already swallows exceptions, but we log
                # here too for a tighter blast radius.
                logger.exception(
                    "dispatch check for experiment %s failed: %s",
                    experiment_id,
                    exc,
                )
    except Exception as exc:
        logger.exception("dispatch check for task %s failed: %s", task_id, exc)
