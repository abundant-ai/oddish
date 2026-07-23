"""Offline harness core for the trajectory-summary path.

Pure logic -- scope resolution, cohort selection, per-trial record building --
with no Modal imports, so it is importable from tests. ``ops_summary_dump.py``
is the thin Modal wrapper that supplies production credentials.

Enters production at ``summarize_trajectory.generate()``'s construction site
(``build_summary_block``), NOT at ``get_or_generate_summary``: the latter
returns a cached block whose ``schema_version`` matches, which would hand back
a stale summary instead of exercising a revised taxonomy.
"""

from __future__ import annotations

import asyncio
import contextlib

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from oddish.core.helpers import _has_fetchable_trajectory
from oddish.db.models import TaskModel, TrialModel, experiment_trials

MAX_CONCURRENCY = 4


def validate_scope(
    *, trials: list[str] | None, task: str | None, experiment: str | None
) -> None:
    """Require exactly one scope. Raises before any query runs."""
    supplied = [name for name, value in
                (("--trials", trials), ("--task", task), ("--experiment", experiment))
                if value]
    if len(supplied) != 1:
        raise ValueError(
            f"Supply exactly one of --trials/--task/--experiment (got: {supplied or 'none'})"
        )


def filter_fetchable(rows, limit: int = 0) -> list:
    """Keep trials whose trajectory can actually be fetched, then apply limit.

    ``_has_fetchable_trajectory`` is a Python predicate, not a SQL condition --
    it also admits finished Grok Build trials that synthesize ATIF from
    grok-build.json. So the limit must be applied after filtering, or a cohort
    of N returns fewer than N.
    """
    kept = [t for t in rows if _has_fetchable_trajectory(t)]
    return kept[:limit] if limit else kept


async def resolve_cohort(
    session,
    *,
    trials: list[str] | None = None,
    task: str | None = None,
    experiment: str | None = None,
    limit: int = 0,
) -> list[TrialModel]:
    """Resolve a scope to an ordered, deterministic list of trials.

    Explicit ids are returned in the order given, unfiltered -- naming a probe
    trial is an intentional act. Task/experiment scopes exclude probes, order
    by trial id, and are then narrowed by ``filter_fetchable``.
    """
    validate_scope(trials=trials, task=task, experiment=experiment)
    stmt = select(TrialModel).options(selectinload(TrialModel.task))

    if trials:
        rows = (await session.execute(stmt.where(TrialModel.id.in_(trials)))).scalars().all()
        by_id = {t.id: t for t in rows}
        return [by_id[tid] for tid in trials if tid in by_id]

    if task:
        # Task names are the human-facing handle (unique per org); tasks.id is a
        # random primary key, so resolve through the tasks table.
        stmt = stmt.join(TaskModel, TaskModel.id == TrialModel.task_id).where(
            TaskModel.name == task
        )
    else:
        # Collection experiments gather existing trials into a new experiment
        # for viewing WITHOUT moving them (see experiment_trials' comment in
        # oddish/db/models.py), so trials.experiment_id still points at each
        # trial's home experiment -- membership is the union of both forms.
        # experiment_trials is a Core Table, not a mapped class, so the
        # soft-delete auto-filter (register_soft_delete_models, ORM-only)
        # never touches it; deleted_at is filtered explicitly here.
        member_ids = select(TrialModel.id).where(
            TrialModel.experiment_id == experiment
        ).union(
            select(experiment_trials.c.trial_id).where(
                experiment_trials.c.experiment_id == experiment,
                experiment_trials.c.deleted_at.is_(None),
            )
        )
        stmt = stmt.where(TrialModel.id.in_(member_ids))

    stmt = stmt.where(TrialModel.is_probe.is_(False)).order_by(TrialModel.id.asc())
    rows = (await session.execute(stmt)).scalars().all()
    return filter_fetchable(rows, limit=limit)


def failed_record(trial, task_context, *, model: str, error: str) -> dict:
    """Record for a trial that never got far enough to have a block.

    The harness contract is one record per attempted trial, so a failure with
    no block is still data. Fields are read defensively -- the very shapes that
    raise here are the ones likeliest to be missing attributes.
    """
    from api.services.summarize_trajectory import SCHEMA_VERSION

    task = getattr(trial, "task", None)
    return {
        "trial_id": getattr(trial, "id", None),
        "task_name": (
            getattr(task_context, "task_name", None)
            if task_context is not None
            else getattr(task, "name", None)
        ),
        "final_reward": (
            getattr(task_context, "final_reward", None)
            if task_context is not None
            else getattr(trial, "reward", None)
        ),
        "model": model,
        "schema_version": SCHEMA_VERSION,
        "status": "failed",
        "duration_s": None,
        "error": error,
        "prompt": None,
        "raw": "",
        "summary": None,
    }


