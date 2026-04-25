"""Materialize schema + stamp alembic chains on a fresh preview branch."""

import asyncio
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, os.getcwd())  # backend/ — find flat-layout `models`

import models  # noqa: E402, F401  # registers cloud-auth tables on Base
from oddish.db import init_db  # noqa: E402

asyncio.run(init_db())

for project in (Path.cwd().parent / "oddish", Path.cwd()):
    subprocess.run(["alembic", "stamp", "head"], cwd=project, check=True)
