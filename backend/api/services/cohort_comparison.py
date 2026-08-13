"""Resolve, compare and cache the successful-vs-failing cohort comparison."""
from __future__ import annotations

import asyncio
import hashlib
from collections import defaultdict
from typing import MutableMapping

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from oddish.blocks.analyzer.analyzer_block import AnalyzerType
from oddish.db.models import AnalyzerBlockModel, JobStatus, TrialModel

# Below three trials per side the comparison is anecdote, not evidence.
MIN_COHORT = 3

SUCCESS_CLASS = "GOOD_SUCCESS"
FAILURE_CLASS = "GOOD_FAILURE"


def cohort_hash(success_ids: list[str], failure_ids: list[str]) -> str:
    """Identity of a cohort pair.

    Sorted so trial ordering does not churn the hash, and the two sides are
    separated so moving a trial between cohorts changes it.
    """
    payload = "|".join(sorted(success_ids)) + "//" + "|".join(sorted(failure_ids))
    return hashlib.sha256(payload.encode()).hexdigest()[:32]


async def _summaries_for(session: AsyncSession, trial_ids: list[str]) -> dict:
    """Latest SUCCESS trajectory summary per trial, in one DISTINCT ON pass."""
    if not trial_ids:
        return {}
    rows = (
        await session.execute(
            select(AnalyzerBlockModel.analyzer_id, AnalyzerBlockModel.output)
            .where(
                AnalyzerBlockModel.analyzer_id.in_(trial_ids),
                AnalyzerBlockModel.type == AnalyzerType.TRAJECTORY_SUMMARY.value,
                AnalyzerBlockModel.status == JobStatus.SUCCESS,
            )
            .order_by(
                AnalyzerBlockModel.analyzer_id,
                AnalyzerBlockModel.created_at.desc(),
            )
            .distinct(AnalyzerBlockModel.analyzer_id)
        )
    ).all()
    return {r[0]: (r[1] or {}) for r in rows}


async def resolve_cohorts(
    session: AsyncSession, task_version_id: str
) -> tuple[list[dict], list[dict]]:
    """Return (successful, failing) trials with their component streams.

    Classification lives in the ``analysis`` JSONB column, matching the filter
    in oddish/src/oddish/core/endpoints/tasks_query.py:1509.
    """
    out: dict[str, list[dict]] = {SUCCESS_CLASS: [], FAILURE_CLASS: []}
    for cls in (SUCCESS_CLASS, FAILURE_CLASS):
        rows = (
            await session.execute(
                select(
                    TrialModel.id,
                    TrialModel.total_steps,
                    TrialModel.trajectory_summary,
                    # Which model produced the run. Attribution is computed
                    # from these in code, never asked of the LLM: "which model
                    # did better" is a counting question, and a fabricated
                    # answer to it would be indistinguishable from a real one.
                    TrialModel.model,
                ).where(
                    TrialModel.task_version_id == task_version_id,
                    TrialModel.is_probe.is_(False),
                    # A retry leaves its superseded attempt in the table, still
                    # classified. Listings and verdict aggregation exclude those
                    # (see oddish/core/quotas.py), so cohorts must too, or the
                    # comparison and its membership hash count abandoned runs.
                    TrialModel.superseded_by_trial_id.is_(None),
                    TrialModel.analysis["classification"].astext == cls,
                )
            )
        ).all()
        ids = [r[0] for r in rows]
        total_steps = {r[0]: r[1] for r in rows}
        models = {r[0]: r[3] for r in rows}
        # Prefer the mirror on the trial row. summarize_trajectory writes every
        # summary to trials.trajectory_summary as well as analyzer_blocks, and
        # the mirror is what the sibling QA surfaces read -- post-trial reads
        # trials.analysis, pre-trial reads task_versions.pre_trial. Reading
        # analyzer_blocks alone made this the only feature that cannot work on
        # a preview deploy, because preview_seed copies trials but not
        # analyzer_blocks. Fall back to the block for rows written before the
        # mirror existed.
        mirrored = {r[0]: r[2] for r in rows if r[2]}
        missing = [t for t in ids if t not in mirrored]
        summaries = {**(await _summaries_for(session, missing)), **mirrored}
        for tid in ids:
            comps = [
                c
                for c in ((summaries.get(tid) or {}).get("components") or [])
                if isinstance(c, dict) and c.get("step_ids")
            ]
            # A trial with no summary contributes nothing citable.
            if not comps:
                continue
            # Coverage guards against the summariser's long-run defect: one
            # 2,940-step trial yields ~20 covered steps, so a comparison can
            # rest on far thinner evidence than its trial count suggests.
            #
            # The denominator is the trial's real length. Dividing by the
            # highest cited step instead would score a summary that covers a
            # dense early band of a long run at close to 100%, which is exactly
            # backwards. total_steps is nullable, so fall back to the cited
            # span and accept the optimism rather than dropping the trial.
            all_ids = {i for c in comps for i in c["step_ids"]}
            span = max(all_ids)
            denominator = total_steps.get(tid) or span
            out[cls].append(
                {
                    "trial_id": tid,
                    "model": models.get(tid),
                    "components": comps,
                    "covered_steps": len(all_ids),
                    "span": span,
                    "total_steps": total_steps.get(tid),
                    "coverage": (
                        round(min(len(all_ids) / denominator, 1.0), 3)
                        if denominator
                        else 0.0
                    ),
                }
            )
    return out[SUCCESS_CLASS], out[FAILURE_CLASS]


