from __future__ import annotations

import asyncio
from typing import Iterable

from api.services.cc_chat.daytona_client import CreatedSandbox, DaytonaClient


async def upload_files(
    client: DaytonaClient,
    sandbox: CreatedSandbox,
    *,
    files: Iterable[tuple[str, bytes]],
    workspace_root: str,
    concurrency: int = 8,
) -> None:
    """Upload (rel_path, bytes) tuples to <workspace_root>/<rel_path> in parallel."""
    sem = asyncio.Semaphore(concurrency)
    files = list(files)

    async def upload_one(rel: str, content: bytes) -> None:
        dest = f"{workspace_root.rstrip('/')}/{rel.lstrip('/')}"
        async with sem:
            await client.upload_file(sandbox, dest_path=dest, content=content)

    if not files:
        return
    await asyncio.gather(*(upload_one(rel, c) for rel, c in files))
