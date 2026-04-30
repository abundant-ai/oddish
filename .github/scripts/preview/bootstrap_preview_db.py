"""Apply Alembic to the PR's Supabase preview branch.

With Supabase data-branching enabled, the preview branch is a clone of
prod: ``alembic_version`` already points at prod's head, every table
already has prod-shaped rows. ``alembic upgrade head`` then only
applies the revisions added on this PR — and those revisions run
against real data, so a destructive migration fails *here* instead of
at the prod cutover. That's the entire point of this step.
"""

import subprocess
from pathlib import Path

for project in (Path.cwd().parent / "oddish", Path.cwd()):
    subprocess.run(["alembic", "upgrade", "head"], cwd=project, check=True)
