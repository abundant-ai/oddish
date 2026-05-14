"""Apply Alembic to the PR's Supabase preview branch.

With Supabase data-branching enabled, the preview branch is a clone of
prod: ``alembic_version`` already points at prod's head, every table
already has prod-shaped rows. ``alembic upgrade head`` then only
applies the revisions added on this PR — and those revisions run
against real data, so a destructive migration fails *here* instead of
at the prod cutover. That's the entire point of this step.

Supabase's Supavisor pooler trips a per-project circuit breaker when
it sees too many recent auth errors (from concurrent PRs, cancelled
in-flight workflows, or earlier failed runs on this same branch).
asyncpg surfaces that as ``InternalServerError: Circuit breaker open``
even when our credentials are perfectly valid. Retry alembic a few
times with exponential backoff so a temporarily-open breaker doesn't
fail the deploy.
"""

import subprocess
import sys
import time
from pathlib import Path

MAX_ATTEMPTS = 5


def alembic_upgrade(project: Path) -> None:
    for attempt in range(1, MAX_ATTEMPTS + 1):
        result = subprocess.run(["alembic", "upgrade", "head"], cwd=project)
        if result.returncode == 0:
            return
        if attempt == MAX_ATTEMPTS:
            raise SystemExit(
                f"alembic upgrade head failed in {project} after "
                f"{MAX_ATTEMPTS} attempts (exit {result.returncode})"
            )
        delay = 2 ** attempt
        print(
            f"[bootstrap] alembic exited {result.returncode}; "
            f"retrying in {delay}s (attempt {attempt}/{MAX_ATTEMPTS})",
            file=sys.stderr,
            flush=True,
        )
        time.sleep(delay)


for project in (Path.cwd().parent / "oddish", Path.cwd()):
    alembic_upgrade(project)
