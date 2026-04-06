"""Create a v1 TaskVersion row for every task that doesn't have one yet.

Also sets ``current_version_id`` on those tasks and pins their trials
so the frontend can display a version badge for all tasks.

Usage:
    # Dry run (default) -- prints what would change
    uv run python scripts/backfill_task_versions.py

    # Apply changes
    uv run python scripts/backfill_task_versions.py --apply
"""

from __future__ import annotations

import argparse
import asyncio

import asyncpg


# Direct connection (port 5432) to bypass pgbouncer statement timeout limits.
DB_URL = (
    "postgresql://postgres.lmmlhiospelnvmtjljnn:s5F74NtU4NPLrfyq"
    "@aws-1-us-east-2.pooler.supabase.com:5432/postgres"
)

COUNT_TASKS_WITHOUT_VERSIONS = """
    SELECT count(*)
    FROM tasks t
    LEFT JOIN task_versions tv ON tv.task_id = t.id
    WHERE tv.id IS NULL
"""

# Bulk-insert v1 rows for all tasks that lack any version.
BULK_INSERT_VERSIONS = """
    INSERT INTO task_versions (id, task_id, version, task_path, task_s3_key, created_at, updated_at)
    SELECT
        t.id || '-v1',
        t.id,
        1,
        t.task_path,
        t.task_s3_key,
        COALESCE(t.created_at, now()),
        now()
    FROM tasks t
    LEFT JOIN task_versions tv ON tv.task_id = t.id
    WHERE tv.id IS NULL
"""

# Point every task at its new v1 row (only for tasks that still have NULL).
BULK_UPDATE_TASK_PTRS = """
    UPDATE tasks
    SET current_version_id = id || '-v1'
    WHERE current_version_id IS NULL
      AND EXISTS (
          SELECT 1 FROM task_versions tv
          WHERE tv.id = tasks.id || '-v1'
      )
"""

# Pin all orphan trials to their task's v1 version.
BULK_PIN_TRIALS = """
    UPDATE trials
    SET task_version_id = task_id || '-v1'
    WHERE task_version_id IS NULL
      AND EXISTS (
          SELECT 1 FROM task_versions tv
          WHERE tv.id = trials.task_id || '-v1'
      )
"""


async def main(apply: bool) -> None:
    conn = await asyncpg.connect(DB_URL, statement_cache_size=0)

    try:
        count = await conn.fetchval(COUNT_TASKS_WITHOUT_VERSIONS)
        print(f"Tasks without a version row: {count}")

        if count == 0:
            print("Nothing to do.")
            return

        if not apply:
            print("Dry run -- pass --apply to execute.")
            return

        # Extend the statement timeout for these bulk operations.
        await conn.execute("SET statement_timeout = '300s'")

        print(f"\n1/3  Inserting {count} task_versions rows...")
        result = await conn.execute(BULK_INSERT_VERSIONS)
        print(f"     {result}")

        print("2/3  Setting current_version_id on tasks...")
        result = await conn.execute(BULK_UPDATE_TASK_PTRS)
        print(f"     {result}")

        print("3/3  Pinning orphan trials to v1...")
        result = await conn.execute(BULK_PIN_TRIALS)
        print(f"     {result}")

        remaining = await conn.fetchval(COUNT_TASKS_WITHOUT_VERSIONS)
        print(f"\nDone. Tasks still without versions: {remaining}")
    finally:
        await conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually execute the backfill (default is dry run)",
    )
    args = parser.parse_args()
    asyncio.run(main(args.apply))