def _span(step_ids) -> tuple[int, int] | None:
    """(first, last) of a component's steps -- what the prompt actually shows.

    The prompt renders each component as a compact ``[min-max]`` range, since a
    component can span hundreds of steps and printing them all would bloat the
    prompt past the point where it fits. Matching on the exact list would
    therefore reject every citation the model could possibly write, so both
    sides compare the span instead.
    """
    ids = [i for i in (step_ids or []) if isinstance(i, int)]
    if not ids:
        return None
    return (min(ids), max(ids))


def _index(trials: list[dict]) -> dict[tuple, str]:
    """(trial_id, component, step span) -> the component's stored summary."""
    out: dict[tuple, str] = {}
    for t in trials:
        for c in t.get("components") or []:
            span = _span(c.get("step_ids"))
            if span is None:
                continue
            key = (t["trial_id"], c.get("trajectory_component"), span)
            out[key] = (c.get("summary") or "").strip()
    return out


def validate_evidence(
    output: dict, successful: list[dict], failing: list[dict]
) -> tuple[dict, dict]:
    """Drop citations that do not resolve against the stored summaries.

    This repo has had an analyzer fabricate trial ids at scale, so citations
    are verified rather than trusted. Evidence must name a component that
    exists, on the side of the comparison it was cited under, covering the
    same step span, with the stored summary text unaltered.

    Called from ``CohortComparisonBlock.to_output`` so the validated shape is
    what gets persisted -- validating after ``block.run()`` returns would leave
    every later cache hit serving the raw, unvalidated model output.
    """
    index = {"successful": _index(successful), "failing": _index(failing)}
    drops = {"evidence": 0, "observations": 0, "categories": 0}

    kept_categories = []
    for cat in output.get("categories") or []:
        kept_sides: dict[str, list] = {}
        for side in ("successful", "failing"):
            kept_obs = []
            for obs in cat.get(side) or []:
                kept_ev = []
                for ev in obs.get("evidence") or []:
                    span = _span(ev.get("step_ids"))
                    key = (
                        ev.get("trial_id"),
                        ev.get("trajectory_component"),
                        span,
                    )
                    stored = None if span is None else index[side].get(key)
                    if stored is not None and stored == (ev.get("quote") or "").strip():
                        kept_ev.append(ev)
                    else:
                        drops["evidence"] += 1
                if kept_ev:
                    kept_obs.append({**obs, "evidence": kept_ev})
                else:
                    drops["observations"] += 1
            kept_sides[side] = kept_obs
        if kept_sides["successful"] or kept_sides["failing"]:
            kept_categories.append({**cat, **kept_sides})
        else:
            drops["categories"] += 1

    return {**output, "categories": kept_categories}, drops


