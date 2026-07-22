"""Dev harness: run the FIRST analyzer MAP AnalyzerBlock for an experiment.

Entry point is the analyzer's first map step, wrapped as an `AnalyzerBlock`
(`AnalyzerType.TRAJECTORY_FAILURE_ANALYSIS`, API backend). It mirrors what
`run_analyzer_eval` does up to the first `_map_one` call — load the experiment's
trials, build analyzer inputs, bucket into bad/good, build the roster, and take
the first selected trial's `build_map_prompt(...)` — then runs that one block so
you can inspect its output (the per-trial MAP finding) in isolation.

Set EXPERIMENT_ID below, then run from the backend package with prod DB + S3 +
Anthropic creds in the env (build_analyzer_inputs reads trajectories from S3;
subanalysis bucketing reads trial.analysis from the DB):

    cd backend
    ODDISH_DATABASE_URL=<prod> ANTHROPIC_API_KEY=... \
    <S3/AWS creds as your settings expect> \
    .venv/bin/python run_analyzer_map_block.py

Notes:
  - Only trials already classified into the bad/good cohorts are mappable; if the
    experiment has none, the script reports that and exits.
  - `AnalyzerBlock.run()` PERSISTS a row to `analyzer_blocks` + an S3 object. Set
    DRY_RUN=True to no-op both and just print the finding.
"""

from __future__ import annotations

import asyncio
import json

from sqlalchemy import select

from oddish.blocks.analyzer.analyzer_block import (
    AnalyzerBlock,
    AnalyzerInput,
    AnalyzerType,
)
from oddish.blocks.analyzer.analyzer_llm_client import (
    ApiAnalyzerLLMClient,
    LLMClientType,
)
from oddish.config import settings, to_anthropic_api_model_id
from oddish.core.analyzer_inputs import build_analyzer_inputs
from oddish.core.experiment_membership import trial_in_experiment
from oddish.db import get_session
from oddish.db.models import ExperimentModel, TaskModel, TrialModel, TrialStatus
from oddish.evals.analyzer.bucketing import bucket_subanalyses
from oddish.evals.analyzer.core import build_roster
from oddish.evals.analyzer.prompt_builder import build_map_prompt

# ---- configure me -----------------------------------------------------------
EXPERIMENT_ID = "REPLACE_WITH_AN_EXPERIMENT_ID"
DRY_RUN = True   # True = don't persist the analyzer_blocks row / S3 object
MODEL_OVERRIDE: str | None = None  # None -> settings.analysis_model
# -----------------------------------------------------------------------------


def _model() -> str:
    if MODEL_OVERRIDE:
        return MODEL_OVERRIDE
    return to_anthropic_api_model_id(settings.analysis_model) or settings.analysis_model


async def _experiment_inputs(experiment_id: str):
    """Load the experiment's trials as (trial, task_path) rows and build the
    analyzer inputs (bundles + subanalyses). Mirrors the trial filters in
    haiku_sandbox_bad_failures._gather. build_analyzer_inputs reads trajectories
    from S3, so it must run inside the session."""
    async with get_session() as session:
        exp = await session.get(ExperimentModel, experiment_id)
        if exp is None:
            raise SystemExit(f"No experiment {experiment_id!r} in this database.")

        raw_rows = (
            await session.execute(
                select(TrialModel, TaskModel.task_path)
                .join(TaskModel, TrialModel.task_id == TaskModel.id)
                .where(
                    trial_in_experiment(experiment_id),
                    TrialModel.superseded_by_trial_id.is_(None),
                    TrialModel.org_id == exp.org_id,
                    TrialModel.status.in_([TrialStatus.SUCCESS, TrialStatus.FAILED]),
                )
            )
        ).all()

        seen: set[str] = set()
        rows: list[tuple[object, str]] = []
        for trial, task_path in raw_rows:
            if trial.id in seen:
                continue
            seen.add(trial.id)
            rows.append((trial, task_path))

        if not rows:
            raise SystemExit(f"Experiment {experiment_id!r} has no SUCCESS/FAILED trials.")

        inputs = await build_analyzer_inputs(rows)
    return inputs, len(rows)


async def run() -> None:
    inputs, n_trials = await _experiment_inputs(EXPERIMENT_ID)

    bad, good, breakdown = bucket_subanalyses(inputs.subanalyses)
    roster = build_roster(bad, good)
    by_trial = {b.trial_id: b for b in inputs.bundles}
    selected = [sa for sa in (bad + good) if sa.trial_id in by_trial]

    print(f"experiment={EXPERIMENT_ID}  trials={n_trials}  "
          f"bad={len(bad)} good={len(good)}  breakdown={json.dumps(breakdown)}")
    if not selected:
        raise SystemExit("No bad/good-failure trials to map (nothing classified into a cohort).")

    first = selected[0]
    bundle = by_trial[first.trial_id]
    prompt = build_map_prompt(bundle, first, roster)
    print(f"mapping first trial={first.trial_id}  "
          f"{first.classification}/{first.subtype}  prompt_chars={len(prompt)}")

    model = _model()
    llm = ApiAnalyzerLLMClient(model=model, max_tokens=6000)
    block = AnalyzerBlock(
        analyzer_type=AnalyzerType.TRAJECTORY_FAILURE_ANALYSIS,
        llm_client_type=LLMClientType.API,
        input=AnalyzerInput(input={"trial_id": first.trial_id, "experiment_id": EXPERIMENT_ID}),
        prompt=prompt,
        analyzer_id=EXPERIMENT_ID,
        block_metadata={"model": model, "trial_id": first.trial_id},
        client=llm,
    )

    if DRY_RUN:
        async def _noop(*_a, **_k):
            return None
        block.save_to_s3 = _noop  # type: ignore[method-assign]
        block.save_to_db = _noop  # type: ignore[method-assign]

    print(f"model={model}  block_id={block.id}  dry_run={DRY_RUN}")
    print("running analyzer map block ...\n")
    try:
        out = await block.run()
    finally:
        await llm.aclose()

    print(f"status={block.status.value}  duration={block.job_duration_seconds:.2f}s  "
          f"error={block.error}")
    print("\n=== map finding (raw block.output.output) ===")
    print(out.output)


if __name__ == "__main__":
    asyncio.run(run())
