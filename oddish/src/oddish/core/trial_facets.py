"""Write-through + rebuild maintenance for the ``trial_facets`` vocabulary.

The task browser's filter dropdowns need the distinct facet values an org has
run (agent, model, provider, ...). Deriving them from ``trials`` on every
facets request scanned the whole org history seven times per page view;
``trial_facets`` (see :class:`oddish.db.models.TrialFacetModel`) stores the
tiny vocabulary instead. Two writers keep it correct:

* :func:`record_trial_facets` — write-through at trial creation, so a value
  is filterable the moment its first trial is queued or imported. Additive
  only, idempotent (``ON CONFLICT DO NOTHING``), and best-effort by
  contract: it must never abort the trial transaction it rides in.
* :func:`rebuild_trial_facets_core` — the periodic sweep's mark-and-prune
  reconciliation from one grouped scan over live trials. This is the
  exactness authority: it adds anything write-through missed (e.g.
  ``harbor_stage`` progressing during execution, classifications written by
  analyzers) and prunes values whose last trial was deleted, superseded, or
  left behind by a version bump — without ever holding locks a trial writer
  would wait on.

Freshness contract: spec facets (agent/model/pair/provider/environment) are
instant; stage/classification additions and every removal converge within one
sweep interval (plus the prune grace). Between sweeps the vocabulary may
over-include — an option whose trials just vanished yields an empty (not
wrong) filter result.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any, Iterable, Sequence

from sqlalchemy import delete, func, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import AsyncSession

from oddish.db.models import TaskModel, TrialFacetModel, TrialModel
from oddish.db.pg_errors import is_missing_table

logger = logging.getLogger(__name__)

# Facet kinds served by ``browse_task_facets_core``. ``agent_model`` is the
# only pair kind (value=agent, value_2=model-or-''); the rest are scalar.
TRIAL_FACET_KINDS = (
    "agent",
    "model",
    "agent_model",
    "provider",
    "environment",
    "harbor_stage",
    "analysis_classification",
)

_VALUE_WIDTH = 160  # trial_facets value/value_2 column width


def _clip(value: str) -> str:
    return value[:_VALUE_WIDTH]


def facet_rows_for_trial(
    *,
    org_id: str | None,
    agent: str | None,
    model: str | None = None,
    provider: str | None = None,
    environment: str | None = None,
    harbor_stage: str | None = None,
    is_probe: bool = False,
) -> set[tuple[str, str, str, str]]:
    """Vocabulary rows one trial spec contributes, as (org, kind, value, value_2).

    Probe trials contribute nothing (facets exclude probes), and org-less
    trials (the single-tenant standalone path, which has no facets endpoint)
    contribute nothing.
    """
    if is_probe or org_id is None:
        return set()
    rows: set[tuple[str, str, str, str]] = set()
    if agent:
        rows.add((org_id, "agent", _clip(agent), ""))
        rows.add((org_id, "agent_model", _clip(agent), _clip(model or "")))
    if model:
        rows.add((org_id, "model", _clip(model), ""))
    if provider:
        rows.add((org_id, "provider", _clip(provider), ""))
    if environment:
        rows.add((org_id, "environment", _clip(environment), ""))
    if harbor_stage:
        rows.add((org_id, "harbor_stage", _clip(harbor_stage), ""))
    return rows


def facet_rows_for_trial_dicts(
    trials: Iterable[dict[str, Any]],
) -> set[tuple[str, str, str, str]]:
    """Vocabulary rows for a batch of queue-shaped trial dicts."""
    rows: set[tuple[str, str, str, str]] = set()
    for t in trials:
        rows |= facet_rows_for_trial(
            org_id=t.get("org_id"),
            agent=t.get("agent"),
            model=t.get("model"),
            provider=t.get("provider"),
            environment=t.get("environment"),
            is_probe=bool(t.get("is_probe")),
        )
    return rows


async def record_trial_facets(
    session: AsyncSession, rows: Sequence[tuple[str, str, str, str]] | set
) -> None:
    """Idempotently add vocabulary rows in the caller's transaction.

    An auxiliary derived-state write must never abort the primary transaction
    (trial submission / import). Code deploys and migrations land
    independently, so a missing ``trial_facets`` table is the realistic
    deploy-before-migrate window: the savepoint keeps the caller's
    transaction usable and the migration backfill recovers anything skipped.
    Every other failure is a real bug and propagates — same shape as the
    quotas/costs missing-table tolerance.
    """
    if not rows:
        return
    try:
        async with session.begin_nested():
            await session.execute(
                pg_insert(TrialFacetModel).on_conflict_do_nothing(),
                [
                    {"org_id": org, "kind": kind, "value": value, "value_2": value_2}
                    for org, kind, value, value_2 in sorted(rows)
                ],
            )
    except ProgrammingError as exc:
        if not is_missing_table(exc):
            raise
        logger.warning(
            "trial_facets missing (schema not migrated yet); vocabulary "
            "write-through skipped",
            exc_info=True,
        )


async def rebuild_trial_facets_core(
    session: AsyncSession, *, prune_grace_seconds: float = 60.0
) -> tuple[int, int]:
    """Reconcile the vocabulary from one grouped scan over live trials.

    Mark-and-prune, never wholesale replace, so trial writers never wait on
    the scan: derive lock-free from the same trial population the browse
    filters match — non-probe, non-superseded, on each task's current
    version, org-scoped (the soft-delete listener adds ``deleted_at IS
    NULL`` for both tables) — then upsert every derived row with a fresh
    ``written_at`` (row locks held for milliseconds), then prune rows
    neither this upsert nor a recent write-through asserted alive.
    ``prune_grace_seconds`` spares write-throughs whose transaction was in
    flight when the scan-start timestamp ``t0`` was read (their
    ``written_at`` predates ``t0``).

    Accepted epsilon for zero writer contention: a key that went stale this
    cycle and is revived by a trial landing mid-scan can be pruned and
    reappears next cycle. Returns ``(org_count, row_count)``.
    """
    t0 = (await session.execute(select(func.now()))).scalar_one()
    scan = (
        select(
            TrialModel.org_id,
            TrialModel.agent,
            TrialModel.model,
            TrialModel.provider,
            TrialModel.environment,
            TrialModel.harbor_stage,
            TrialModel.analysis["classification"].astext,
        )
        .join(TaskModel, TaskModel.id == TrialModel.task_id)
        .where(
            TrialModel.org_id.isnot(None),
            TrialModel.is_probe.isnot(True),
            TrialModel.superseded_by_trial_id.is_(None),
            TrialModel.task_version_id == TaskModel.current_version_id,
        )
        # GROUP BY select-list position: the JSONB extraction binds its key
        # as a parameter, so repeating the expression would make Postgres see
        # two different binds ($1 vs $2) and reject the grouping.
        .group_by(*[text(str(i)) for i in range(1, 8)])
    )
    rows: set[tuple[str, str, str, str]] = set()
    for org, agent, model, provider, environment, stage, classification in (
        await session.execute(scan)
    ).all():
        rows |= facet_rows_for_trial(
            org_id=org,
            agent=agent,
            model=model,
            provider=provider,
            environment=environment,
            harbor_stage=stage,
        )
        if classification:
            rows.add((org, "analysis_classification", _clip(classification), ""))

    if rows:
        await session.execute(
            pg_insert(TrialFacetModel).on_conflict_do_update(
                index_elements=["org_id", "kind", "value", "value_2"],
                set_={"written_at": func.now()},
            ),
            [
                {"org_id": org, "kind": kind, "value": value, "value_2": value_2}
                for org, kind, value, value_2 in sorted(rows)
            ],
        )
    await session.execute(
        delete(TrialFacetModel).where(
            TrialFacetModel.written_at < t0 - timedelta(seconds=prune_grace_seconds)
        )
    )
    return len({row[0] for row in rows}), len(rows)