async def summarize_trial(
    trial,
    trajectory: dict,
    task_context,
    *,
    model: str,
    persist: bool,
    client=None,
) -> dict:
    """Run the summary block for one trial and return its full record.

    Never raises for a trial-level failure (cancellation excepted): a failure
    yields a record carrying ``status``, ``error``, and whatever ``raw``
    accumulated -- including failures raised before a block even exists.
    """
    from oddish.blocks.analyzer.analyzer_llm_client import ApiAnalyzerLLMClient
    from api.services.summarize_trajectory import (
        SCHEMA_VERSION,
        SUMMARY_MAX_TOKENS,
        _load_summary_prompt,
        build_summary_block,
    )
    from oddish.db import get_session
    from oddish.db.models import JobStatus

    owned = client is None
    llm = None
    try:
        # Same registry lookup generate() uses, so the dump can't diverge from
        # what prod sends -- no call site gets a fallback template.
        async with get_session() as session:
            prompt_template, prompt_version = await _load_summary_prompt(session)
        llm = client or ApiAnalyzerLLMClient(model=model, max_tokens=SUMMARY_MAX_TOKENS)
        block = build_summary_block(
            trajectory, task_context, analyzer_id=trial.id, model=model, client=llm,
            prompt_template=prompt_template, prompt_version=prompt_version,
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        # A malformed trajectory raises in build_prompt(), before any block
        # exists to carry the error -- synthesize the record instead.
        if owned and llm is not None:
            with contextlib.suppress(Exception):
                await llm.aclose()
        return failed_record(trial, task_context, model=model, error=repr(exc))

    if not persist:
        async def _noop(*_a, **_k):
            return None
        block.save_to_s3 = _noop  # type: ignore[method-assign]
        block.save_to_db = _noop  # type: ignore[method-assign]

    summary: dict | None = None
    harness_error: str | None = None
    try:
        out = await block.run()
        summary = out.output
    except asyncio.CancelledError:
        # Cancellation is not a trial-level failure -- never swallow it.
        raise
    except Exception as exc:
        # block.run() usually recorded status/error before re-raising, but not
        # when its own finally raised (e.g. _persist's utf-8 encode on a lone
        # surrogate) -- keep the exception so that case is not reported clean.
        harness_error = repr(exc)
    finally:
        if owned:
            try:
                await llm.aclose()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                harness_error = harness_error or repr(exc)

    error = block.error or harness_error
    status = block.status
    # A teardown-only error (aclose() raising after a real summary) must not
    # relabel an otherwise-good record -- only a missing summary downgrades.
    if status == JobStatus.SUCCESS and summary is None:
        status = JobStatus.FAILED

    return {
        "trial_id": trial.id,
        "task_name": task_context.task_name,
        "final_reward": task_context.final_reward,
        "model": model,
        "schema_version": SCHEMA_VERSION,
        "status": status.value,
        "duration_s": block.job_duration_seconds,
        "error": error,
        "prompt": block.prompt,
        "raw": "".join(block._chunks),
        "summary": summary,
    }


class CohortResult(list):
    """Records for the cohort, plus which requested trials produced none.

    Subclasses ``list`` so existing callers that treat the result as a plain
    ``list[dict]`` keep working unchanged; ``skipped`` is additive.
    """

    def __init__(self, records: list[dict], skipped: list[dict]) -> None:
        super().__init__(records)
        self.skipped = skipped


async def run_cohort(
    session,
    *,
    trials: list[str] | None = None,
    task: str | None = None,
    experiment: str | None = None,
    limit: int = 0,
    model: str | None = None,
    persist: bool = False,
) -> CohortResult:
    """Resolve the cohort, then summarize every trial in it.

    Trajectories and task context are read inside the caller's session; the
    blocks then run outside it, bounded by ``MAX_CONCURRENCY``.
    """
    from api.services.summarize_trajectory import build_task_context, resolve_summary_model
    from oddish.core.trial_io import read_trial_trajectory

    model = model or resolve_summary_model()
    cohort = await resolve_cohort(
        session, trials=trials, task=task, experiment=experiment, limit=limit,
    )
    print(f"resolved {len(cohort)} trials: {', '.join(t.id for t in cohort)}")

    # resolve_cohort silently drops unknown explicit ids; diff against what
    # was asked for so a typo'd id is reported, not swallowed.
    skipped: list[dict] = []
    if trials:
        resolved_ids = {t.id for t in cohort}
        skipped.extend(
            {"trial_id": tid, "reason": "no such trial"}
            for tid in trials if tid not in resolved_ids
        )

    # One slot per attempted trial, filled in cohort order. A prep failure
    # occupies its slot immediately; summarized trials claim theirs below.
    slots: list[dict | None] = []
    prepared: list[tuple[int, object, dict, object]] = []
    for trial in cohort:
        try:
            trajectory = await read_trial_trajectory(trial)
            # None means "no trajectory" -- skip, but still report it.
            if trajectory is None:
                print(f"  skip {trial.id}: no fetchable trajectory")
                skipped.append({"trial_id": trial.id, "reason": "no fetchable trajectory"})
                continue
            ctx = await build_task_context(trial)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(f"  {trial.id}: prep failed: {exc!r}")
            slots.append(failed_record(trial, None, model=model, error=repr(exc)))
            continue
        prepared.append((len(slots), trial, trajectory, ctx))
        slots.append(None)

    sem = asyncio.Semaphore(MAX_CONCURRENCY)

    async def _one(_idx, trial, trajectory, ctx) -> dict:
        async with sem:
            record = await summarize_trial(
                trial, trajectory, ctx, model=model, persist=persist,
            )
            print(f"  {record['trial_id']}: {record['status']}")
            return record

    outcomes = await asyncio.gather(
        *(_one(*p) for p in prepared), return_exceptions=True
    )
    for (idx, trial, _traj, ctx), outcome in zip(prepared, outcomes):
        if isinstance(outcome, asyncio.CancelledError):
            raise outcome
        if isinstance(outcome, BaseException):
            # summarize_trial is meant never to raise; belt-and-braces so a
            # regression there costs one record, not the whole cohort.
            slots[idx] = failed_record(trial, ctx, model=model, error=repr(outcome))
        else:
            slots[idx] = outcome

    return CohortResult([r for r in slots if r is not None], skipped)
