"""Bootstrap a fresh Supabase preview branch with oddish's core schema.

`oddish/alembic/versions/baseline_baseline.py` is intentionally a no-op;
the core tables (tasks, trials, experiments, queue_slots, worker_jobs,
task_versions) are created from `Base.metadata` via `init_db()` rather
than via DDL migrations. Production was bootstrapped once long ago, but
every Supabase preview branch starts empty so we have to materialize
the schema here before stamping the alembic chain.

Run with `uv run` from `oddish/` so the venv resolves correctly.
"""

from __future__ import annotations

import asyncio

from oddish.db import init_db


def main() -> int:
    asyncio.run(init_db())
    print("oddish core schema created via Base.metadata.create_all()")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
