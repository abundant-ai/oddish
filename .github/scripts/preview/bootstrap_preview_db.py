"""Build each preview-branch Alembic stack to a schema that matches the migrations."""

import asyncio
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
    return marker == SCHEMA_MARKER


async def _rebuild_schema(url: str) -> None:
    # Importing the backend models registers the cloud tables on the shared Base,
    # so create_all resolves the cross-stack api_keys -> organizations FK.
    backend_dir = str(Path.cwd())
    if backend_dir not in sys.path:
        sys.path.insert(0, backend_dir)
    import models  # noqa: F401
    from oddish.db.models import Base

    engine = _engine(url)
    try:
        async with engine.begin() as conn:
            await conn.execute(text("DROP SCHEMA IF EXISTS public CASCADE"))
            await conn.execute(text("CREATE SCHEMA public"))
            await conn.execute(text("GRANT ALL ON SCHEMA public TO public"))
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
    finally:
        await engine.dispose()


async def _mark_trusted(url: str) -> None:
    engine = _engine(url)
    try:
        async with engine.begin() as conn:
            await conn.execute(text(f"COMMENT ON SCHEMA public IS '{SCHEMA_MARKER}'"))
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
