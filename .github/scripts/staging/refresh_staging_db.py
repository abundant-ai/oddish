"""Mirror ALL prod app data into the staging branch (spec D8).

Truncate-and-COPY every public table except the exclusions, in FK-topological
order, then apply the quiesce transform. Known FK back-edges (the same two
preview_seed declares) are nulled during load and patched afterward.
Self-enforcing: unclassified tables or unexpected FK cycles abort the run.
"""

import asyncio
import os
import re
import shutil
import sys
import tempfile
import time

import asyncpg

EXCLUDE = {
    # Alembic pointers: staging runs AHEAD of prod — never overwrite.
    "alembic_version_oddish", "alembic_version_backend", "alembic_version",
    # Runtime/queue state: rebuilds itself.
    "queue_slots", "trial_events", "_preview_seed_state",
    # Prod idempotency claims must not dedupe staging submissions.
    "submission_idempotency",
    # Prod API keys must never authenticate against staging — they would blur
    # cost/usage attribution across environments. Staging keys are minted per
    # person from the staging dashboard. (Purged below as well, since an
    # excluded table is not truncated and may hold rows from an earlier run.)
    "api_keys",
}
# (task, column) back-edges nulled during COPY and patched after — mirrors
# preview_seed._BACKEDGES + _LINKAGE_COLUMNS.
BACKEDGES = {("tasks", "current_version_id"), ("trials", "superseded_by_trial_id")}

QUIESCE = [
    "UPDATE worker_jobs SET status='CANCELLED' WHERE status::text NOT IN ('SUCCESS','FAILED','CANCELLED')",
    # SKIPPED is terminal in its own right (the baseline gate cancelled the
    # trial); coercing it to FAILED would erase that distinction on every mirror.
    "UPDATE trials SET status='FAILED' "
    "WHERE status::text NOT IN ('SUCCESS','FAILED','SKIPPED')",
    "UPDATE tasks  SET status='FAILED' WHERE status::text NOT IN ('COMPLETED','FAILED')",
]

# Destination-only hygiene applied after the load. Separate from QUIESCE
# (which neutralises copied in-flight work) because this enforces an
# environment boundary rather than fixing job state.
PURGE = [
    "DELETE FROM api_keys",
    # Excluded tables are never truncated, so prod's idempotency claims would
    # otherwise persist across mirrors and silently dedupe staging submissions.
    "DELETE FROM submission_idempotency",
]


def _plain(url: str) -> str:
    return url.replace("postgresql+asyncpg://", "postgresql://", 1)


def _session_pooler(url: str) -> str:
    # Prod's app URL rides the transaction pooler (6543), which reaps
    # long-lived transactions — the multi-minute snapshot COPYs must use
    # the session pooler (5432) on the same host. No-op for URLs already
    # on 5432 or direct connections.
    return url.replace(":6543/", ":5432/", 1)


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


async def _connect_with_retry(url: str, attempts: int = 12, delay: float = 15.0):
    # The staging branch restarts on compute/disk changes, and its pooler can
    # report healthy while still refusing connections for a short window
    # (observed: econnrefused seconds after ACTIVE_HEALTHY). Retry connects.
    last: Exception | None = None
    for i in range(1, attempts + 1):
        try:
            return await asyncpg.connect(url, statement_cache_size=0)
        except (asyncpg.PostgresConnectionError, ConnectionError, OSError) as exc:
            last = exc
            print(f"connect attempt {i}/{attempts} failed: {exc!r}; retrying in {delay:.0f}s",
                  flush=True)
            await asyncio.sleep(delay)
    raise last  # type: ignore[misc]


async def _src_connect(url: str):
    return await _connect_with_retry(url)


def _pick_spool_dir() -> str:
    # The per-table spool (src -> tempfile -> dst) can run tens of GB for the
    # largest tables, and this job runs inside a container (ghcr.io ci-base)
    # on a GitHub-hosted runner, where neither the container's default temp
    # dir nor a host-mounted volume is reliably the roomier option (and /mnt
    # may not even be mounted in-container). Measure the real candidates and
    # spool wherever there's the most free space.
    candidates = ["/__w", os.environ.get("RUNNER_TEMP"), tempfile.gettempdir()]
    best_dir, best_free = tempfile.gettempdir(), -1
    for c in candidates:
        if not c or not os.path.isdir(c) or not os.access(c, os.W_OK):
            continue
        try:
            free = shutil.disk_usage(c).free
        except OSError:
            continue
        if free > best_free:
            best_dir, best_free = c, free
    return best_dir


