"""Materialize oddish core tables on a fresh Supabase preview branch."""

import asyncio

from oddish.db import init_db

asyncio.run(init_db())
