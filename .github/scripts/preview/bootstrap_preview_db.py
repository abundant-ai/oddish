"""Rehearse the prod migration path on the PR's preview branch.

On rebuild (fresh branch, invalidated trust marker, or auto-heal) the branch
gets a snapshot of production's ``public`` schema plus prod's Alembic version
pointers, is seeded with sampled prod rows, and then runs
``alembic upgrade head`` for oddish and backend -- the same commands, order,
and starting shape as the push-to-main migration workflow. The PR's new
migrations therefore execute here first, against prod-shaped schema and data,
instead of being stamped as applied. A trusted (already-built) branch skips
the snapshot and runs the same incremental ``upgrade head`` prod does.
"""

import asyncio
import hashlib
import os
import subprocess
import sys
from pathlib import Path

from sqlalchemy import pool, text
from sqlalchemy.ext.asyncio import create_async_engine

REPO_ROOT = Path(__file__).resolve().parents[3]
ODDISH_DIR = REPO_ROOT / "oddish"
BACKEND_DIR = REPO_ROOT / "backend"
SCRIPT_DIR = Path(__file__).resolve().parent
# Prod migration order: oddish first, then backend (supabase-db-migrations.yml).
STACKS = (ODDISH_DIR, BACKEND_DIR)
ALEMBIC_VERSION_TABLES = ("alembic_version_oddish", "alembic_version_backend")

SCHEMA_MARKER = "oddish-preview:prod-snapshot"


def _upgrade_head(project: Path) -> None:
    subprocess.run(["alembic", "upgrade", "head"], cwd=project, check=True)


def _branch_db_url() -> str:
    url = os.environ["ODDISH_DATABASE_URL"]
    if url.startswith("postgresql://") and "+asyncpg" not in url:
        url = url.replace("postgresql://", "postgresql+asyncpg://", 1)
    return url


def _source_db_url() -> str:
    """Production URL used read-only for the schema snapshot + pointer copy."""
    url = os.environ.get("PREVIEW_SAMPLE_SOURCE_DB_URL", "")
    if not url:
        raise RuntimeError(
            "bootstrap_preview_db: PREVIEW_SAMPLE_SOURCE_DB_URL is required to "
            "snapshot the production schema; refusing to rebuild without it"
        )
    if url.startswith("postgresql://") and "+asyncpg" not in url:
        url = url.replace("postgresql://", "postgresql+asyncpg://", 1)
    return url


def _libpq_url(url: str) -> str:
    """pg_dump/psql speak libpq URLs; strip the SQLAlchemy driver suffix."""
    return url.replace("postgresql+asyncpg://", "postgresql://", 1)


def _session_pooler_url(url: str) -> str:
    """Route pg_dump/psql through the SESSION pooler (port 5432).

    They set session-level state -- pg_dump hardens its connection with
    ``set_config('search_path', '', false)`` -- and on the transaction pooler
    (port 6543) that state sticks to a SHARED pooled backend after disconnect,
    poisoning later clients: their unqualified queries then fail with
    ``relation ... does not exist`` (observed on prod for ~7 minutes after the
    first snapshot dump). Session mode dedicates the backend to this one
    connection and discards its state on disconnect. Same host, same auth.
    """
    return _libpq_url(url).replace(":6543/", ":5432/", 1)


def _engine(url: str):
    return create_async_engine(
        url, connect_args={"statement_cache_size": 0}, poolclass=pool.NullPool
    )


def _load_base():
    """Import the backend + oddish models so the full cross-stack metadata is registered.

    Importing the backend models registers the cloud tables on the shared Base,
    so the fingerprint and the model-schema assert see both stacks.
    """
    for path in (BACKEND_DIR, ODDISH_DIR / "src"):
        path_str = str(path)
        if path_str not in sys.path:
            sys.path.insert(0, path_str)
    import models  # noqa: F401
    from oddish.db.models import Base

    return Base


def _index_descriptor(index) -> str:
    """One index's identity (name, columns, uniqueness) for the fingerprint.

    Folding indexes in means an index-only model change invalidates the cached
    schema; otherwise it would never reach a preview.
    """
    cols = ",".join(sorted(col.name for col in index.columns))
    return f"{index.name}({cols}){'!u' if index.unique else ''}"


