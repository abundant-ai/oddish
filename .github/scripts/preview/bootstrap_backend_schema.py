"""Bootstrap the cloud-auth tables on a fresh Supabase preview branch.

The backend alembic chain assumes an evolving oddish baseline that no
longer exists on a freshly-created branch (e.g. `e2f3a4b5c6d7` backfills
`experiments.org_id` from `tasks.experiment_id`, a column oddish later
restructured into a `task_experiments` M2M table). Materialize the
cloud-auth tables (organizations, users, api_keys) via
`Base.metadata.create_all` and then `alembic stamp head` separately
rather than running the chain top-to-bottom.

Importing `models` (flat layout under `backend/`) registers the
cloud-auth tables on the same `Base` that `oddish.db.models` defines, so
`init_db()` covers both. The oddish core tables already exist from the
previous step, so the IF NOT EXISTS path inside `create_all` is a no-op
for them.

Run with `uv run` from `backend/` so the venv resolves correctly.
"""

from __future__ import annotations

import asyncio
import os
import sys

# `uv run python <absolute-path>` puts the *script's* directory on
# sys.path, not the cwd, so the flat-layout `models.py` in backend/
# becomes unimportable. Re-add the working directory.
sys.path.insert(0, os.getcwd())

import models  # noqa: E402, F401  # register cloud-auth tables on Base.metadata
from oddish.db import init_db  # noqa: E402


def main() -> int:
    asyncio.run(init_db())
    print("backend cloud-auth schema materialized via Base.metadata.create_all()")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
