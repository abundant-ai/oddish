"""Test the cohort-size hypothesis for the sandbox analyzer.

bad (8 trials) succeeded and good (97) failed with a 0B reduce, but TWO things
differed: bucket AND size. Running the *good* bucket at a small size isolates
size -- if good@8 succeeds where good@97 failed, bucket is ruled out and size
is the variable.

Reads prod trials read-only; provisions one Daytona sandbox per run and writes
nothing back to the DB.

    cd backend && modal run scripts/probe_cohort_size.py --n 8
"""

import json

import modal

from modal_app import image, runtime_secrets

app = modal.App("oddish-cohort-size-test")

EXPERIMENT = "c02666c5"


@app.function(image=image, secrets=runtime_secrets, timeout=3600)
async def size_test(n: int, eid: str) -> str:
    from sqlalchemy import select

    from oddish.core.experiment_membership import trial_in_experiment
    from oddish.db import get_session
    from oddish.db.models import TaskModel, TrialModel, TrialStatus
    from oddish.evals.analyzer.schemas import AnalyzerEvalConfig
    from worker.analyzer_sandbox import sandbox_eval_rows

    async with get_session() as session:
        stmt = (
            select(TrialModel, TaskModel.task_path)
            .join(TaskModel, TrialModel.task_id == TaskModel.id)
            .where(
                trial_in_experiment(eid),
                TrialModel.superseded_by_trial_id.is_(None),
                TrialModel.status.in_([TrialStatus.SUCCESS, TrialStatus.FAILED]),
            )
        )
        rows = (await session.execute(stmt)).all()

        # Good bucket only, so exactly one cohort (and one sandbox) runs.
        good = [
            (t, p) for t, p in rows
            if (t.analysis or {}).get("classification") == "GOOD_FAILURE"
        ]
        subset = good[:n]
        if not subset:
            return json.dumps({"error": "no GOOD_FAILURE trials found"})

        # Session stays open: sandbox_eval_rows -> _gather touches trial.agent,
        # which is lazy-loaded off these ORM rows.
        try:
            out = await sandbox_eval_rows(
                subset, AnalyzerEvalConfig(), f"sizetest-{n}"
            )
        except Exception as exc:
            return json.dumps({
                "n": n, "ok": False,
                "error_type": type(exc).__name__, "error": str(exc)[:2000],
            })

    return json.dumps({
        "n": n,
        "ok": True,
        "findings": len(out.findings),
        "counts": out.counts,
        "breakdown": out.breakdown,
        "section_lengths": {k: len(v or "") for k, v in out.sections.items()},
        "headroom_head": (out.sections.get("headroom") or "")[:400],
    }, default=str)


@app.local_entrypoint()
def main(n: int = 8, eid: str = EXPERIMENT):
    print(f"running good-bucket cohort at n={n} ...")
    print(size_test.remote(n, eid))
