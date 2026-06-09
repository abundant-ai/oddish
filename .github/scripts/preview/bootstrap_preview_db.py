"""Apply Alembic to the PR's data-less Supabase preview branch.

A data-less branch still inherits the parent project's *schema* (only the
data is omitted), so ``alembic upgrade head`` applies just the revisions
this PR adds on top for both stacks (oddish + backend) -- it is not a
from-scratch build. NOTE: because the branch carries no data, destructive
/backfill data migrations are not exercised against real rows here; that
coverage is deliberately traded away. Curated data is loaded afterwards by
``seed_preview_db.py``.
"""

import subprocess
from pathlib import Path

for project in (Path.cwd().parent / "oddish", Path.cwd()):
    subprocess.run(["alembic", "upgrade", "head"], cwd=project, check=True)
