"""Dev harness: run the trajectory-SUMMARY AnalyzerBlock for a single trial.

Entry point is the *first (and only) AnalyzerBlock* of the generate-summary
process — the same block `summarize_trajectory.generate()` builds: a
`TrajectoryBlock` supplies the prompt + `to_summary` parser, and an
`AnalyzerBlock` (API backend) streams it and self-persists to `analyzer_blocks`
+ S3. This script wires that block by hand so you can inspect the block object
(status / id / output / error / duration), not just the returned dict.

Set TRIAL_ID below, then run from the backend package (its uv env has `oddish`
+ `api.services` importable) with prod DB + S3 + Anthropic creds in the env:

    cd backend
    ODDISH_DATABASE_URL=<prod> ANTHROPIC_API_KEY=... \
    <S3/AWS creds as your settings expect> \
    .venv/bin/python run_summary_block.py

Notes:
  - Reads the trial's trajectory from S3 and calls Anthropic for real.
  - `AnalyzerBlock.run()` PERSISTS a row to `analyzer_blocks` + an S3 object
    (that's the block's contract). Set DRY_RUN=True to no-op both and just print.
"""

from __future__ import annotations

import asyncio
import json

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from oddish.blocks.analyzer.analyzer_block import (
    AnalyzerBlock,
    AnalyzerInput,
    AnalyzerType,
)
from oddish.blocks.analyzer.analyzer_llm_client import LLMClientType
from api.services.blocks.analyzer.trajectory.trajectory_component_block import (
    TrajectoryBlock,
    TrajectoryInput,
)
from api.services.summarize_trajectory import (
    SCHEMA_VERSION,
    build_task_context,
    load_summary_prompt_template,
)
from oddish.config import settings, to_anthropic_api_model_id
from oddish.core.trial_io import read_trial_trajectory
from oddish.db import get_session
from oddish.db.models import TrialModel

# ---- configure me -----------------------------------------------------------
TRIAL_ID = "REPLACE_WITH_A_TRIAL_ID"
DRY_RUN = True  # True = don't persist the analyzer_blocks row / S3 object
MODEL_OVERRIDE: str | None = None  # None -> settings.analysis_model
# -----------------------------------------------------------------------------


def _model() -> str:
    if MODEL_OVERRIDE:
        return MODEL_OVERRIDE
    return to_anthropic_api_model_id(settings.analysis_model) or settings.analysis_model


async def run() -> None:
    # Load the trial with its task eager-loaded (build_task_context reads
    # trial.task.name; an async lazy-load would raise). Read the trajectory +
    # assemble the TaskContext inside the session, then close it before running
    # the block (the block opens its own session to persist).
    async with get_session() as session:
        trial = (
            await session.execute(
                select(TrialModel)
                .options(selectinload(TrialModel.task))
                .where(TrialModel.id == TRIAL_ID)
            )
        ).scalar_one_or_none()
        if trial is None:
            raise SystemExit(f"No trial {TRIAL_ID!r} in this database.")

        trajectory = await read_trial_trajectory(trial)
        if trajectory is None:
            raise SystemExit(f"Trial {TRIAL_ID!r} has no fetchable trajectory.")
        task_context = await build_task_context(trial)

    prompt_template = load_summary_prompt_template()

    print(
        f"trial={TRIAL_ID}  task={task_context.task_name!r}  "
        f"reward={task_context.final_reward}  steps={len(trajectory.get('steps') or [])}"
    )

    model = _model()
    tb = TrajectoryBlock(
        TrajectoryInput(
            task_name=task_context.task_name,
            instruction=task_context.instruction,
            final_reward=task_context.final_reward,
            model_used=task_context.model_used,
            verifier_output=task_context.verifier_output,
            trajectory=trajectory,
        ),
        instructions_template=prompt_template,
    )
    block = AnalyzerBlock(
        analyzer_type=AnalyzerType.TRAJECTORY_SUMMARY,
        llm_client_type=LLMClientType.API,
        input=AnalyzerInput(
            input={"trial_id": TRIAL_ID, "task_name": task_context.task_name}
        ),
        prompt=tb.build_prompt(),
        analyzer_id=TRIAL_ID,
        block_metadata={
            "schema_version": SCHEMA_VERSION,
            "model": model,
        },
        output_transform=lambda raw: tb.to_summary(raw, model=model),
        model=model,
        max_tokens=2048,
    )

    if DRY_RUN:
        # Skip persistence so the harness leaves no analyzer_blocks / S3 trace.
        async def _noop(*_a, **_k):
            return None

        block.save_to_s3 = _noop  # type: ignore[method-assign]
        block.save_to_db = _noop  # type: ignore[method-assign]

    print(f"model={model}  block_id={block.id}  dry_run={DRY_RUN}")
    print("running summary block ...\n")
    out = await block.run()

    print(
        f"status={block.status.value}  duration={block.job_duration_seconds:.2f}s  "
        f"error={block.error}"
    )
    print("\n=== summary dict (block.output.output) ===")
    print(json.dumps(out.output, indent=2))


if __name__ == "__main__":
    asyncio.run(run())
