"""Shared test fixtures for the backend test suite.

The backend tests touch the local Postgres database via ``oddish.db``.
``.env.local`` is expected to be loaded by the caller (e.g. ``set -a &&
source .env.local && set +a && uv run pytest``); we don't try to load it
inside the test process to keep the contract explicit.
"""

from __future__ import annotations

import pytest


# pytest-asyncio strict mode is fine: tests opt in with @pytest.mark.asyncio.
# We declare the loop scope here so async fixtures share the loop with the
# tests that consume them.
pytest_plugins = ("pytest_asyncio",)


@pytest.fixture(scope="session")
def anyio_backend() -> str:
    return "asyncio"
