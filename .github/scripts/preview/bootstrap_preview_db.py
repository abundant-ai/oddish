"""Apply Alembic to the PR's data-less Supabase preview branch.

The branch starts empty, so ``alembic upgrade head`` builds the full
schema from scratch for both stacks (oddish + backend). NOTE: because
the branch has no data at migration time, destructive/backfill data
migrations are NOT exercised against real rows here -- that coverage is
deliberately traded away (see the schema+seed design spec). Curated
data is loaded afterwards by ``seed_preview_db.py``.
"""

import subprocess
from pathlib import Path

for project in (Path.cwd().parent / "oddish", Path.cwd()):
    subprocess.run(["alembic", "upgrade", "head"], cwd=project, check=True)
