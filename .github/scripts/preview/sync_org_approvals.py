"""Final preview DB step: synchronize approval decisions without reseeding."""

import asyncio
import os
from pathlib import Path
import sys
import time

from sqlalchemy import pool
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "backend"))
from preview_org_approvals import read_approval_decisions, sync_approval_decisions


async def main() -> None:
    source_url = make_url(os.environ["PREVIEW_SAMPLE_SOURCE_DB_URL"])
    target_url = make_url(os.environ["ODDISH_DATABASE_URL"])
    prod_ref = os.environ.get("SUPABASE_PROJECT_REF")
    if source_url._replace(password=None) == target_url._replace(password=None) or (
        prod_ref and prod_ref in str(target_url)
    ):
        raise RuntimeError("Approval source and preview database must differ")
    engines = [
        create_async_engine(
            url,
            poolclass=pool.NullPool,
            connect_args={
                "statement_cache_size": 0,
                "timeout": 10,
            },
        )
        for url in (source_url, target_url)
    ]
    started = time.monotonic()
    try:
        decisions = await read_approval_decisions(engines[0])
        result = await sync_approval_decisions(engines[1], decisions)
    finally:
        for engine in engines:
            await engine.dispose()
    print(
        f"Preview organization approvals: {result}; elapsed={time.monotonic() - started:.2f}s"
    )


if __name__ == "__main__":
    asyncio.run(main())
