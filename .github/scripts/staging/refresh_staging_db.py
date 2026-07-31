"""Mirror ALL prod app data into the staging branch (spec D8).

Truncate-and-COPY every public table except the exclusions, in FK-topological
order, then apply the quiesce transform. Known FK back-edges (the same two
preview_seed declares) are nulled during load and patched afterward.
Self-enforcing: unclassified tables or unexpected FK cycles abort the run.
"""

import asyncio
import os
import sys
import tempfile

import asyncpg

EXCLUDE = {
    # Alembic pointers: staging runs AHEAD of prod — never overwrite.
    "alembic_version_oddish", "alembic_version_backend", "alembic_version",
    # Runtime/queue state: rebuilds itself.
    "queue_slots", "trial_events", "_preview_seed_state",
    # Prod idempotency claims must not dedupe staging submissions.
    "submission_idempotency",
}
# (task, column) back-edges nulled during COPY and patched after — mirrors
# preview_seed._BACKEDGES + _LINKAGE_COLUMNS.
BACKEDGES = {("tasks", "current_version_id"), ("trials", "superseded_by_trial_id")}

QUIESCE = [
    "UPDATE worker_jobs SET status='CANCELLED' WHERE status::text NOT IN ('SUCCESS','FAILED','CANCELLED')",
    "UPDATE trials SET status='FAILED' WHERE status::text NOT IN ('SUCCESS','FAILED')",
    "UPDATE tasks  SET status='FAILED' WHERE status::text NOT IN ('COMPLETED','FAILED')",
]


def _plain(url: str) -> str:
    return url.replace("postgresql+asyncpg://", "postgresql://", 1)


async def _tables(conn) -> set[str]:
    rows = await conn.fetch(
        "SELECT tablename FROM pg_tables WHERE schemaname='public'"
    )
    return {r["tablename"] for r in rows}


async def _topo_order(conn, include: set[str]) -> list[str]:
    fks = await conn.fetch("""
        SELECT tc.table_name AS child, ccu.table_name AS parent,
               kcu.column_name AS child_col
        FROM information_schema.table_constraints tc
        JOIN information_schema.key_column_usage kcu
          ON tc.constraint_name = kcu.constraint_name AND tc.table_schema = kcu.table_schema
        JOIN information_schema.constraint_column_usage ccu
          ON tc.constraint_name = ccu.constraint_name AND tc.table_schema = ccu.table_schema
        WHERE tc.constraint_type='FOREIGN KEY' AND tc.table_schema='public'
    """)
    deps: dict[str, set[str]] = {t: set() for t in include}
    for r in fks:
        c, p = r["child"], r["parent"]
        if c in include and p in include and c != p:
            if (c, r["child_col"]) in BACKEDGES:
                continue  # broken edge: loaded as NULL, patched later
            deps[c].add(p)
    order, done = [], set()
    while deps:
        ready = sorted(t for t, ds in deps.items() if ds <= done)
        if not ready:
            sys.exit(f"FK cycle not covered by BACKEDGES: {sorted(deps)}")
        for t in ready:
            order.append(t); done.add(t); deps.pop(t)
    return order


async def main() -> None:
    src = await asyncpg.connect(_plain(os.environ["SOURCE_DB_URL"]),
                                statement_cache_size=0)
    dst = await asyncpg.connect(_plain(os.environ["TARGET_DB_URL"]),
                                statement_cache_size=0)
    src_tables, dst_tables = await _tables(src), await _tables(dst)
    include = (src_tables & dst_tables) - EXCLUDE
    unclassified = (src_tables - dst_tables - EXCLUDE)
    if unclassified:
        sys.exit(f"Tables on prod but not staging (run migrations first?): {sorted(unclassified)}")
    order = await _topo_order(src, include)

    backedge_cols = {t: [c for (bt, c) in BACKEDGES if bt == t] for t in include}
    # One repeatable-read snapshot for every source read: cross-table FK
    # consistency against a live prod, and SET LOCAL (transaction-scoped,
    # therefore Supavisor-safe — only session-level SET poisons the shared
    # pooler backend) lifts prod's statement timeout for the big COPYs.
    async with src.transaction(isolation="repeatable_read", readonly=True):
        await src.execute("SET LOCAL statement_timeout = 0")
        await src.execute("SET LOCAL idle_in_transaction_session_timeout = 0")
        async with dst.transaction():
            await dst.execute("SET LOCAL statement_timeout = 0")
            for t in reversed(order):
                await dst.execute(f'TRUNCATE TABLE "{t}" CASCADE')
            for t in order:
                cols = [r["column_name"] for r in await src.fetch(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema='public' AND table_name=$1 ORDER BY ordinal_position", t)]
                select_cols = ", ".join(
                    f'NULL AS "{c}"' if c in backedge_cols.get(t, []) else f'"{c}"' for c in cols)
                with tempfile.TemporaryFile() as spool:
                    await src.copy_from_query(
                        f'SELECT {select_cols} FROM "{t}"', output=spool)
                    spool.seek(0)
                    await dst.copy_to_table(t, source=spool, columns=cols)
                n = await dst.fetchval(f'SELECT count(*) FROM "{t}"')
                print(f"mirrored {t}: {n} rows", flush=True)
            for (t, col) in sorted(BACKEDGES):
                if t not in include:
                    continue
                pk = "id"
                rows = await src.fetch(f'SELECT "{pk}", "{col}" FROM "{t}" WHERE "{col}" IS NOT NULL')
                for r in rows:
                    await dst.execute(
                        f'UPDATE "{t}" SET "{col}"=$1 WHERE "{pk}"=$2', r[col], r[pk])
                print(f"patched {t}.{col}: {len(rows)} rows", flush=True)
            for stmt in QUIESCE:
                tag = await dst.execute(stmt)
                print(f"quiesce: {stmt.split(' WHERE')[0]} -> {tag}", flush=True)
    live = await dst.fetchval(
        "SELECT count(*) FROM worker_jobs WHERE status::text NOT IN ('SUCCESS','FAILED','CANCELLED')")
    assert live == 0, f"{live} non-terminal worker_jobs after quiesce"
    await src.close(); await dst.close()
    print("mirror complete")


if __name__ == "__main__":
    asyncio.run(main())
