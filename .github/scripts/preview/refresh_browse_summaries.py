"""Recalculate preview counters from sampled trials after all seed/migration work."""

import asyncio
import sys

from bootstrap_preview_db import (
    BACKEND_DIR,
    _assert_preview_branch,
    _branch_db_url,
    _engine,
)

sys.path.insert(0, str(BACKEND_DIR))

from preview_seed import refresh_browse_summaries


async def main() -> None:
    url = _branch_db_url()
    _assert_preview_branch(url)
    engine = _engine(url)
    try:
        await refresh_browse_summaries(engine)
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