def is_stale(
    block_metadata: dict | None,
    *,
    current_hash: str,
    schema_version: int,
    task_version_id: str,
) -> bool:
    """Freshness keys on the cohort hash, the schema version, AND the task
    version.

    Keying on schema_version alone is the bug that left stored trajectory
    summaries serving a retired taxonomy forever. The task-version check is
    belt-and-braces on top of the hash (trials belong to exactly one version,
    so the hash already changes across versions) -- kept explicit rather than
    relied on implicitly, since block rows are now looked up by the stable
    ``task_id`` rather than the version id.
    """
    if not block_metadata:
        return True
    return (
        block_metadata.get("cohort_hash") != current_hash
        or block_metadata.get("schema_version") != schema_version
        or block_metadata.get("task_version_id") != task_version_id
    )


# Per-(task_id, task_version_id) locks so two concurrent misses don't both
# fire an LLM call. Process-local, same tradeoff as summarize_trajectory's
# _GEN_LOCKS: cross-container racing costs at most a few duplicate
# generations, and the writes are idempotent.
_GEN_LOCKS: MutableMapping[tuple[str, str], asyncio.Lock] = defaultdict(asyncio.Lock)


async def _load_fresh_comparison(
    session: AsyncSession,
    *,
    task_id: str,
    task_version_id: str,
    current_hash: str,
    schema_version: int,
) -> dict | None:
    """The latest fresh SUCCESS cohort_comparison block's output, or None.

    Rows carry the stable ``task_id`` (``TaskModel.id``) because cost
    attribution resolves against ``TaskModel``, so the version is filtered on
    inside ``block_metadata``. That filter is load-bearing, not decoration:
    taking the newest row for the *task* lets a comparison of another version
    shadow this version's fresh one, and switching between two versions then
    regenerates both on every view.
    """
    row = (
        await session.execute(
            select(AnalyzerBlockModel)
            .where(
                AnalyzerBlockModel.task_id == task_id,
                AnalyzerBlockModel.type == AnalyzerType.COHORT_COMPARISON.value,
                AnalyzerBlockModel.status == JobStatus.SUCCESS,
                AnalyzerBlockModel.block_metadata["task_version_id"].astext
                == task_version_id,
            )
            .order_by(AnalyzerBlockModel.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if row is not None and not is_stale(
        row.block_metadata,
        current_hash=current_hash,
        schema_version=schema_version,
        task_version_id=task_version_id,
    ):
        return row.output
    return None


async def _end_read_transaction(session: AsyncSession) -> None:
    """Close the read transaction before anything slow.

    This generates on the read path, and a generation runs for minutes. Two
    things make holding the request's transaction across one a real fault
    rather than untidiness:

    * The connecting role carries ``idle_in_transaction_session_timeout`` of
      five minutes (``oddish.db.connection.apply_role_defaults``, pinned onto
      the role because Supavisor drops client-supplied server_settings), so a
      transaction left open across a long generation is terminated by Postgres
      and the request fails at commit -- even though ``AnalyzerBlock``
      persisted its SUCCESS row through a session of its own.
    * An API container's pool is two connections plus one overflow
      (``backend/endpoints.py``), and ``AnalyzerBlock`` needs one of them to
      write. Parking a third for the length of an LLM call is a large share of
      a small pool.

    Nothing is written through this session, so the commit only ends the
    transaction and returns the connection to the pool; the next statement
    checks one out again. ``expire_on_commit=False``, so already-loaded rows
    survive it.
    """
    await session.commit()


async def get_or_generate_comparison(
    session: AsyncSession,
    task_version_id: str,
    *,
    task_id: str,
    task_name: str,
    refresh: bool = False,
    triggered_by_user_id: str | None = None,
) -> dict | None:
    """Return the comparison, generating on miss. None when the gate fails.

    ``task_id`` must be the real ``TaskModel.id``: ``AnalyzerBlock``'s cost
    attribution (``_attribute_to_task``) looks it up against ``TaskModel``,
    and a task-version id never matches there -- it would silently attribute
    the spend to no org and no user. Version scoping instead lives in
    ``block_metadata["task_version_id"]``, checked by ``is_stale``.
    """
    from oddish.blocks.analyzer.analyzer_block import AnalyzerBlock, AnalyzerInput
    from oddish.blocks.analyzer.analyzer_llm_client import LLMClientType

    from api.services.blocks.analyzer.cohort import cohort_prompts as cp
    from api.services.blocks.analyzer.cohort.cohort_comparison_block import (
        SCHEMA_VERSION,
        CohortComparisonBlock,
        CohortComparisonOutput,
        CohortInput,
    )
    from api.services.summarize_trajectory import (
        SUMMARY_MAX_TOKENS,
        resolve_summary_model,
    )

    successful, failing = await resolve_cohorts(session, task_version_id)
    # One populated side is enough. Requiring both meant a task whose runs all
    # failed -- the case a reader most wants explained -- got silence, even
    # with ten classified failures on the table. What a cohort did is worth
    # reporting on its own; what it did *differently* just needs two.
    if max(len(successful), len(failing)) < MIN_COHORT:
        return None

    current = cohort_hash(
        [t["trial_id"] for t in successful], [t["trial_id"] for t in failing]
    )

    if not refresh:
        fresh = await _load_fresh_comparison(
            session,
            task_id=task_id,
            task_version_id=task_version_id,
            current_hash=current,
            schema_version=SCHEMA_VERSION,
        )
        if fresh is not None:
            return fresh

    # Before the lock, not just before the model call: waiting behind another
    # coroutine's generation is the same minutes-long wait, and a request that
    # queues there would otherwise hold its transaction open for the whole of
    # someone else's generation as well as its own.
    await _end_read_transaction(session)

    async with _GEN_LOCKS[(task_id, task_version_id)]:
        # Re-check inside the lock -- another coroutine may have generated
        # one while this one waited. A refresh deliberately skips this: it
        # was explicitly asked for a new comparison, and the one waiting in
        # front of it may be the stale one it wants replaced.
        if not refresh:
            fresh = await _load_fresh_comparison(
                session,
                task_id=task_id,
                task_version_id=task_version_id,
                current_hash=current,
                schema_version=SCHEMA_VERSION,
            )
            if fresh is not None:
                return fresh
            # The re-check opened a transaction of its own.
            await _end_read_transaction(session)

        model = resolve_summary_model()
        cb = CohortComparisonBlock(
            CohortInput(task_name=task_name, successful=successful, failing=failing),
            instructions_template=cp.load_cohort_prompt_template(),
        )
        block = AnalyzerBlock(
            analyzer_type=AnalyzerType.COHORT_COMPARISON,
            llm_client_type=LLMClientType.API,
            input=AnalyzerInput(input={"task_version_id": task_version_id}),
            prompt=cb.build_prompt(),
            task_id=task_id,
            block_metadata={
                "schema_version": SCHEMA_VERSION,
                "cohort_hash": current,
                "task_version_id": task_version_id,
            },
            output_transform=cb.to_output,
            model=model,
            # Same cap as the trajectory summary, for the same reason: the
            # default 4096 truncates the model mid-JSON, and a seven-category
            # two-sided comparison quoting stored summaries verbatim is well
            # past that. A ceiling, not a target -- billing is on tokens
            # actually generated.
            max_tokens=SUMMARY_MAX_TOKENS,
            response_format=CohortComparisonOutput,
            output_schema=CohortComparisonOutput.model_json_schema(),
            triggered_by_user_id=triggered_by_user_id,
        )
        # Citation validation and the thin-coverage list are applied inside
        # cb.to_output, so what run() returns is what was persisted.
        result = await block.run()
        return result.output
