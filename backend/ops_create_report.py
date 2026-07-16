"""One-off: create a report for an experiment, resolving a short experiment id.

    cd backend
    uv run modal run -e main ops_create_report.py --experiment c02666c5
    uv run modal run -e main ops_create_report.py --experiment c02666c5 --apply

Dry run by default: resolves the prefix, prints any reports already covering
the experiment, and stops without writing.

There is no CLI/API path for this: ``oddish report create`` posts the id
verbatim (no prefix resolution) and ``POST /reports`` takes org_id/user_id from
the auth context. Here both are read off the experiment row instead.

Import only ``modal_app`` -- importing ``endpoints`` or ``worker`` registers the
scheduled dispatcher functions and would start a second dispatcher.
"""

from __future__ import annotations

import modal

from modal_app import image, runtime_secrets

app = modal.App("create-report-oneoff")


@app.function(image=image, secrets=runtime_secrets, timeout=900)
async def create_report(
    experiment_prefix: str, name: str, apply: bool, save_trials: bool
) -> None:
    from oddish.config import Settings

    Settings.db_use_null_pool = True

    from sqlalchemy import select

    from oddish.core.analyzers import create_analyzer_core
    from oddish.db import get_session
    from oddish.db.models import AnalyzerModel, ExperimentModel, analyzer_experiments
    from oddish.schemas import ReportCreate

    async with get_session() as session:
        matches = (
            await session.execute(
                select(
                    ExperimentModel.id,
                    ExperimentModel.name,
                    ExperimentModel.org_id,
                    ExperimentModel.owner_user_id,
                )
                .where(ExperimentModel.id.startswith(experiment_prefix))
                .where(ExperimentModel.deleted_at.is_(None))
            )
        ).all()

        if not matches:
            print(f"No live experiment with id starting {experiment_prefix!r}.")
            return
        if len(matches) > 1:
            print(f"Ambiguous prefix {experiment_prefix!r} -- {len(matches)} matches:")
            for eid, ename, _, _ in matches:
                print(f"  {eid}  {ename}")
            return

        exp_id, exp_name, org_id, owner_user_id = matches[0]
        print(f"Experiment: {exp_id}  {exp_name!r}  org={org_id}")

        existing = (
            await session.execute(
                select(
                    AnalyzerModel.id,
                    AnalyzerModel.name,
                    AnalyzerModel.status,
                    AnalyzerModel.error,
                    AnalyzerModel.num_trials,
                    AnalyzerModel.created_at,
                )
                .join(
                    analyzer_experiments,
                    analyzer_experiments.c.analyzer_id == AnalyzerModel.id,
                )
                .where(analyzer_experiments.c.experiment_id == exp_id)
                .where(analyzer_experiments.c.deleted_at.is_(None))
                .where(AnalyzerModel.deleted_at.is_(None))
                .order_by(AnalyzerModel.created_at)
            )
        ).all()

        if existing:
            print(f"\n{len(existing)} existing report(s) covering this experiment:")
            for rid, rname, status, error, num_trials, created_at in existing:
                print(f"  {rid}  {status.value:9}  {rname}")
                print(f"    created={created_at}  num_trials={num_trials}")
                if error:
                    print(f"    error: {error}")
        else:
            print("\nNo existing reports cover this experiment.")

        if not apply:
            print("\nDry run -- pass --apply to create. Nothing written.")
            return

        report = await create_analyzer_core(
            session,
            data=ReportCreate(
                experiment_ids=[exp_id],
                name=name or None,
                save_trial_analyses=save_trials,
            ),
            org_id=org_id,
            user_id=owner_user_id,
        )
        await session.commit()
        await session.refresh(report)
        print(f"\nCreated report {report.id} ({report.name}) -- status: {report.status.value}")


@app.local_entrypoint()
def main(
    experiment: str,
    name: str = "",
    apply: bool = False,
    save_trials: bool = False,
) -> None:
    create_report.remote(
        experiment_prefix=experiment, name=name, apply=apply, save_trials=save_trials
    )
