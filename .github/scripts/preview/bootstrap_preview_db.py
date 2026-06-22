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


_REVISION_NOT_FOUND = "Can't locate revision"


def _stamp_to_parent(project: Path, table: str, rev: str) -> None:
    proc = subprocess.run(
        ["alembic", "stamp", rev], cwd=project, text=True, capture_output=True
    )
    sys.stdout.write(proc.stdout)
    sys.stderr.write(proc.stderr)
    if proc.returncode == 0:
        return
    if _REVISION_NOT_FOUND not in (proc.stdout + proc.stderr):
        raise subprocess.CalledProcessError(
            proc.returncode, proc.args, proc.stdout, proc.stderr
        )
    raise SystemExit(
        f"{table}: parent revision {rev} is not in this branch's Alembic "
        "history, so this branch is behind or diverged from the schema "
        "source. Stamping head here would assert a schema state nothing "
        "verified and silently corrupt the branch. Fix: merge `main` into "
        "this branch (or otherwise reconcile migrations) so its history "
        f"contains {rev}, then recreate the preview branch."
    )


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
            _stamp_to_parent(project, table, rev)

    for project, _ in STACKS:
        _alembic(project, "upgrade", "head")


if __name__ == "__main__":
    main()
