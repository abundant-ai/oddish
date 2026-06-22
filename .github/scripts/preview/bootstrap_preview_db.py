"""Bootstrap the PR's data-less Supabase preview branch: it inherits the parent's
schema but an empty ``alembic_version_*``, so stamp each stack to the parent's
current revision before ``alembic upgrade head`` to apply only this PR's migrations.
"""

import asyncio
import os
import subprocess
import sys
from pathlib import Path

from sqlalchemy import pool, text
from sqlalchemy.ext.asyncio import create_async_engine

STACKS = (
    (Path.cwd().parent / "oddish", "alembic_version_oddish"),
    (Path.cwd(), "alembic_version_backend"),
)


async def _parent_revisions(parent_url: str) -> dict[str, str]:
    """Read each stack's current revision from the parent (production) DB.

    The parent is a Supavisor pooler URL, so mirror seed_preview_db.py: disable
    asyncpg's prepared-statement cache, use NullPool, and pin the transaction
    read-only since we only ever SELECT here. The timeouts bound the read so a
    hung pooler connection can't wedge the job up to GitHub's 6h ceiling.
    """
    engine = create_async_engine(
        parent_url,
        connect_args={
            "statement_cache_size": 0,
            "server_settings": {"default_transaction_read_only": "on"},
            "timeout": 30,
            "command_timeout": 30,
        },
        poolclass=pool.NullPool,
    )
    revisions: dict[str, str] = {}
    try:
        async with engine.connect() as conn:
            for _, table in STACKS:
                if await conn.scalar(text("SELECT to_regclass(:t)"), {"t": table}) is None:
                    continue
                rev = await conn.scalar(text(f"SELECT version_num FROM {table}"))
                if rev:
                    revisions[table] = rev
    finally:
        await engine.dispose()
    return revisions


def _alembic(project: Path, *args: str) -> None:
    subprocess.run(["alembic", *args], cwd=project, check=True)


def main() -> None:
    branch_was_created = os.environ.get("BRANCH_WAS_CREATED") == "true"
    parent_url = os.environ.get("PREVIEW_SAMPLE_SOURCE_DB_URL")

    if branch_was_created and parent_url:
        revisions = asyncio.run(_parent_revisions(parent_url))
        for project, table in STACKS:
            rev = revisions.get(table)
            if rev is None:
                raise SystemExit(
                    f"parent DB exposes no {table} revision to stamp; refusing "
                    "to replay full history against the inherited branch schema"
                )
            try:
                _alembic(project, "stamp", rev)
            except subprocess.CalledProcessError:
                print(
                    f"{table}: parent revision {rev} not in this branch's history; "
                    "stamping branch head instead (branch behind or diverged)",
                    file=sys.stderr,
                )
                _alembic(project, "stamp", "head")

    for project, _ in STACKS:
        _alembic(project, "upgrade", "head")


if __name__ == "__main__":
    main()
