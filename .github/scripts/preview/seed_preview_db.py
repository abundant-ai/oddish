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

from sqlalchemy.ext.asyncio import create_async_engine

from preview_seed import seed


async def _main() -> None:
    url = os.environ["ODDISH_DATABASE_URL"]
    engine = create_async_engine(url)
    try:
        await seed(engine)
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(_main())
