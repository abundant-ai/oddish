"""Backfill ``trial.result`` with the verifier reward breakdown.

New trials persist their partial-credit breakdown (code/workflow fractions,
tests passed/total) into ``trial.result`` at ingestion. This one-off backfills
*existing* trials: for any trial that has a scalar reward but no result blob, it
reads ``reward.json`` from S3 and stores ``{"reward_breakdown": {...}}`` so
historical scores are explainable in the UI/CLI instead of bare numbers.

Idempotent (only touches trials with ``result IS NULL``); dry-run by default.
Runs in an environment with DB + S3 access (the backend):

    python -m oddish.backfill_reward_breakdown                      # dry run
    python -m oddish.backfill_reward_breakdown --apply              # write
    python -m oddish.backfill_reward_breakdown --experiment 18b8fffc --apply
"""

from __future__ import annotations

import argparse
import asyncio

from sqlalchemy import select

from oddish.db import get_session
from oddish.db.models import TrialModel
from oddish.db.storage import StorageClient

# Numeric sub-reward keys the partial-credit grader emits into reward.json.
_NUMERIC_BREAKDOWN_KEYS = {
    "reward",
    "code_fraction",
    "code_tests_passed",
    "code_tests_total",
    "code_exit",
    "workflow_fraction",
    "workflow_passed",
    "workflow_total",
    "workflow_failures_count",
}


async def _reward_json_for_trial(storage: StorageClient, trial: TrialModel):
    prefix = trial.trial_s3_key or StorageClient._trial_prefix(trial.id)
    try:
        keys = await storage.list_keys(prefix)
    except Exception:
        return None
    key = next((k for k in keys if k.endswith("/verifier/reward.json")), None)
    if not key:
        return None
    try:
        return await storage.download_json(key)
    except Exception:
        return None


def extract_breakdown(reward_json: object) -> dict | None:
    """Return the numeric sub-reward dict, or None when there's no real breakdown
    (scalar-only graders, where reward is the only numeric field)."""
    if not isinstance(reward_json, dict):
        return None
    bd = {
        k: v
        for k, v in reward_json.items()
        if k in _NUMERIC_BREAKDOWN_KEYS and isinstance(v, (int, float))
    }
    if not any(k != "reward" for k in bd):
        return None
    return bd


async def run_backfill(*, apply: bool, experiment: str | None, limit: int) -> None:
    storage = StorageClient()
    async with get_session() as session:
        query = select(TrialModel).where(
            TrialModel.reward.isnot(None),
            TrialModel.result.is_(None),
        )
        if experiment:
            query = query.where(TrialModel.experiment_id == experiment)
        query = query.limit(limit)
        trials = list((await session.execute(query)).scalars().all())
        print(f"Candidate trials (reward set, result null): {len(trials)}")

        updated = skipped = 0
        for trial in trials:
            breakdown = extract_breakdown(
                await _reward_json_for_trial(storage, trial)
            )
            if breakdown is None:
                skipped += 1
                continue
            if apply:
                trial.result = {"reward_breakdown": breakdown}
            updated += 1

        if apply:
            await session.commit()
            print(f"Applied: updated {updated}, skipped {skipped} (no breakdown).")
        else:
            print(
                f"Dry run: would update {updated}, skip {skipped}. "
                "Re-run with --apply to write."
            )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Backfill trial.result reward breakdown from S3 reward.json."
    )
    parser.add_argument(
        "--apply", action="store_true", help="Write changes (default: dry run)."
    )
    parser.add_argument(
        "--experiment", default=None, help="Limit to a single experiment id."
    )
    parser.add_argument(
        "--limit", type=int, default=2000, help="Max trials to scan per run."
    )
    args = parser.parse_args()
    asyncio.run(
        run_backfill(apply=args.apply, experiment=args.experiment, limit=args.limit)
    )


if __name__ == "__main__":
    main()
