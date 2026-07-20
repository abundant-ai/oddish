"""One-off: re-run probe analysis for a prod probe trial, inside Modal.

Runs in the deployed worker image (so the ``oddish`` package + deps are
present) with the ``oddish-prod`` secret (prod DB + S3 + Anthropic key). Reads
the trial's stored S3 artifacts and re-runs ``run_probe_analyzer`` with the
CURRENT code — no agent re-run.

Run from the ``backend/`` dir so modal_app's relative build paths resolve:

    cd backend
    modal run reanalyze_last_probe_modal.py                # dry run, prints JSON
    modal run reanalyze_last_probe_modal.py --write        # persist to trial.analysis
    modal run reanalyze_last_probe_modal.py --trial-id <id> --write
"""

import modal

# Reuse the deployed worker image + prod secret so the container is identical to
# where probe analysis normally runs.
from modal_app import image, runtime_secrets

app = modal.App("probe-reanalyze-oneoff")


@app.function(image=image, secrets=runtime_secrets, timeout=900)
async def reanalyze(trial_id: str | None, write: bool) -> str:
    import json

    from sqlalchemy import select

    from oddish.config import settings
    from oddish.core.trial_io import read_trial_probe_artifacts
    from oddish.db import AnalysisStatus, TrialModel, get_session, utcnow
    from oddish.worker.probe_analysis import run_probe_analyzer

    async with get_session() as session:
        if trial_id:
            trial = await session.get(TrialModel, trial_id)
        else:
            # "The last run" = most recent probe trial that already completed and
            # was analyzed (so its artifacts are guaranteed available).
            trial = (
                (
                    await session.execute(
                        select(TrialModel)
                        .where(TrialModel.is_probe.is_(True))
                        .where(TrialModel.superseded_by_trial_id.is_(None))
                        .where(TrialModel.analysis_status == AnalysisStatus.SUCCESS)
                        .order_by(TrialModel.created_at.desc())
                        .limit(1)
                    )
                )
                .scalars()
                .first()
            )
        if not trial:
            return "No completed/analyzed probe trial found."
        harbor_config = trial.harbor_config or {}
        info = {
            "trial_id": trial.id,
            "task_id": trial.task_id,
            "created_at": str(trial.created_at),
            "agent": trial.agent,
            "extra_instructions": harbor_config.get("extra_instructions"),
            "result_focus": harbor_config.get("result_focus") or "",
            "reward": trial.reward,
        }
        artifacts = await read_trial_probe_artifacts(trial)

    if not info["extra_instructions"]:
        return f"Trial {info['trial_id']} is not a probe trial (no extra_instructions)."
    if not artifacts.get("agent_messages"):
        return (
            f"Trial {info['trial_id']} has no stored agent transcript "
            "(artifacts unavailable) — cannot re-analyze."
        )

    resolved_trial_id = info["trial_id"]
    result = await run_probe_analyzer(
        extra_instructions=info["extra_instructions"],
        agent_messages=artifacts["agent_messages"],
        verifier_stdout=artifacts.get("verifier_stdout") or "",
        reward=info["reward"],
        result_focus=info["result_focus"],
        model=settings.analysis_model,
    )

    if write:
        async with get_session() as session:
            trial = await session.get(TrialModel, resolved_trial_id)
            if trial:
                trial.analysis = result
                trial.analysis_status = AnalysisStatus.SUCCESS
                trial.analysis_finished_at = utcnow()
                trial.analysis_error = None

    return json.dumps(
        {"trial": info, "wrote": write, "analysis": result}, indent=2, default=str
    )


@app.function(image=image, secrets=runtime_secrets, timeout=300)
async def check(trial_id: str | None) -> str:
    """Read-only: report the stored analysis state for a trial (to verify a write)."""
    import json

    from sqlalchemy import select

    from oddish.db import AnalysisStatus, TrialModel, get_session

    async with get_session() as session:
        if trial_id:
            trial = await session.get(TrialModel, trial_id)
        else:
            trial = (
                (
                    await session.execute(
                        select(TrialModel)
                        .where(TrialModel.is_probe.is_(True))
                        .where(TrialModel.superseded_by_trial_id.is_(None))
                        .where(TrialModel.analysis_status == AnalysisStatus.SUCCESS)
                        .order_by(TrialModel.created_at.desc())
                        .limit(1)
                    )
                )
                .scalars()
                .first()
            )
        if not trial:
            return "No completed/analyzed probe trial found."
        analysis = trial.analysis or {}
        return json.dumps(
            {
                "trial_id": trial.id,
                "analysis_status": str(trial.analysis_status),
                "analysis_finished_at": str(trial.analysis_finished_at),
                "analysis_generated_at": analysis.get("generated_at"),
                "has_result_focus_summary": "result_focus_summary" in analysis,
                "result_focus_summary": analysis.get("result_focus_summary"),
            },
            indent=2,
            default=str,
        )


@app.local_entrypoint()
def main(write: bool = False, trial_id: str = "") -> None:
    print(reanalyze.remote(trial_id=trial_id or None, write=write))


@app.local_entrypoint()
def verify(trial_id: str = "") -> None:
    print(check.remote(trial_id=trial_id or None))
