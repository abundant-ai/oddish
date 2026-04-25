"""Materialize backend cloud-auth tables on a fresh Supabase preview branch."""

import asyncio
import os
import sys

# Flat-layout `models.py` lives in the cwd (backend/), not on sys.path
# when invoked via an absolute script path.
sys.path.insert(0, os.getcwd())

import models  # noqa: E402, F401  # registers cloud-auth tables on Base.metadata
from oddish.db import init_db  # noqa: E402

asyncio.run(init_db())
