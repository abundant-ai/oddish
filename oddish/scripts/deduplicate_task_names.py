"""Rename duplicate task names so (org_id, name) becomes unique.

The oldest task in each group keeps its original name.  Later duplicates
get a ``-v1``, ``-v2``, … suffix (ordered by created_at).

Usage:
    # Dry run (default) — prints what would change
    uv run python scripts/deduplicate_task_names.py

    # Apply changes
    uv run python scripts/deduplicate_task_names.py --apply
"""

from __future__ import annotations

import argparse
import asyncio

import asyncpg


DB_URL = (
    "postgresql://postgres.lmmlhiospelnvmtjljnn:s5F74NtU4NPLrfyq"
    "@aws-1-us-east-2.pooler.supabase.com:6543/postgres"
)

# Window function assigns a 1-based row number within each (org, name)
# group ordered by created_at.  Row 1 = the original; rows 2+ need renaming.
FIND_ALL_DUPLICATES = """
    WITH numbered AS (
        SELECT
            id,
            name,
            COALESCE(org_id, '__null__') AS org_key,
            ROW_NUMBER() OVER (
                PARTITION BY COALESCE(org_id, '__null__'), name
                ORDER BY created_at ASC, id ASC
            ) AS rn
        FROM tasks
    )
    SELECT id, name, rn
    FROM numbered
    WHERE rn > 1
    ORDER BY name, rn
"""


async def main(apply: bool) -> None:
    conn = await asyncpg.connect(DB_URL, statement_cache_size=0)

    rows = await conn.fetch(FIND_ALL_DUPLICATES)
    print(f"Found {len(rows)} tasks to rename\n")

    if not rows:
        print("Nothing to do — all (org_id, name) pairs are already unique.")
        await conn.close()
        return

    renames: list[tuple[str, str, str]] = []  # (id, old_name, new_name)
    for row in rows:
        task_id = row["id"]
        old_name = row["name"]
        suffix = row["rn"] - 1  # first duplicate → v1, second → v2, …
        new_name = f"{old_name}-v{suffix}"
        renames.append((task_id, old_name, new_name))

    # Show summary by original name
    from collections import Counter
    name_counts = Counter(old for _, old, _ in renames)
    print(f"Affected original names: {len(name_counts)}")
    print(f"Top 10 by duplicate count:")
    for name, cnt in name_counts.most_common(10):
        print(f"  {cnt:>4} duplicates  {name}")
    print()

    # Show a few examples
    for task_id, old_name, new_name in renames[:10]:
        prefix = "  RENAME" if apply else "  [dry-run]"
        print(f"{prefix} {task_id}:  {old_name}  →  {new_name}")
    if len(renames) > 10:
        print(f"  ... and {len(renames) - 10} more")
    print()

    if apply:
        # Batch update using a single prepared query
        await conn.executemany(
            "UPDATE tasks SET name = $1 WHERE id = $2",
            [(new_name, task_id) for task_id, _, new_name in renames],
        )
        print(f"Renamed {len(renames)} tasks.")

        # Verify no duplicates remain
        remaining = await conn.fetchval("""
            SELECT COUNT(*) FROM (
                SELECT COALESCE(org_id, '__null__'), name
                FROM tasks
                GROUP BY COALESCE(org_id, '__null__'), name
                HAVING COUNT(*) > 1
            ) sub
        """)
        print(f"Remaining duplicate groups: {remaining}")
    else:
        print(f"Would rename {len(renames)} tasks.")
        print("Re-run with --apply to execute.")

    await conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually rename tasks (default is dry-run)",
    )
    args = parser.parse_args()
    asyncio.run(main(apply=args.apply))
