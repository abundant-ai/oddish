"""Build preview schemas from migrations."""

import asyncio
import hashlib
import importlib
import os
import subprocess
import sys
from pathlib import Path

from sqlalchemy import pool, text
from sqlalchemy.ext.asyncio import create_async_engine

REPO_ROOT = Path(__file__).resolve().parents[3]
ODDISH_DIR = REPO_ROOT / "oddish"
BACKEND_DIR = REPO_ROOT / "backend"
STACKS = (
    ODDISH_DIR,
    BACKEND_DIR,
)

SCHEMA_MARKER = "oddish-preview:schema-built-from-base"


def _upgrade_head(project: Path) -> None:
    subprocess.run(
        ["alembic", "upgrade", "head"],
        cwd=project,
        check=True,
    )


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
    for path in (BACKEND_DIR, ODDISH_DIR / "src"):
        path_str = str(path)
        if path_str not in sys.path:
            sys.path.insert(0, path_str)
    importlib.import_module("models")
    from oddish.db.models import Base

    return Base


def _fingerprint_metadata(metadata) -> str:
    parts = [
        f"{table.name}:{','.join(sorted(col.name for col in table.columns))}"
        for table in sorted(metadata.tables.values(), key=lambda t: t.name)
    ]
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:16]


def _model_fingerprint() -> str:
    return _fingerprint_metadata(_load_base().metadata)


async def _assert_model_columns_exist(url: str) -> None:
    metadata = _load_base().metadata
    engine = _engine(url)
    missing = []
    try:
        async with engine.connect() as conn:
            for table in sorted(metadata.tables.values(), key=lambda t: t.name):
                columns = await conn.execute(
                    text(
                        """
                        SELECT column_name
                        FROM information_schema.columns
                        WHERE table_schema = 'public' AND table_name = :table_name
                        """
                    ),
                    {"table_name": table.name},
                )
                actual = {row[0] for row in columns}
                if not actual:
                    missing.append(table.name)
                    continue
                for column in table.columns:
                    if column.name not in actual:
                        missing.append(f"{table.name}.{column.name}")
    finally:
        await engine.dispose()
    if missing:
        preview = ", ".join(missing[:20])
        extra = "" if len(missing) <= 20 else f", and {len(missing) - 20} more"
        raise RuntimeError(f"preview schema missing model columns: {preview}{extra}")


def _migration_fingerprint() -> str:
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
            marker = await conn.scalar(
                text("SELECT obj_description('public'::regnamespace, 'pg_namespace')")
            )
    finally:
        await engine.dispose()
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
        print(
            "bootstrap_preview_db: untrusted schema; rebuilding by running migrations",
            file=sys.stderr,
        )
        asyncio.run(_reset_schema(url))
        for project in STACKS:
            _upgrade_head(project)
        asyncio.run(_assert_model_columns_exist(url))
        asyncio.run(_mark_trusted(url))
        rebuilt = True

    sentinel = os.environ.get("SCHEMA_REBUILT_FILE")
    if rebuilt and sentinel:
        Path(sentinel).write_text("1")


if __name__ == "__main__":
    main()
