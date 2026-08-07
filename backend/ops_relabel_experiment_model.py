"""Guarded one-off relabel for copied Oddish experiment 9d73c288.

Dry-run is the default. ``--apply`` changes only ``trials.model`` on the
exact nine copied rows after every experiment, row-count, identity, and source
guard succeeds. ``--rollback --apply`` restores the pre-label value.
"""

from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from collections.abc import Mapping, Sequence
from typing import Any

TARGET_EXPERIMENT_ID = "9d73c288"
TARGET_EXPERIMENT_NAME = "Inference sample"
SOURCE_EXPERIMENT_ID = "d352f859"
SOURCE_EXPERIMENT_NAME = "[xAI] Inference sample"
SOURCE_MODEL = "xai/v9m-rl-learnability-tp8"
PREVIOUS_MODEL = "xai/grok-4-5"
DISPLAY_MODEL = "Groq 4.5"
EXPECTED_QUEUE_KEY = "xai/grok-4-5"
EXPECTED_AGENT = "grok-build"
EXPECTED_PROVIDER = "xai"
EXPECTED_TRIAL_COUNT = 33
EXPECTED_TARGET_IDS = frozenset(
    {
        "minisgl-cuda-graph-replay-0a08bed4-148",
        "minisgl-cuda-graph-replay-0a08bed4-151",
        "minisgl-cuda-graph-replay-0a08bed4-153",
        "minisgl-radix-prefix-reuse-75f40470-86",
        "minisgl-radix-prefix-reuse-75f40470-87",
        "minisgl-radix-prefix-reuse-75f40470-88",
        "mixed-greedy-sampler-6448ef46-186",
        "mixed-greedy-sampler-6448ef46-187",
        "mixed-greedy-sampler-6448ef46-189",
    }
)
EXPECTED_SOURCE_IDS = frozenset(
    {
        "minisgl-cuda-graph-replay-0a08bed4-134",
        "minisgl-cuda-graph-replay-0a08bed4-137",
        "minisgl-cuda-graph-replay-0a08bed4-141",
        "minisgl-radix-prefix-reuse-75f40470-73",
        "minisgl-radix-prefix-reuse-75f40470-74",
        "minisgl-radix-prefix-reuse-75f40470-75",
        "mixed-greedy-sampler-6448ef46-172",
        "mixed-greedy-sampler-6448ef46-173",
        "mixed-greedy-sampler-6448ef46-176",
    }
)
EXPECTED_TASK_COUNTS = {
    "minisgl-cuda-graph-replay-0a08bed4": 11,
    "minisgl-radix-prefix-reuse-75f40470": 11,
    "mixed-greedy-sampler-6448ef46": 11,
}


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(f"GUARD FAILED: {message}")


def _validate_experiment(
    row: Mapping[str, Any] | None, *, expected_id: str, expected_name: str
) -> None:
    _require(row is not None, f"experiment {expected_id} does not exist")
    assert row is not None
    _require(row["id"] == expected_id, f"unexpected experiment id {row['id']!r}")
    _require(
        row["name"] == expected_name,
        f"experiment {expected_id} name is {row['name']!r}, expected {expected_name!r}",
    )
    _require(row["deleted_at"] is None, f"experiment {expected_id} is deleted")


