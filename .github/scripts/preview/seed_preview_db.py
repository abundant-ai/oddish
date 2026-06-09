"""Run the preview seed against the branch DB (ODDISH_DATABASE_URL).

Invoked from prepare_preview_database.sh under `uv run` in the backend
env. The seed engine lives in backend/, so we add it to sys.path here
(the script's own dir is on sys.path, not the cwd).
"""
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "backend"))

from sqlalchemy import pool
from sqlalchemy.ext.asyncio import create_async_engine

from preview_seed import seed


async def _main() -> None:
    url = os.environ["ODDISH_DATABASE_URL"]
    # The branch URL is the Supabase pooler (transaction-mode pgbouncer /
    # Supavisor), which can't reuse asyncpg's cached named prepared
    # statements -- disable the cache and use NullPool, mirroring
    # oddish.db.connection._base_connect_args.
    engine = create_async_engine(
        url,
        connect_args={"statement_cache_size": 0},
        poolclass=pool.NullPool,
    )
    try:
        await seed(engine)
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(_main())