async def _run_mirror() -> None:
    spool_dir = _pick_spool_dir()
    print(f"spool dir: {spool_dir} ({shutil.disk_usage(spool_dir).free / 1e9:.2f} GB free)",
          flush=True)
    src_url = _session_pooler(_plain(os.environ["SOURCE_DB_URL"]))
    holder = await _src_connect(src_url)
    dst = await _connect_with_retry(_plain(os.environ["TARGET_DB_URL"]))
    try:
        src_tables, dst_tables = await _tables(holder), await _tables(dst)
        include = (src_tables & dst_tables) - EXCLUDE
        unclassified = (src_tables - dst_tables - EXCLUDE)
        if unclassified:
            sys.exit(f"Tables on prod but not staging (run migrations first?): {sorted(unclassified)}")
        order = await _topo_order(holder, include)
        backedge_cols = {t: [c for (bt, c) in BACKEDGES if bt == t] for t in include}

        # The holder pins ONE repeatable-read snapshot for the whole run and is kept
        # warm by a keepalive ping; every table then reads through its own
        # short-lived connection importing that snapshot, so no source connection
        # ever idles longer than its own active COPY (the observed kill mode:
        # the single source socket sat idle through 30+ minute destination uploads).
        async with holder.transaction(isolation="repeatable_read", readonly=True):
            await holder.execute("SET LOCAL statement_timeout = 0")
            await holder.execute("SET LOCAL idle_in_transaction_session_timeout = 0")
            snapshot_id = await holder.fetchval("SELECT pg_export_snapshot()")
            assert re.fullmatch(r"[0-9A-Fa-f\-]+", snapshot_id)
            holder_lock = asyncio.Lock()
            stop_ping = asyncio.Event()

            async def _keepalive() -> None:
                while not stop_ping.is_set():
                    try:
                        await asyncio.wait_for(stop_ping.wait(), timeout=60)
                    except asyncio.TimeoutError:
                        async with holder_lock:
                            await holder.execute("SELECT 1")

            ping_task = asyncio.create_task(_keepalive())

            async def _copy_table_from_src(t: str, select_cols: str, cols: list[str]) -> None:
                with tempfile.TemporaryFile(dir=spool_dir) as spool:
                    conn = await _src_connect(src_url)
                    try:
                        async with conn.transaction(isolation="repeatable_read", readonly=True):
                            await conn.execute("SET LOCAL statement_timeout = 0")
                            await conn.execute(f"SET TRANSACTION SNAPSHOT '{snapshot_id}'")
                            await conn.copy_from_query(
                                f'SELECT {select_cols} FROM "{t}"', output=spool)
                    finally:
                        await conn.close()
                    # Source is fully closed before the (potentially very long)
                    # destination upload starts — no source connection ever idles
                    # through it (the observed kill mode on both pooler modes).
                    spool.seek(0)
                    await dst.copy_to_table(t, source=spool, columns=cols)

            try:
                async with dst.transaction():
                    await dst.execute("SET LOCAL statement_timeout = 0")
                    await dst.execute("SET LOCAL synchronous_commit = off")
                    for t in reversed(order):
                        await dst.execute(f'TRUNCATE TABLE "{t}" CASCADE')
                    for t in order:
                        started = time.monotonic()
                        async with holder_lock:
                            cols = [r["column_name"] for r in await holder.fetch(
                                "SELECT column_name FROM information_schema.columns "
                                "WHERE table_schema='public' AND table_name=$1 ORDER BY ordinal_position", t)]
                        select_cols = ", ".join(
                            f'NULL AS "{c}"' if c in backedge_cols.get(t, []) else f'"{c}"' for c in cols)
                        attempts = 0
                        while True:
                            attempts += 1
                            try:
                                async with dst.transaction():   # savepoint
                                    # CASCADE, not plain TRUNCATE: Postgres refuses to
                                    # truncate a table with an existing FK reference
                                    # from another table regardless of whether that
                                    # DELETE, not TRUNCATE: plain TRUNCATE errors
                                    # structurally when any FK references t, and
                                    # TRUNCATE ... CASCADE follows EVERY FK edge —
                                    # including the back-edges dropped from the topo
                                    # order (tasks.current_version_id -> task_versions),
                                    # so a task_versions retry would silently empty the
                                    # already-loaded tasks table. DELETE is safe: at
                                    # retry time no loaded row references t's rows
                                    # (children load later; back-edge columns are still
                                    # NULL until the patch phase).
                                    await dst.execute(f'DELETE FROM "{t}"')
                                    await _copy_table_from_src(t, select_cols, cols)
                                break
                            except (asyncpg.PostgresConnectionError,
                                    asyncpg.InterfaceError,
                                    ConnectionError, OSError) as exc:
                                if attempts >= 2:
                                    raise
                                print(f"retrying {t} after transient failure: {exc!r}", flush=True)
                                await asyncio.sleep(5)
                        n = await dst.fetchval(f'SELECT count(*) FROM "{t}"')
                        print(f"mirrored {t}: {n} rows in {time.monotonic() - started:.0f}s", flush=True)
                    for (t, col) in sorted(BACKEDGES):
                        if t not in include:
                            continue
                        pk = "id"
                        conn = await _src_connect(src_url)
                        try:
                            async with conn.transaction(isolation="repeatable_read", readonly=True):
                                await conn.execute(f"SET TRANSACTION SNAPSHOT '{snapshot_id}'")
                                rows = await conn.fetch(
                                    f'SELECT "{pk}", "{col}" FROM "{t}" WHERE "{col}" IS NOT NULL')
                        finally:
                            await conn.close()
                        # Set-based patch: row-by-row UPDATEs over the pooler ran
                        # for hours on millions of rows (observed: 4h silent).
                        # Stage the pairs in a typed temp table and join once.
                        started = time.monotonic()
                        if rows:
                            tmp = f"_patch_{t}_{col}"
                            await dst.execute(
                                f'CREATE TEMP TABLE "{tmp}" ON COMMIT DROP AS '
                                f'SELECT "{pk}", "{col}" FROM "{t}" WITH NO DATA')
                            await dst.copy_records_to_table(
                                tmp, records=[(r[pk], r[col]) for r in rows])
                            await dst.execute(
                                f'UPDATE "{t}" AS tgt SET "{col}" = p."{col}" '
                                f'FROM "{tmp}" AS p WHERE tgt."{pk}" = p."{pk}"')
                        print(f"patched {t}.{col}: {len(rows)} rows in "
                              f"{time.monotonic() - started:.0f}s", flush=True)
                    for stmt in QUIESCE:
                        tag = await dst.execute(stmt)
                        print(f"quiesce: {stmt.split(' WHERE')[0]} -> {tag}", flush=True)
                    for stmt in PURGE:
                        tag = await dst.execute(stmt)
                        print(f"purge: {stmt} -> {tag}", flush=True)
            finally:
                stop_ping.set()
                ping_task.cancel()
                try:
                    await ping_task
                except (asyncio.CancelledError, Exception):
                    pass
        live = await dst.fetchval(
            "SELECT count(*) FROM worker_jobs WHERE status::text NOT IN ('SUCCESS','FAILED','CANCELLED')")
        assert live == 0, f"{live} non-terminal worker_jobs after quiesce"
        print("mirror complete")
    finally:
        # A failed attempt must not leak connections into the next retry: close
        # both ends regardless of how _run_mirror exited, tolerating a
        # connection that is already dead/closed (e.g. the source drop that
        # motivated the outer retry in main()).
        try:
            await holder.close()
        except Exception:
            pass
        try:
            await dst.close()
        except Exception:
            pass


async def main() -> None:
    attempts = 3
    for attempt in range(1, attempts + 1):
        try:
            await _run_mirror()
            return
        except (asyncpg.PostgresConnectionError, asyncpg.InterfaceError,
                ConnectionError, OSError) as exc:
            if attempt >= attempts:
                raise
            print(f"mirror attempt {attempt}/{attempts} failed with a connection error "
                  f"({exc!r}); restarting the whole mirror with fresh connections "
                  f"and a fresh snapshot", flush=True)
            await asyncio.sleep(30)


if __name__ == "__main__":
    asyncio.run(main())
