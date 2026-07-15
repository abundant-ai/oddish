"""Run one bucket's cohort at a chosen size, against real prod trials.

Built to test the cohort-size hypothesis: bad (8 trials) succeeded and good (97)
failed with a 0B reduce, but TWO things differed -- bucket AND size. Running the
*good* bucket at a small size isolates size. good@8 succeeded, which is the
evidence the batching in analyzer_cohort.py rests on.

Now also the way to verify the batched loop end-to-end without deploying: the
Modal image bakes local source, so `--n 97` runs the working tree's code against
the cohort that originally blew up.

Reads prod trials read-only; provisions one Daytona sandbox per run and writes
nothing back to the DB.

    cd backend && modal run scripts/probe_cohort_size.py --n 97
"""

import json

import modal

from modal_app import image, runtime_secrets

app = modal.App("oddish-cohort-size-test")

EXPERIMENT = "c02666c5"
# Must exceed analyzer_cohort.COHORT_TIMEOUT_SECONDS (5400) or Modal kills the
# container first and a merely-slow cohort looks like a failure of the code.
FUNCTION_TIMEOUT = 7200


class _Row:
    """A detached snapshot of the trial columns the sandbox path reads.

    The session CANNOT stay open across the sandbox work: a batched 97-trial
    cohort takes far longer than the Supabase pooler's idle timeout, and the
    connection gets dropped mid-run ("cannot call Transaction.commit(): the
    underlying connection is closed"). Every attribute below is a plain column,
    so snapshotting them lets the session close before the slow part starts --
    there are no lazy relationships here to trip a DetachedInstanceError.
    """

    __slots__ = ("id", "task_id", "analysis", "analysis_status", "agent",
                 "model", "org_id", "reward", "trajectory_summary")

    def __init__(self, t):
        for f in self.__slots__:
            setattr(self, f, getattr(t, f, None))


@app.function(image=image, secrets=runtime_secrets, timeout=FUNCTION_TIMEOUT)
async def size_test(n: int, eid: str, classification: str) -> str:
    import logging

    from sqlalchemy import select

    from oddish.core.experiment_membership import trial_in_experiment
    from oddish.db import get_session
    from oddish.db.models import TaskModel, TrialModel, TrialStatus
    from oddish.evals.analyzer.schemas import AnalyzerEvalConfig
    from worker.analyzer_sandbox import sandbox_eval_rows

    # The per-batch progress lines are the point of this run; without them a
    # failure says nothing about which batch broke.
    logging.basicConfig(level=logging.INFO, force=True)

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
        subset = [
            (_Row(t), p) for t, p in rows
            if (t.analysis or {}).get("classification") == classification
        ][:n]
    # Session closed. Everything below runs off detached snapshots.

    if not subset:
        return json.dumps({"error": f"no {classification} trials found"})

    print(f"cohort: {len(subset)} {classification} trials from {eid}", flush=True)
    try:
        out = await sandbox_eval_rows(subset, AnalyzerEvalConfig(), f"sizetest-{n}")
    except Exception as exc:
        return json.dumps({
            "n": n, "ok": False,
            "error_type": type(exc).__name__, "error": str(exc)[:2000],
        })

    by_sub: dict[str, int] = {}
    for f in out.findings:
        by_sub[f.subcategory] = by_sub.get(f.subcategory, 0) + 1

    return json.dumps({
        "n": n,
        "ok": True,
        "findings": len(out.findings),
        # A batch that truncated findings.jsonl instead of appending, or wrote
        # prefixed lines, shows up here as findings < cohort size.
        "trials_missing_a_finding": len(subset) - len(out.findings),
        "counts": out.counts,
        "breakdown": out.breakdown,
        "findings_by_subcategory": by_sub,
        "section_lengths": {k: len(v or "") for k, v in out.sections.items()},
        "headroom_head": (out.sections.get("headroom") or "")[:400],
    }, default=str)


@app.local_entrypoint()
def main(n: int = 8, eid: str = EXPERIMENT, classification: str = "GOOD_FAILURE"):
    print(f"running {classification} cohort at n={n} ...")
    print(size_test.remote(n, eid, classification))
