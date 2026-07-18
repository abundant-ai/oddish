"""Apply legacy_imported_at_001 to prod, deadlock-resistant.

ONE-OFF MIGRATION SCRIPT -- TEMPORARY BY DESIGN.
Committed as a record of how the Sauron -> Oddish migration was run, not as
general-purpose tooling. It applies exactly one alembic revision by hand and
should be deleted once the migration is finished. Do not build on it or reach
for it as a general "apply a migration" tool -- normal migrations go through
CI (.github/workflows/supabase-db-migrations.yml).

Why this exists: CI's `alembic upgrade head` wraps all 9 statements in ONE
transaction, so it holds AccessExclusiveLock on tasks + trials + experiments
simultaneously. A live queue query holding trials and wanting tasks deadlocked
it (run 29653194459). Every statement here is metadata-only (nullable columns,
no default) or a partial index matching zero rows, so none of them are slow -
the only hard part is acquiring the lock without cycling.

Fixes applied:
  - one statement per transaction (autocommit), so two hot tables are never
    locked at the same time
  - lock_timeout, so we back off cleanly instead of deadlocking
  - retry with backoff on deadlock / lock-not-available
  - alembic_version_oddish stamped only after every statement succeeds, and
    only if it is still sitting on the expected parent revision

Idempotent: every DDL uses IF NOT EXISTS, the stamp is a guarded UPDATE.

Usage:
    modal run backend/scripts/legacy_prod_apply_migration.py            # dry run
    modal run backend/scripts/legacy_prod_apply_migration.py --execute
"""

import os

import modal

app = modal.App("oddish-legacy-apply-migration")
image = modal.Image.debian_slim(python_version="3.13").pip_install("asyncpg")
secret = modal.Secret.from_name("oddish-prod", environment_name="main")

REVISION = "legacy_imported_at_001"
PARENT = "analysis_costs_001"
VERSION_TABLE = "alembic_version_oddish"

# Exactly the statements from
# oddish/alembic/versions/legacy_imported_at_001_add_imported_at.py::upgrade()
STATEMENTS = [
    ("tasks", "ALTER TABLE tasks       ADD COLUMN IF NOT EXISTS imported_at TIMESTAMPTZ"),
    ("trials", "ALTER TABLE trials      ADD COLUMN IF NOT EXISTS imported_at TIMESTAMPTZ"),
    ("experiments", "ALTER TABLE experiments ADD COLUMN IF NOT EXISTS imported_at TIMESTAMPTZ"),
    ("trials", "ALTER TABLE trials      ADD COLUMN IF NOT EXISTS orig_s3_src TEXT"),
    ("experiments", "ALTER TABLE experiments ADD COLUMN IF NOT EXISTS orig_s3_src TEXT"),
    (
        "tasks",
        "CREATE INDEX IF NOT EXISTS idx_tasks_imported_at "
        "ON tasks (imported_at) WHERE imported_at IS NOT NULL",
    ),
    (
        "trials",
        "CREATE INDEX IF NOT EXISTS idx_trials_imported_at "
        "ON trials (imported_at) WHERE imported_at IS NOT NULL",
    ),
    (
        "experiments",
        "CREATE INDEX IF NOT EXISTS idx_experiments_imported_at "
        "ON experiments (imported_at) WHERE imported_at IS NOT NULL",
    ),
    (
        "trials",
        "CREATE INDEX IF NOT EXISTS idx_trials_orig_s3_src "
        "ON trials (orig_s3_src) WHERE orig_s3_src IS NOT NULL",
    ),
]

EXPECTED_COLUMNS = [
    ("tasks", "imported_at"),
    ("trials", "imported_at"),
    ("trials", "orig_s3_src"),
    ("experiments", "imported_at"),
    ("experiments", "orig_s3_src"),
]

MAX_ATTEMPTS = 6
LOCK_TIMEOUT = "5s"


