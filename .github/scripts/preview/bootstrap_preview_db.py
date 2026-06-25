"""Build each preview-branch Alembic stack to a schema that matches the migrations."""

import asyncio
import hashlib
import os
import subprocess
import sys
from pathlib import Path

from sqlalchemy import pool, text
from sqlalchemy.ext.asyncio import create_async_engine

STACKS = (
    Path.cwd().parent / "oddish",
    Path.cwd(),
)

SCHEMA_MARKER = "oddish-preview:schema-built-from-base"


def _upgrade_head(project: Path) -> None:
    subprocess.run(["alembic", "upgrade", "head"], cwd=project, check=True)


def _stamp_head(project: Path) -> None:
    subprocess.run(["alembic", "stamp", "head"], cwd=project, check=True)


def _branch_db_url() -> str:
    url = os.environ["ODDISH_DATABASE_URL"]
    if url.startswith("postgresql://") and "+asyncpg" not in url:
        url = url.replace("postgresql://", "postgresql+asyncpg://", 1)
    return url


def _engine(url: str):
    return create_async_engine(
        url, connect_args={"statement_cache_size": 0}, poolclass=pool.NullPool
    )


def _load_base():
    """Import the backend + oddish models so the full cross-stack metadata is registered.

    Importing the backend models registers the cloud tables on the shared Base,
    so create_all resolves the cross-stack ``api_keys -> organizations`` FK.
    """
    backend_dir = str(Path.cwd())
    if backend_dir not in sys.path:
        sys.path.insert(0, backend_dir)
    import models  # noqa: F401
    from oddish.db.models import Base

    return Base


def _index_descriptor(index) -> str:
    """Stable string for one index: name, its columns, and uniqueness.

    Indexes are folded into the fingerprint so a model that gains/loses an
    index (e.g. a unique index mirrored onto a model to match a migration)
    invalidates the cached schema -- create_all only builds indexes the models
    declare, so an index-only change would otherwise never reach a preview.
    """
    cols = ",".join(sorted(col.name for col in index.columns))
    return f"{index.name}({cols}){'!u' if index.unique else ''}"


def _fingerprint_metadata(metadata) -> str:
    """Stable short hash of a metadata's table + column + index names."""
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
    the version is already stamped at head, so ``alembic upgrade head`` is a
    no-op, the new column is never created, and every read of it 500s.
    """
    return _fingerprint_metadata(_load_base().metadata)


def _trust_marker() -> str:
    return f"{SCHEMA_MARKER}:{_model_fingerprint()}"


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
    # Trust the cached schema only when it was built from the *current* model graph.
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


async def _rebuild_schema(url: str) -> None:
    _assert_preview_branch(url)
    base = _load_base()

    engine = _engine(url)
    try:
        async with engine.begin() as conn:
            await conn.execute(text("DROP SCHEMA IF EXISTS public CASCADE"))
            await conn.execute(text("CREATE SCHEMA public"))
            await conn.execute(text("GRANT ALL ON SCHEMA public TO public"))
        async with engine.begin() as conn:
            await conn.run_sync(base.metadata.create_all)
    finally:
        await engine.dispose()


async def _mark_trusted(url: str) -> None:
    marker = _trust_marker()
    engine = _engine(url)
    try:
        async with engine.begin() as conn:
            await conn.execute(text(f"COMMENT ON SCHEMA public IS '{marker}'"))
    finally:
        await engine.dispose()


def main() -> None:
    url = _branch_db_url()

    if asyncio.run(_schema_trusted(url)):
        for project in STACKS:
            _upgrade_head(project)
        rebuilt = False
    else:
        print("bootstrap_preview_db: untrusted schema; rebuilding from base", file=sys.stderr)
        asyncio.run(_rebuild_schema(url))
        for project in STACKS:
            _stamp_head(project)
        asyncio.run(_mark_trusted(url))
        rebuilt = True

    sentinel = os.environ.get("SCHEMA_REBUILT_FILE")
    if rebuilt and sentinel:
        Path(sentinel).write_text("1")


if __name__ == "__main__":
    main()
