"""One-off: find trial-execution failures via the AUTHORITATIVE error source.

Why this exists: ``trials.error_message`` is unreliable for these failures. It
is wiped to NULL at the start of every attempt (``_prepare_trial_run``) and only
repopulated if the final terminal attempt runs ``_store_trial_results`` to
completion. A trial stuck retrying, stale-reaped while RUNNING, or whose worker
died mid-failure shows ``error_message = NULL`` even though every attempt hit the
same error. Filtering on that column finds nothing (observed: a probe cohort
that all failed on Daytona's "Docker daemon not ready" showed zero rows when
filtered on error_message).

So this scan reads the durable sources instead, per trial:

  1. S3 ``result.json`` -> ``exception_info`` (Harbor-authoritative: type,
     message, traceback, occurred_at) -- the real "why".
  2. ``worker_jobs`` rows (subject_table='trials') -- per-attempt history that
     survives retries: error_message, last_heartbeat_error, stale_reaped_at.
  3. ``trials`` row state for cross-reference (status / reward / error_message).

It groups failures by exception_type so you see the whole failure surface, not
just the one error you already knew about.

Read-only. Runs in the deployed worker image with the ``oddish-prod`` secret.

    cd backend
    .venv/bin/modal run probe_docker_failures_modal.py                 # 72h, probes
    .venv/bin/modal run probe_docker_failures_modal.py --hours 168     # last week
    .venv/bin/modal run probe_docker_failures_modal.py --all-trials    # incl. non-probe
    .venv/bin/modal run probe_docker_failures_modal.py --limit 300
"""

import modal

from modal_app import image, runtime_secrets

app = modal.App("probe-docker-failures-oneoff")


def _trunc(value, n: int = 200):
    if value is None:
        return None
    s = str(value)
    return s if len(s) <= n else s[:n] + f"... [+{len(s) - n} chars]"


@app.function(image=image, secrets=runtime_secrets, timeout=900)
async def examine(hours: int, all_trials: bool, limit: int, needle: str) -> str:
    import asyncio
    import json
    from collections import Counter
    from datetime import timedelta

    from sqlalchemy import and_, select

    from oddish.db import (
        TrialModel,
        WorkerJobModel,
        get_session,
        get_storage_client,
        utcnow,
    )

    storage = get_storage_client()
    cutoff = utcnow() - timedelta(hours=hours)

    async with get_session() as session:
        # Candidate set: every NON-success trial in the window. We do NOT filter
        # on error_message — that's the whole point. ``reward IS NULL`` + a
        # non-success status is the reliable "this trial did not produce a
        # verifier score" signal.
        conditions = [
            TrialModel.created_at >= cutoff,
            TrialModel.superseded_by_trial_id.is_(None),
            TrialModel.reward.is_(None),
        ]
        if not all_trials:
            conditions.append(TrialModel.is_probe.is_(True))

        trials = (
            (
                await session.execute(
                    select(TrialModel)
                    .where(and_(*conditions))
                    .order_by(TrialModel.created_at.desc())
                    .limit(limit)
                )
            )
            .scalars()
            .all()
        )

        # Pull worker_jobs for the whole candidate set in one query.
        trial_ids = [t.id for t in trials]
        jobs_by_trial: dict[str, list] = {tid: [] for tid in trial_ids}
        if trial_ids:
            jrows = (
                (
                    await session.execute(
                        select(WorkerJobModel)
                        .where(WorkerJobModel.subject_table == "trials")
                        .where(WorkerJobModel.subject_id.in_(trial_ids))
                        .order_by(WorkerJobModel.created_at.asc())
                    )
                )
                .scalars()
                .all()
            )
            for j in jrows:
                jobs_by_trial.setdefault(j.subject_id, []).append(j)

        trial_meta = [
            {
                "id": t.id,
                "task_id": t.task_id,
                "is_probe": t.is_probe,
                "status": str(t.status),
                "attempts": t.attempts,
                "max_attempts": t.max_attempts,
                "trial_error_message": _trunc(t.error_message, 120),
                "trial_s3_key": t.trial_s3_key,
                "created_at": str(t.created_at),
                "analysis_headline": (t.analysis or {}).get("headline"),
            }
            for t in trials
        ]

    # Read each trial's authoritative result.json from S3, bounded concurrency.
    sem = asyncio.Semaphore(12)

    async def _exc_info(prefix: str) -> dict | None:
        async with sem:
            try:
                rj = await storage.get_trial_result_json(prefix)
            except Exception as exc:
                return {"_read_error": f"{type(exc).__name__}: {exc}"}
        if not isinstance(rj, dict):
            return None
        return rj.get("exception_info") or None

    prefixes = [
        m["trial_s3_key"] or get_storage_client()._trial_prefix(m["id"])
        for m in trial_meta
    ]
    exc_infos = await asyncio.gather(*[_exc_info(p) for p in prefixes])

    rows = []
    for m, jobs, exc in zip(trial_meta, [jobs_by_trial[m["id"]] for m in trial_meta], exc_infos):
        rows.append(
            {
                **m,
                "result_json_exception_type": (exc or {}).get("exception_type"),
                "result_json_exception_message": _trunc(
                    (exc or {}).get("exception_message"), 200
                ),
                "result_json_occurred_at": (exc or {}).get("occurred_at"),
                "worker_jobs": [
                    {
                        "kind": str(j.kind),
                        "status": str(j.status),
                        "stale_reaped_at": str(j.stale_reaped_at),
                        "error_message": _trunc(j.error_message, 150),
                        "last_heartbeat_error": _trunc(j.last_heartbeat_error, 150),
                    }
                    for j in jobs
                ],
            }
        )

    # Aggregates -------------------------------------------------------------
    total = len(rows)
    by_exc_type = Counter(
        r["result_json_exception_type"] or "(no result.json exception_info)"
        for r in rows
    )
    by_status = Counter(r["status"] for r in rows)
    by_hour = Counter(r["created_at"][:13] for r in rows)
    err_msg_present = sum(1 for r in rows if r["trial_error_message"])

    # Count result.json exception_info entries matching the needle (case-
    # insensitive substring over exception_message OR exception_type).
    nl = needle.lower()
    needle_hits = [
        r
        for r in rows
        if nl
        in (
            (r["result_json_exception_message"] or "")
            + " "
            + (r["result_json_exception_type"] or "")
        ).lower()
    ]
    with_any_exc_info = sum(1 for r in rows if r["result_json_exception_type"])

    aggregate = {
        "window_hours": hours,
        "probe_only": not all_trials,
        "candidate_trials_no_reward": total,
        "trials_with_result_json_exception_info": with_any_exc_info,
        "needle": needle,
        "needle_matches_in_exception_info": len(needle_hits),
        "needle_matching_trial_ids": [r["id"] for r in needle_hits],
        "exception_type_distribution": dict(by_exc_type.most_common()),
        "status_distribution": dict(by_status),
        "per_hour_counts": dict(sorted(by_hour.items())),
        "trials_with_db_error_message": err_msg_present,
        "note": (
            f"{total - err_msg_present}/{total} failed trials have a NULL "
            "trials.error_message — confirming why a LIKE filter on that column "
            "misses them. The real error is in result_json_exception_type."
        ),
    }

    return json.dumps({"aggregate": aggregate, "trials": rows}, indent=2, default=str)


@app.local_entrypoint()
def main(
    hours: int = 72,
    all_trials: bool = False,
    limit: int = 200,
    needle: str = "Docker daemon",
) -> None:
    print(
        examine.remote(
            hours=hours, all_trials=all_trials, limit=limit, needle=needle
        )
    )