def _validate_target_rows(
    rows: Sequence[Mapping[str, Any]], *, expected_model: str, desired_model: str
) -> None:
    _require(
        len(rows) == EXPECTED_TRIAL_COUNT,
        f"target experiment has {len(rows)} live trials, expected {EXPECTED_TRIAL_COUNT}",
    )
    rows_by_id = {str(row["id"]): row for row in rows}
    _require(len(rows_by_id) == len(rows), "duplicate trial ids returned")
    _require(
        dict(Counter(str(row["task_id"]) for row in rows)) == EXPECTED_TASK_COUNTS,
        "target task distribution differs from the expected 11/11/11",
    )
    target_ids = EXPECTED_TARGET_IDS.intersection(rows_by_id)
    _require(
        target_ids == EXPECTED_TARGET_IDS,
        "exact target id set is not present: "
        f"missing={sorted(EXPECTED_TARGET_IDS - target_ids)}",
    )

    sensitive_models = {SOURCE_MODEL, PREVIOUS_MODEL, DISPLAY_MODEL}
    sensitive_ids = {
        str(row["id"])
        for row in rows
        if row["agent"] == EXPECTED_AGENT
        or row["provider"] == EXPECTED_PROVIDER
        or row["model"] in sensitive_models
    }
    _require(
        sensitive_ids == EXPECTED_TARGET_IDS,
        "model-bearing row set differs from the exact nine targets: "
        f"found={sorted(sensitive_ids)}",
    )

    for trial_id in sorted(EXPECTED_TARGET_IDS):
        row = rows_by_id[trial_id]
        checks = {
            "experiment_id": TARGET_EXPERIMENT_ID,
            "agent": EXPECTED_AGENT,
            "provider": EXPECTED_PROVIDER,
            "queue_key": EXPECTED_QUEUE_KEY,
            "model": expected_model,
        }
        for field, expected in checks.items():
            _require(
                row[field] == expected,
                f"{trial_id} {field} is {row[field]!r}, expected {expected!r}",
            )
        _require(row["deleted_at"] is None, f"{trial_id} is deleted")
        _require(
            row["superseded_by_trial_id"] is None,
            f"{trial_id} has been superseded",
        )
        _require(
            str(row["status"]).lower() == "success",
            f"{trial_id} status is {row['status']!r}, expected success",
        )
        _require(
            desired_model != row["model"],
            f"{trial_id} already has desired model {desired_model!r}",
        )


def _validate_source_rows(rows: Sequence[Mapping[str, Any]]) -> None:
    _require(
        len(rows) == EXPECTED_TRIAL_COUNT,
        f"source experiment has {len(rows)} live trials, expected {EXPECTED_TRIAL_COUNT}",
    )
    _require(
        dict(Counter(str(row["task_id"]) for row in rows)) == EXPECTED_TASK_COUNTS,
        "source task distribution differs from the expected 11/11/11",
    )
    sensitive_models = {SOURCE_MODEL, PREVIOUS_MODEL, DISPLAY_MODEL}
    source_xai = [
        row
        for row in rows
        if row["agent"] == EXPECTED_AGENT
        or row["provider"] == EXPECTED_PROVIDER
        or row["model"] in sensitive_models
    ]
    _require(
        {str(row["id"]) for row in source_xai} == EXPECTED_SOURCE_IDS,
        "source xAI/Grok trial IDs do not exactly match the allowlist",
    )
    for row in source_xai:
        _require(row["agent"] == EXPECTED_AGENT, f"source {row['id']} agent drifted")
        _require(
            row["provider"] == EXPECTED_PROVIDER,
            f"source {row['id']} provider drifted",
        )
        _require(
            row["model"] == SOURCE_MODEL,
            f"source {row['id']} model is {row['model']!r}, expected {SOURCE_MODEL!r}",
        )
        _require(
            row["superseded_by_trial_id"] is None,
            f"source {row['id']} has been superseded",
        )
        _require(row["deleted_at"] is None, f"source {row['id']} is deleted")
    _require(
        not EXPECTED_TARGET_IDS.intersection(str(row["id"]) for row in rows),
        "copy and source unexpectedly share trial ids",
    )