def _fingerprint_metadata(metadata) -> str:
    """Stable short hash of a metadata's table + column + index names.

    Constraints are deliberately not fingerprinted: the branch schema derives
    from the prod snapshot + migration files (never from this metadata), so a
    constraint-only model edit with no migration changes nothing anywhere —
    preview and prod stay identical, which is the contract. The marker's only
    job is to notice inputs that would change the rebuilt schema or the assert.
    """
    parts = []
    for table in sorted(metadata.tables.values(), key=lambda t: t.name):
        cols = ",".join(sorted(col.name for col in table.columns))
        idxs = ",".join(sorted(_index_descriptor(ix) for ix in table.indexes))
        parts.append(f"{table.name}:{cols}:{idxs}")
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:16]


def _model_fingerprint() -> str:
    """Fingerprint of the current model graph.

    Folded into the trust marker so a schema cached by an earlier prep is reused
    only while it still matches the branch's *current* models. Without this, a
    branch that gains a column after its first prep -- a merge that adds a
    migration, or a re-pointed migration chain -- keeps its stale cached schema:
    the version is already at head, so ``alembic upgrade head`` is a no-op, the
    new column is never created, and every read of it 500s.
    """
    return _fingerprint_metadata(_load_base().metadata)


def _migration_fingerprint() -> str:
    """Hash of every migration file's path + contents across both stacks.

    Folded into the trust marker so that editing a migration in place (or
    adding one) invalidates the cached schema and forces a fresh snapshot
    rebuild -- an already-at-head branch would otherwise never notice.
    """
    digest = hashlib.sha256()
    for project in STACKS:
        for path in sorted((project / "alembic").rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            digest.update(path.relative_to(project).as_posix().encode())
            digest.update(b"\0")
            digest.update(path.read_bytes())
            digest.update(b"\0")
    return digest.hexdigest()[:16]


def _trust_marker() -> str:
    return f"{SCHEMA_MARKER}:{_model_fingerprint()}:{_migration_fingerprint()}"


async def _schema_trusted(url: str) -> bool:
    engine = _engine(url)
    try:
        async with engine.connect() as conn:
            # Two-arg form; one-arg obj_description() is unreliable for schema comments.
            marker = await conn.scalar(
                text("SELECT obj_description('public'::regnamespace, 'pg_namespace')")
            )
    finally:
        await engine.dispose()
    # Trust the cached schema only when it was built from the current models
    # AND the current migration files.
    return marker == _trust_marker()


def _assert_preview_branch(url: str) -> None:
    prod_ref = os.environ.get("SUPABASE_PROJECT_REF")
    source = os.environ.get("PREVIEW_SAMPLE_SOURCE_DB_URL", "")
    if (prod_ref and prod_ref in url) or (
        source and url.split("://", 1)[-1] == source.split("://", 1)[-1]
    ):
        raise RuntimeError(
            "bootstrap_preview_db: ODDISH_DATABASE_URL resolves to production "
            "(matched SUPABASE_PROJECT_REF / PREVIEW_SAMPLE_SOURCE_DB_URL); "
            "refusing to DROP SCHEMA. This script only rebuilds preview branches."
        )


async def _reset_schema(url: str) -> None:
    _assert_preview_branch(url)
    engine = _engine(url)
    try:
        async with engine.begin() as conn:
            await conn.execute(text("DROP SCHEMA IF EXISTS public CASCADE"))
            await conn.execute(text("CREATE SCHEMA public"))
            await conn.execute(text("GRANT ALL ON SCHEMA public TO public"))
    finally:
        await engine.dispose()


def _pg_dump_schema_cmd(source_url: str) -> list[str]:
    # --no-comments keeps prod's schema comments (if any) from ever colliding
    # with the trust marker; --no-owner/--no-privileges because prod's roles
    # don't matter on a branch (objects belong to the connecting role).
    return [
        "pg_dump",
        "--schema-only",
        "--no-owner",
        "--no-privileges",
        "--no-comments",
        "--schema=public",
        _session_pooler_url(source_url),
    ]


def _psql_restore_cmd(branch_url: str) -> list[str]:
    # One transaction pins a single pooled backend (transaction-mode Supavisor)
    # and makes the restore all-or-nothing; schema-only dumps never contain
    # CREATE INDEX CONCURRENTLY, so this is always transaction-safe.
    return [
        "psql",
        "--no-psqlrc",
        "--quiet",
        "-v",
        "ON_ERROR_STOP=1",
        "--single-transaction",
        _session_pooler_url(branch_url),
    ]


def _filter_schema_sql(sql: str) -> str:
    """Drop ``CREATE SCHEMA public;`` lines -- the schema is pre-created."""
    lines = [ln for ln in sql.splitlines() if ln.strip() != "CREATE SCHEMA public;"]
    return "\n".join(lines) + "\n"


def _dump_prod_schema() -> str:
    proc = subprocess.run(
        _pg_dump_schema_cmd(_source_db_url()),
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        sys.stderr.write(proc.stderr)
        raise RuntimeError(
            "bootstrap_preview_db: pg_dump of the production schema failed"
        )
    return _filter_schema_sql(proc.stdout)


def _restore_schema(url: str, sql: str) -> None:
    subprocess.run(_psql_restore_cmd(url), input=sql, text=True, check=True)


async def _fetch_prod_alembic_versions() -> dict[str, str]:
    """Read prod's Alembic pointers so the branch upgrades from the same spot."""
    engine = _engine(_source_db_url())
    versions: dict[str, str] = {}
    try:
        async with engine.connect() as conn:
            for table in ALEMBIC_VERSION_TABLES:
                result = await conn.execute(
                    text(f"SELECT version_num FROM {table}")  # noqa: S608
                )
                rows = result.scalars().all()
                if len(rows) != 1:
                    raise RuntimeError(
                        f"bootstrap_preview_db: prod {table} has {len(rows)} rows; "
                        "expected exactly 1 -- is prod mid-migration-incident?"
                    )
                versions[table] = rows[0]
    finally:
        await engine.dispose()
    return versions


async def _write_alembic_versions(url: str, versions: dict[str, str]) -> None:
    engine = _engine(url)
    try:
        async with engine.begin() as conn:
            for table, version in versions.items():
                await conn.execute(text(f"DELETE FROM {table}"))  # noqa: S608
                await conn.execute(
                    text(
                        f"INSERT INTO {table} (version_num) "  # noqa: S608
                        "VALUES (:version)"
                    ),
                    {"version": version},
                )
    finally:
        await engine.dispose()


def _run_seed() -> None:
    # Same interpreter (the backend venv under `uv run`); the seed reads
    # ODDISH_DATABASE_URL / PREVIEW_SAMPLE_SOURCE_DB_URL / PR_NUMBER from env.
    subprocess.run(
        [sys.executable, str(SCRIPT_DIR / "seed_preview_db.py")], check=True
    )


async def _assert_model_schema(url: str) -> None:
    """Every model table, column, and named index must exist in the schema.

    Catches model-without-migration drift right on the PR that introduces it.
    Constraint names are deliberately not compared: prod's historical FK/unique
    names legitimately differ from model-generated ones.
    """
    metadata = _load_base().metadata
    engine = _engine(url)
    try:
        async with engine.connect() as conn:
            rows = await conn.execute(
                text(
                    """
                    SELECT table_name, column_name
                    FROM information_schema.columns
                    WHERE table_schema = 'public'
                    """
                )
            )
            actual_columns: dict[str, set[str]] = {}
            for table_name, column_name in rows:
                actual_columns.setdefault(table_name, set()).add(column_name)
            index_rows = await conn.execute(
                text("SELECT indexname FROM pg_indexes WHERE schemaname = 'public'")
            )
            actual_indexes = {row[0] for row in index_rows}
    finally:
        await engine.dispose()

    missing: list[str] = []
    for table in sorted(metadata.tables.values(), key=lambda t: t.name):
        columns = actual_columns.get(table.name)
        if columns is None:
            missing.append(table.name)
            continue
        missing.extend(
            f"{table.name}.{column.name}"
            for column in table.columns
            if column.name not in columns
        )
        missing.extend(
            f"index {index.name}"
            for index in table.indexes
            if index.name and index.name not in actual_indexes
        )
    if missing:
        preview = ", ".join(missing[:20])
        extra = "" if len(missing) <= 20 else f", and {len(missing) - 20} more"
        raise RuntimeError(f"preview schema is missing model objects: {preview}{extra}")


async def _mark_trusted(url: str) -> None:
    marker = _trust_marker()
    engine = _engine(url)
    try:
        async with engine.begin() as conn:
            await conn.execute(text(f"COMMENT ON SCHEMA public IS '{marker}'"))
    finally:
        await engine.dispose()


def _snapshot_prod() -> tuple[str, dict[str, str]]:
    """Dump prod's schema and Alembic pointers as a CONSISTENT pair.

    The pointers are read before and after the dump: a mismatch means prod
    applied a migration mid-snapshot, and pairing the older schema with the
    newer pointers would make ``upgrade head`` silently skip that migration's
    DDL on the branch. Equal brackets mean no migration committed during the
    dump. One retry, then fail loudly.
    """
    for _attempt in range(2):
        before = asyncio.run(_fetch_prod_alembic_versions())
        schema_sql = _dump_prod_schema()
        after = asyncio.run(_fetch_prod_alembic_versions())
        if before == after:
            return schema_sql, after
        print(
            "bootstrap_preview_db: prod applied a migration mid-snapshot; "
            "retrying with a fresh dump",
            file=sys.stderr,
        )
    raise RuntimeError(
        "bootstrap_preview_db: prod migrated during two consecutive schema "
        "snapshots; re-run once prod's migration workflow settles"
    )


def _rebuild(url: str) -> None:
    """Snapshot prod, restore it into the branch, seed, then upgrade to head."""
    # Snapshot (consistent schema + pointer pair) before touching the branch:
    # if prod is unreachable or mid-migration, the branch is left as it was. A branch whose tree dropped a migration
    # (e.g. a reverted PR commit) is also healed here: the rebuild never
    # consults the branch's old pointer.
    schema_sql, versions = _snapshot_prod()
    asyncio.run(_reset_schema(url))
    _restore_schema(url, schema_sql)
    asyncio.run(_write_alembic_versions(url, versions))
    # Seed BEFORE upgrading so the PR's migrations run against realistic data,
    # exactly as they will on prod. The prod-shaped schema means every sampled
    # column fits 1:1.
    _run_seed()
    for project in STACKS:
        _upgrade_head(project)
    asyncio.run(_assert_model_schema(url))
    # Trust last: a cancelled run leaves the branch untrusted -> next run rebuilds.
    asyncio.run(_mark_trusted(url))


def _upgrade_and_verify(url: str) -> None:
    for project in STACKS:
        _upgrade_head(project)
    asyncio.run(_assert_model_schema(url))


def main() -> None:
    url = _branch_db_url()
    rebuilt = True
    healed = False
    if asyncio.run(_schema_trusted(url)):
        try:
            _upgrade_and_verify(url)
            rebuilt = False
        except Exception as exc:  # noqa: BLE001 - funnel into retry, then heal
            # Prod's convention is idempotent migration steps that are safe to
            # re-trigger (autocommit_block migrations commit step-by-step, so a
            # failure leaves partial state -- there is no rollback to lean on).
            # Retrying once rehearses exactly that re-trigger.
            print(
                f"bootstrap_preview_db: trusted-path upgrade failed ({exc}); "
                "retrying once on the partial state (prod re-trigger semantics)",
                file=sys.stderr,
            )
            try:
                _upgrade_and_verify(url)
                rebuilt = False
            except Exception as retry_exc:  # noqa: BLE001 - heal on retry failure
                print(
                    f"bootstrap_preview_db: retry failed ({retry_exc}); "
                    "auto-healing with a full rebuild from the prod snapshot",
                    file=sys.stderr,
                )
                _rebuild(url)
                healed = True
    else:
        print(
            "bootstrap_preview_db: untrusted schema; rebuilding from prod snapshot",
            file=sys.stderr,
        )
        _rebuild(url)

    sentinel = os.environ.get("SCHEMA_REBUILT_FILE")
    if rebuilt and sentinel:
        Path(sentinel).write_text("healed" if healed else "1")


if __name__ == "__main__":
    main()
