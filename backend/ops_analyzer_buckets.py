"""One-off: reproduce an analyzer's bucketing + prompt sizing offline.

    cd backend
    uv run modal run -e main ops_analyzer_buckets.py --analyzer 8e672fc5

Answers "why did the 'good' cohort die before REDUCE?" without running a single
agent: _gather() and bucket_subanalyses() are pure DB reads (trial.analysis
JSON), and build_cohort_prompt() is pure string building. So we can measure
exactly what production fed the agent -- cohort sizes and prompt bytes -- for
free, and compare bad (which succeeds) against good (which does not).

The worker logs these counts at analyzer_sandbox.py:122 via logger.info, which
the Modal container drops at the root logger. This recomputes them.

Import only modal_app -- importing endpoints/worker registers the dispatcher.
"""

from __future__ import annotations

import modal

from modal_app import image, runtime_secrets

app = modal.App("analyzer-buckets-oneoff")


@app.function(image=image, secrets=runtime_secrets, timeout=900)
async def buckets(analyzer_id: str) -> None:
    from oddish.config import Settings

    Settings.db_use_null_pool = True

    from sqlalchemy import text

    from api.services.blocks.analyzer.analyzer_prompt import build_cohort_prompt
    from oddish.db import get_session
    from oddish.evals.analyzer.bucketing import bucket_subanalyses
    from oddish.evals.analyzer.core import build_roster
    from oddish.workers.queue.analyzer_handler import _gather_trial_rows
    from worker.analyzer_sandbox import _gather

    async with get_session() as session:
        org_id = (
            await session.execute(
                text("SELECT org_id FROM analyzers WHERE id = :aid"),
                {"aid": analyzer_id},
            )
        ).scalar_one()
        rows = await _gather_trial_rows(session, analyzer_id, org_id)

    print(f"trial rows gathered: {len(rows)}")

    subs, oracle_by_trial, host_by_trial = _gather(rows)
    bad, good, breakdown = bucket_subanalyses(subs)
    counts = {"trials": len(rows), "bad": len(bad), "good": len(good)}
    print(f"subanalyses: {len(subs)}  (rows without analysis are skipped)")
    print(f"counts    : {counts}")
    print(f"breakdown : {breakdown}")
    print(f"oracle ctx: {len(oracle_by_trial)} trial(s) (bad bucket only)")

    roster = build_roster(bad, good)
    print(f"roster    : {len(roster)} rows")

    print("\n-- prompt sizing (what each cohort agent actually receives) --")
    for bucket, cohort in (("bad", bad), ("good", good)):
        if not cohort:
            print(f"  {bucket:5}: EMPTY -- skipped by analyzer_sandbox.py:152")
            continue
        prompt = build_cohort_prompt(
            bucket, cohort, roster, counts, oracle_by_trial
        )
        print(
            f"  {bucket:5}: cohort={len(cohort):4} trials  "
            f"prompt={len(prompt):7,} chars (~{len(prompt)//4:,} tokens)"
        )

    print(
        "\nEach MAP step costs >=1 CLI trajectory fetch per trial, on Haiku with "
        "no --max-turns. A much larger 'good' cohort would exhaust the agent "
        "during MAP, before it ever writes reduce.json."
    )


@app.local_entrypoint()
def main(analyzer: str) -> None:
    buckets.remote(analyzer_id=analyzer)