@app.function(image=image, secrets=[secret], timeout=1800)
async def apply(execute: bool) -> bool:
    import asyncio

    import asyncpg

    url = os.environ["ODDISH_DATABASE_URL"].replace("postgresql+asyncpg://", "postgresql://")
    conn = await asyncpg.connect(url, statement_cache_size=0)

    host = url.split("@")[-1].split("/")[0] if "@" in url else "?"
    print(f"[info] db host          {host}")
    print(f"[info] mode             {'EXECUTE' if execute else 'DRY RUN (no writes)'}")

    current = await conn.fetchval(f"SELECT version_num FROM {VERSION_TABLE}")  # noqa: S608
    print(f"[info] {VERSION_TABLE}  {current}")

    if current == REVISION:
        print(f"\nAlready at {REVISION}. Nothing to do.")
        await conn.close()
        return True
    if current != PARENT:
        print(f"\nREFUSING: expected parent {PARENT!r}, found {current!r}.")
        print("Someone else moved the chain. Investigate before applying by hand.")
        await conn.close()
        return False

    if not execute:
        print("\nWould run, each in its own transaction:")
        for table, sql in STATEMENTS:
            print(f"  [{table:12}] {' '.join(sql.split())}")
        print(f"\nThen stamp {VERSION_TABLE}: {PARENT} -> {REVISION}")
        print("\nDry run only. Re-run with --execute.")
        await conn.close()
        return True

    # Back off cleanly rather than sitting in a lock queue behind live traffic.
    await conn.execute(f"SET lock_timeout = '{LOCK_TIMEOUT}'")
    print(f"[info] lock_timeout     {LOCK_TIMEOUT}\n")

    for table, sql in STATEMENTS:
        label = " ".join(sql.split())[:72]
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                # No explicit transaction => asyncpg autocommits this single
                # statement, so the lock is released the instant it returns.
                await conn.execute(sql)
                print(f"[ok]   {label}")
                break
            except (
                asyncpg.exceptions.DeadlockDetectedError,
                asyncpg.exceptions.LockNotAvailableError,
                asyncpg.exceptions.QueryCanceledError,
            ) as exc:
                kind = type(exc).__name__
                if attempt == MAX_ATTEMPTS:
                    print(f"[FAIL] {label}\n       gave up after {attempt} attempts: {kind}")
                    await conn.close()
                    return False
                backoff = min(2 ** (attempt - 1), 8)
                print(f"[retry {attempt}/{MAX_ATTEMPTS}] {table} contended ({kind}), {backoff}s")
                await asyncio.sleep(backoff)

    # Verify the schema really landed before touching alembic's bookkeeping.
    print()
    missing = []
    for table, col in EXPECTED_COLUMNS:
        found = await conn.fetchval(
            """
            SELECT 1 FROM information_schema.columns
            WHERE table_schema='public' AND table_name=$1 AND column_name=$2
            """,
            table,
            col,
        )
        print(f"[{'ok' if found else 'FAIL'}]   column {table}.{col}")
        if not found:
            missing.append(f"{table}.{col}")

    if missing:
        print(f"\nNOT stamping: columns still missing: {', '.join(missing)}")
        await conn.close()
        return False

    stamped = await conn.execute(
        f"UPDATE {VERSION_TABLE} SET version_num = $1 WHERE version_num = $2",  # noqa: S608
        REVISION,
        PARENT,
    )
    print(f"\n[ok]   stamped {VERSION_TABLE}: {PARENT} -> {REVISION}  ({stamped})")

    final = await conn.fetchval(f"SELECT version_num FROM {VERSION_TABLE}")  # noqa: S608
    await conn.close()

    ok = final == REVISION
    print("\n" + "=" * 60)
    print(f"{'DONE' if ok else 'FAILED'} - {VERSION_TABLE} now at {final}")
    return ok


@app.local_entrypoint()
def main(execute: bool = False) -> None:
    ok = apply.remote(execute=execute)
    if not ok:
        raise SystemExit(1)
