"""Seed the preview branch DB (ODDISH_DATABASE_URL) with a prod subset.

Invoked from prepare_preview_database.sh under `uv run` in the backend
env. The seed engine lives in backend/, so we add it to sys.path here
(the script's own dir is on sys.path, not the cwd).

PREVIEW_SAMPLE_SOURCE_DB_URL (the production DB URL) is required in CI:
the sampled subset is the preview's only data, so a sampling failure
fails the job loudly rather than deploying an empty preview. With the
variable unset (local runs) the seed is a no-op beyond legacy cleanup.
"""
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "backend"))

from sqlalchemy import pool
from sqlalchemy.ext.asyncio import create_async_engine

from preview_seed import sample_prod_subset, seed


def _engine(url: str, *, read_only: bool = False):
    # Both URLs are Supabase poolers (transaction-mode Supavisor), which
    # can't reuse asyncpg's cached named prepared statements -- disable the
    # cache and use NullPool, mirroring oddish.db.connection. The sampling
    # source additionally pins every transaction read-only at the server.
    connect_args: dict = {"statement_cache_size": 0}
    if read_only:
        connect_args["server_settings"] = {
            "default_transaction_read_only": "on"
        }
    return create_async_engine(
        url, connect_args=connect_args, poolclass=pool.NullPool
    )


async def _main() -> None:
    branch_url = os.environ["ODDISH_DATABASE_URL"]
    source_url = os.environ.get("PREVIEW_SAMPLE_SOURCE_DB_URL")

    sampled = None
    if source_url:
        if source_url == branch_url:
            raise RuntimeError(
                "PREVIEW_SAMPLE_SOURCE_DB_URL equals the branch "
                "ODDISH_DATABASE_URL; refusing to sample a database from itself."
            )
        sample_key = os.environ.get("PR_NUMBER", "default")
        source = _engine(source_url, read_only=True)
        try:
            sampled = await sample_prod_subset(source, sample_key=sample_key)
        finally:
            await source.dispose()
        rows = sum(len(v) for v in sampled["rows"].values())
        print(f"seed_preview_db: sampled {rows} prod rows", file=sys.stderr)
    else:
        print(
            "seed_preview_db: PREVIEW_SAMPLE_SOURCE_DB_URL not set; "
            "skipping prod sample",
            file=sys.stderr,
        )

    engine = _engine(branch_url)
    try:
        await seed(engine, sampled=sampled)
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(_main())
