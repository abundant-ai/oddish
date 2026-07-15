"""Tail-budget report: what does an N-byte tail actually capture?

The sandbox agent sees only the LAST N bytes of each trajectory. This measures,
over a real cohort, how much of each trajectory that keeps and what the whole
cohort costs a single context at various budgets -- so the budget is chosen
against real sizes rather than a guess.

Read-only: pulls trajectories via the same reader the API uses. No sandbox, no LLM.

    cd backend && modal run scripts/tail_budget_report.py --eid c02666c5
"""

import json

import modal

from modal_app import image, runtime_secrets

app = modal.App("oddish-tail-budget-report")

BUDGETS = [2000, 4000, 8000, 16000]
# Rough chars-per-token for JSON-ish trajectory text; only used for an estimate.
CHARS_PER_TOKEN = 4


@app.function(image=image, secrets=runtime_secrets, timeout=1800)
async def report(eid: str, classification: str, limit: int) -> str:
    from sqlalchemy import select

    from oddish.core.experiment_membership import trial_in_experiment
    from oddish.core.trial_io import read_trial_trajectory
    from oddish.db import get_session
    from oddish.db.models import TaskModel, TrialModel, TrialStatus

    sizes: list[int] = []
    failed = 0
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
        cohort = [
            t for t, _ in rows
            if (t.analysis or {}).get("classification") == classification
        ][:limit]

        for trial in cohort:
            try:
                traj = await read_trial_trajectory(trial)
            except Exception:
                failed += 1
                continue
            if traj is None:
                failed += 1
                continue
            sizes.append(len(json.dumps(traj)))

    if not sizes:
        return json.dumps({"error": "no trajectories read", "failed": failed})

    sizes.sort()
    n = len(sizes)

    def pct(p):
        return sizes[min(int(n * p), n - 1)]

    out = {
        "experiment": eid,
        "classification": classification,
        "trials_measured": n,
        "trajectories_unreadable": failed,
        "trajectory_bytes": {
            "min": sizes[0], "p50": pct(0.50), "p90": pct(0.90),
            "max": sizes[-1], "total": sum(sizes),
        },
        "budgets": [],
    }
    for b in BUDGETS:
        kept = sum(min(b, s) for s in sizes)
        untouched = sum(1 for s in sizes if s <= b)
        out["budgets"].append({
            "tail_bytes": b,
            "cohort_bytes_at_this_budget": kept,
            "est_tokens_whole_cohort_one_context": kept // CHARS_PER_TOKEN,
            "est_tokens_per_batch_of_10": (kept // max(n, 1) * 10) // CHARS_PER_TOKEN,
            "trials_fully_shown": f"{untouched}/{n}",
            "pct_of_median_trajectory_kept": round(100 * min(b, pct(0.50)) / pct(0.50), 1),
        })
    return json.dumps(out, indent=2)


@app.local_entrypoint()
def main(eid: str = "c02666c5", classification: str = "GOOD_FAILURE", limit: int = 97):
    print(report.remote(eid, classification, limit))
