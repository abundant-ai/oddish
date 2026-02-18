from __future__ import annotations

import argparse
import asyncio

from sqlalchemy import and_, func, or_, select, text

from oddish.db import (
    AnalysisStatus,
    TaskModel,
    TaskStatus,
    TrialModel,
    VerdictStatus,
    get_session,
)
from oddish.queue import enqueue_verdict


STUCK_VERDICT_STATUSES = (
    VerdictStatus.PENDING,
    VerdictStatus.QUEUED,
    VerdictStatus.RUNNING,
)


async def _has_openai_verdict_job(task_id: str) -> bool:
    async with get_session() as session:
        result = await session.execute(
            text(
                """
                SELECT 1
                FROM pgqueuer
                WHERE payload IS NOT NULL
                  AND (convert_from(payload, 'UTF8')::jsonb ->> 'job_type') = 'verdict'
                  AND (convert_from(payload, 'UTF8')::jsonb ->> 'task_id') = :task_id
                  AND entrypoint = 'openai'
                  AND status IN ('queued', 'picked')
                LIMIT 1
                """
            ),
            {"task_id": task_id},
        )
        return result.scalar_one_or_none() is not None


async def _print_verdict_queue_counts() -> None:
    async with get_session() as session:
        result = await session.execute(
            text(
                """
                SELECT
                  entrypoint,
                  status::text AS status,
                  COUNT(*) AS count
                FROM pgqueuer
                WHERE payload IS NOT NULL
                  AND (convert_from(payload, 'UTF8')::jsonb ->> 'job_type') = 'verdict'
                  AND status IN ('queued', 'picked')
                GROUP BY entrypoint, status
                ORDER BY entrypoint, status
                """
            )
        )
        rows = result.all()

    if not rows:
        print("Verdict queue: no queued/picked jobs.")
        return

    print("Verdict queue counts (queued/picked):")
    for entrypoint, status, count in rows:
        print(f"  {entrypoint}: {status}={count}")


async def requeue_stuck_verdicts(limit: int | None, dry_run: bool) -> None:
    await _print_verdict_queue_counts()
    async with get_session() as session:
        pending_analysis_count = (
            select(func.count(TrialModel.id))
            .where(
                and_(
                    TrialModel.task_id == TaskModel.id,
                    or_(
                        TrialModel.analysis_status.is_(None),
                        TrialModel.analysis_status.in_(
                            [
                                AnalysisStatus.PENDING,
                                AnalysisStatus.QUEUED,
                                AnalysisStatus.RUNNING,
                            ]
                        ),
                    ),
                )
            )
            .scalar_subquery()
        )
        query = select(TaskModel).where(
            and_(
                TaskModel.status.in_(
                    [TaskStatus.ANALYZING, TaskStatus.VERDICT_PENDING]
                ),
                TaskModel.run_analysis.is_(True),
                TaskModel.verdict.is_(None),
                or_(
                    TaskModel.verdict_status.is_(None),
                    TaskModel.verdict_status.in_(STUCK_VERDICT_STATUSES),
                ),
                pending_analysis_count == 0,
            )
        )
        if limit:
            query = query.limit(limit)

        tasks = (await session.execute(query)).scalars().all()

    if not tasks:
        print("No eligible verdict tasks found.")
        return

    print(f"Eligible tasks: {len(tasks)}")

    requeued = 0
    skipped = 0

    for task in tasks:
        if task.verdict:
            print(f"Skipping {task.id}: verdict already present.")
            skipped += 1
            continue

        if await _has_openai_verdict_job(task.id):
            print(f"Skipping {task.id}: openai verdict job already queued.")
            skipped += 1
            continue

        if dry_run:
            print(f"[dry-run] Would requeue verdict for {task.id}")
            requeued += 1
            continue

        async with get_session() as session:
            task_row = await session.get(TaskModel, task.id)
            if not task_row:
                print(f"Skipping {task.id}: task not found.")
                skipped += 1
                continue

            task_row.verdict_status = VerdictStatus.QUEUED
            task_row.verdict_error = None
            task_row.verdict_started_at = None
            task_row.verdict_finished_at = None
            await enqueue_verdict(session, task_row.id)
            await session.commit()

        print(f"Requeued verdict for {task.id}")
        requeued += 1

    print(f"Done. Requeued: {requeued}, Skipped: {skipped}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Requeue verdict jobs for stuck tasks."
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Limit number of tasks to requeue.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print actions without enqueuing.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    asyncio.run(requeue_stuck_verdicts(limit=args.limit, dry_run=args.dry_run))


if __name__ == "__main__":
    main()
