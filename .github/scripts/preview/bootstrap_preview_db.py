"""Build each preview-branch Alembic stack to a schema that matches the migrations.

A data-less Supabase preview branch inherits a *stale* snapshot of the parent
schema. The old bootstrap stamped ``alembic_version`` to the parent's current
revision before ``upgrade head``; when the inherited schema lagged it, the stamp
marked migrations applied whose DDL never ran, so the branch sat "at head" with
columns physically missing (``queue_slots.locked_at``, ...).

So we make the schema a pure function of the migrations, keyed on a marker
comment we stamp on ``public`` once we've built it:

* marker present -> ``alembic upgrade head`` (applies whatever the PR adds);
* marker absent  -> drop ``public``, materialize the schema from the model graph
  (``create_all``), stamp both stacks to head, set the marker.

A rebuild drops the branch's data, so we touch ``SCHEMA_REBUILT_FILE`` and
prepare_preview_database.sh re-seeds.
"""

import asyncio
import os
import subprocess
import sys
from pathlib import Path

from sqlalchemy import pool, text
from sqlalchemy.ext.asyncio import create_async_engine

# Alembic project roots. run_preview_migrations.sh invokes us from ``backend/``,
# so cwd is the backend stack and its sibling is the oddish stack. Oddish first:
# the cloud stack layers on top of the core schema.
STACKS = (
    Path.cwd().parent / "oddish",
    Path.cwd(),
)

# Comment stamped on ``public`` once we've built it from base. Its presence is
# what distinguishes a branch this script owns from a stale inherited snapshot.
SCHEMA_MARKER = "oddish-preview:schema-built-from-base"


def _upgrade_head(project: Path) -> None:
    subprocess.run(["alembic", "upgrade", "head"], cwd=project, check=True)


def _stamp_head(project: Path) -> None:
    subprocess.run(["alembic", "stamp", "head"], cwd=project, check=True)


def _branch_db_url() -> str:
    url = os.environ["ODDISH_DATABASE_URL"]
    # Mirror alembic/env.py: ensure the async driver so create_async_engine
    # doesn't fall back to a sync DBAPI.
    if url.startswith("postgresql://") and "+asyncpg" not in url:
        url = url.replace("postgresql://", "postgresql+asyncpg://", 1)
    return url


def _engine(url: str):
    # Supabase poolers (transaction mode) can't reuse asyncpg's cached prepared
    # statements, mirroring oddish.db.connection.
    return create_async_engine(
        url, connect_args={"statement_cache_size": 0}, poolclass=pool.NullPool
    )


async def _schema_trusted(url: str) -> bool:
    """True when ``public`` carries our marker -- i.e. this script built it, so
    its ``alembic_version`` honestly reflects the schema and we can just upgrade.
    """
    engine = _engine(url)
    try:
        async with engine.connect() as conn:
            marker = await conn.scalar(
                # Two-arg form: the one-arg obj_description() is unreliable for
                # schema comments (it isn't scoped to pg_namespace).
                text(
                    "SELECT obj_description('public'::regnamespace, 'pg_namespace')"
                )
            )
    finally:
        await engine.dispose()
    return marker == SCHEMA_MARKER


async def _reset_public_schema(url: str) -> None:
    """Drop and recreate ``public`` so the next upgrade builds from an empty DB.

    Safe here: Supabase keeps auth/storage in their own schemas, and no oddish
    or backend migration creates an extension, so nothing outside the migration
    chain lives in ``public``.
    """
    engine = _engine(url)
    try:
        async with engine.begin() as conn:
            await conn.execute(text("DROP SCHEMA IF EXISTS public CASCADE"))
            await conn.execute(text("CREATE SCHEMA public"))
            await conn.execute(text("GRANT ALL ON SCHEMA public TO public"))
    finally:
        await engine.dispose()


async def _create_all_from_models(url: str) -> None:
    """Materialize the full schema from the combined model graph.

    The oddish and backend stacks share one ``Base``. Importing the backend
    models registers the cloud tables (``organizations``, ...) on it, so the
    cross-stack FK ``api_keys.org_id -> organizations`` resolves -- which a
    standalone oddish ``create_all`` (``000_initial_schema``, run by the oddish
    stack alone) cannot. This mirrors ``oddish.db.connection.init_db``.
    """
    backend_dir = str(Path.cwd())  # run_preview_migrations.sh runs us from backend/
    if backend_dir not in sys.path:
        sys.path.insert(0, backend_dir)
    import models  # noqa: F401  registers the cloud tables on the shared Base
    from oddish.db.models import Base

    engine = _engine(url)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
    finally:
        await engine.dispose()


async def _mark_trusted(url: str) -> None:
    engine = _engine(url)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(f"COMMENT ON SCHEMA public IS '{SCHEMA_MARKER}'")
            )
    finally:
        await engine.dispose()


def main() -> None:
    url = _branch_db_url()

    if asyncio.run(_schema_trusted(url)):
        for project in STACKS:
            _upgrade_head(project)
        rebuilt = False
    else:
        print(
            "bootstrap_preview_db: branch schema is untrusted (fresh snapshot or "
            "old-stamp poison); rebuilding from base",
            file=sys.stderr,
        )
        asyncio.run(_reset_public_schema(url))
        asyncio.run(_create_all_from_models(url))
        for project in STACKS:
            _stamp_head(project)
        asyncio.run(_mark_trusted(url))
        rebuilt = True

    sentinel = os.environ.get("SCHEMA_REBUILT_FILE")
    if rebuilt and sentinel:
        Path(sentinel).write_text("1")


if __name__ == "__main__":
    main()
