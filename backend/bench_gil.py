"""Local, single-process GIL/contention test for the slow experiment read.

Why this exists: prod is never idle, so a "solo" request there is never truly
alone. Run in ONE process (one GIL), nothing else touching it, so "solo" is
provably solo and `gather(K)` reveals whether the work serializes (CPU-bound)
or overlaps (I/O-bound).

Read-only: only issues SELECTs via list_tasks_core. No writes, no deploy.

Run (from backend/, with the prod *read* DB URL set):
    $env:ODDISH_DATABASE_URL = "postgresql+asyncpg://USER:PASS@HOST:PORT/postgres"
    uv run python bench_gil.py
"""

import asyncio
import time

from oddish.db import get_session
from oddish.core.endpoints import list_tasks_core

EXPERIMENT_ID = "61a0cf64"  # the heavy ~841-task experiment


async def one_call(label: str) -> float:
    t0 = time.perf_counter()
    async with get_session() as session:
        rows = await list_tasks_core(
            session,
            experiment_id=EXPERIMENT_ID,
            compact_tasks=True,
            include_trials=False,
            limit=2000,
            offset=0,
        )
    dt = time.perf_counter() - t0
    print(f"  [{label:>9}] {dt:6.2f}s   ({len(rows)} tasks)")
    return dt


async def main() -> None:
    print("SOLO (5 sequential -- provably isolated, nothing else hits this process):")
    for i in range(5):
        await one_call(f"solo {i + 1}")

    for k in (2, 4, 8):
        print(f"\nCONCURRENT k={k} (one process / one GIL, fired together):")
        t0 = time.perf_counter()
        times = await asyncio.gather(*(one_call(f"k{k}#{j + 1}") for j in range(k)))
        wall = time.perf_counter() - t0
        avg = sum(times) / len(times)
        print(f"  -> per-call avg={avg:.2f}s   wall={wall:.2f}s")

    print(
        "\nRead it: if k=8 per-call ~= 8x solo (wall ~= 8x), the work is CPU-bound "
        "and serializes on the GIL (theory TRUE). If k=8 per-call ~= solo "
        "(wall ~= solo), it overlaps -> I/O-bound (theory FALSE)."
    )


if __name__ == "__main__":
    asyncio.run(main())
