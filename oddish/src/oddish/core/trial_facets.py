"""Write-through + rebuild maintenance for the ``trial_facets`` vocabulary.

The task browser's filter dropdowns need the distinct facet values an org has
run (agent, model, provider, ...). Deriving them from ``trials`` on every
facets request scanned the whole org history seven times per page view;
``trial_facets`` (see :class:`oddish.db.models.TrialFacetModel`) stores the
tiny vocabulary instead. Two writers keep it correct:

* :func:`record_trial_facets` — write-through at trial creation, so a value
  is filterable the moment its first trial is queued or imported. Additive
  only, idempotent (``ON CONFLICT DO NOTHING``).
* :func:`rebuild_trial_facets_core` — the periodic sweep's wholesale rebuild
  from one grouped scan over live trials. This is the exactness authority: it
  adds anything write-through missed (e.g. ``harbor_stage`` progressing
  during execution, classifications written by analyzers) and drops values
  whose last trial was deleted, superseded, or left behind by a version bump.

Freshness contract: spec facets (agent/model/pair/provider/environment) are
instant; stage/classification additions and every removal converge within one
sweep interval. Between sweeps the vocabulary may over-include — an option
whose trials just vanished yields an empty (not wrong) filter result.
"""

from __future__ import annotations

from typing import Any, Iterable, Sequence

from sqlalchemy import delete, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from oddish.db.models import TaskModel, TrialFacetModel, TrialModel

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
    """Idempotently add vocabulary rows in the caller's transaction."""
    if not rows:
        return
    await session.execute(
        pg_insert(TrialFacetModel).on_conflict_do_nothing(),
        [
            {"org_id": org, "kind": kind, "value": value, "value_2": value_2}
            for org, kind, value, value_2 in sorted(rows)
        ],
    )


async def rebuild_trial_facets_core(session: AsyncSession) -> tuple[int, int]:
    """Rebuild the whole vocabulary from one grouped scan over live trials.

    Scans the same trial population the browse filters match — non-probe,
    non-superseded, on each task's current version, org-scoped (the
    soft-delete listener adds ``deleted_at IS NULL`` for both tables) — as a
    single ``GROUP BY`` over every facet column, then replaces ``trial_facets``
    wholesale in the caller's transaction. Readers see the old vocabulary
    until commit. Returns ``(org_count, row_count)``.
    """
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

    await session.execute(delete(TrialFacetModel))
    if rows:
        await session.execute(
            pg_insert(TrialFacetModel),
            [
                {"org_id": org, "kind": kind, "value": value, "value_2": value_2}
                for org, kind, value, value_2 in sorted(rows)
            ],
        )
    return len({row[0] for row in rows}), len(rows)