async def _run(*, apply: bool, rollback: bool) -> None:
    from sqlalchemy import text

    from oddish.config import Settings

    Settings.db_use_null_pool = True
    from oddish.db import get_session

    expected_model = DISPLAY_MODEL if rollback else PREVIOUS_MODEL
    desired_model = PREVIOUS_MODEL if rollback else DISPLAY_MODEL
    mode = "ROLLBACK" if rollback else "RELABEL"

    async with get_session() as session:
        try:
            target_experiment = (
                await session.execute(
                    text(
                        """
                        SELECT id, name, org_id, deleted_at
                        FROM experiments
                        WHERE id = :experiment_id AND deleted_at IS NULL
                        FOR UPDATE
                        """
                    ),
                    {"experiment_id": TARGET_EXPERIMENT_ID},
                )
            ).mappings().one_or_none()
            _validate_experiment(
                target_experiment,
                expected_id=TARGET_EXPERIMENT_ID,
                expected_name=TARGET_EXPERIMENT_NAME,
            )

            target_rows = (
                await session.execute(
                    text(
                        """
                        SELECT id, task_id, experiment_id, agent, provider,
                               queue_key, model, status, superseded_by_trial_id,
                               deleted_at
                        FROM trials
                        WHERE experiment_id = :experiment_id
                          AND deleted_at IS NULL
                          AND superseded_by_trial_id IS NULL
                        ORDER BY id
                        FOR UPDATE
                        """
                    ),
                    {"experiment_id": TARGET_EXPERIMENT_ID},
                )
            ).mappings().all()
            _validate_target_rows(
                target_rows,
                expected_model=expected_model,
                desired_model=desired_model,
            )

            source_experiment = (
                await session.execute(
                    text(
                        """
                        SELECT id, name, org_id, deleted_at
                        FROM experiments
                        WHERE id = :experiment_id AND deleted_at IS NULL
                        """
                    ),
                    {"experiment_id": SOURCE_EXPERIMENT_ID},
                )
            ).mappings().one_or_none()
            _validate_experiment(
                source_experiment,
                expected_id=SOURCE_EXPERIMENT_ID,
                expected_name=SOURCE_EXPERIMENT_NAME,
            )
            assert target_experiment is not None and source_experiment is not None
            _require(
                target_experiment["org_id"] == source_experiment["org_id"],
                "copy and source experiments belong to different organizations",
            )
            source_rows = (
                await session.execute(
                    text(
                        """
                        SELECT id, task_id, agent, provider, model,
                               superseded_by_trial_id, deleted_at
                        FROM trials
                        WHERE experiment_id = :experiment_id
                          AND deleted_at IS NULL
                          AND superseded_by_trial_id IS NULL
                        ORDER BY id
                        """
                    ),
                    {"experiment_id": SOURCE_EXPERIMENT_ID},
                )
            ).mappings().all()
            _validate_source_rows(source_rows)

            print(
                f"{mode} guards passed: experiment={TARGET_EXPERIMENT_ID} "
                f"rows={len(target_rows)} targets={len(EXPECTED_TARGET_IDS)} "
                f"{expected_model!r} -> {desired_model!r}"
            )
            if not apply:
                await session.rollback()
                print("DRY RUN: transaction rolled back; no rows changed")
                return

            changed = 0
            for trial_id in sorted(EXPECTED_TARGET_IDS):
                result = await session.execute(
                    text(
                        """
                        UPDATE trials
                        SET model = :desired_model
                        WHERE id = :trial_id
                          AND experiment_id = :experiment_id
                          AND deleted_at IS NULL
                          AND superseded_by_trial_id IS NULL
                          AND model = :expected_model
                          AND agent = :agent
                          AND provider = :provider
                          AND queue_key = :queue_key
                        """
                    ),
                    {
                        "desired_model": desired_model,
                        "trial_id": trial_id,
                        "experiment_id": TARGET_EXPERIMENT_ID,
                        "expected_model": expected_model,
                        "agent": EXPECTED_AGENT,
                        "provider": EXPECTED_PROVIDER,
                        "queue_key": EXPECTED_QUEUE_KEY,
                    },
                )
                _require(
                    result.rowcount == 1,
                    f"guarded update affected {result.rowcount} rows for {trial_id}",
                )
                changed += result.rowcount

            _require(changed == len(EXPECTED_TARGET_IDS), f"changed {changed} rows")
            verified_rows = (
                await session.execute(
                    text(
                        """
                        SELECT id, task_id, experiment_id, agent, provider,
                               queue_key, model, status, superseded_by_trial_id,
                               deleted_at
                        FROM trials
                        WHERE experiment_id = :experiment_id
                          AND deleted_at IS NULL
                          AND superseded_by_trial_id IS NULL
                        ORDER BY id
                        """
                    ),
                    {"experiment_id": TARGET_EXPERIMENT_ID},
                )
            ).mappings().all()
            _validate_target_rows(
                verified_rows,
                expected_model=desired_model,
                desired_model=expected_model,
            )
            await session.commit()
            print(
                f"APPLIED: changed exactly {changed} copied rows; "
                f"source {SOURCE_EXPERIMENT_ID} was not updated"
            )
        except BaseException:
            await session.rollback()
            raise


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="commit the guarded update")
    parser.add_argument(
        "--rollback",
        action="store_true",
        help=f"restore {DISPLAY_MODEL!r} to {PREVIOUS_MODEL!r}",
    )
    args = parser.parse_args()
    asyncio.run(_run(apply=args.apply, rollback=args.rollback))


if __name__ == "__main__":
    main()
