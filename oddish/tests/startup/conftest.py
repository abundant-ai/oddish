"""Startup checks exercise only fresh CLI processes, with no database setup."""

import pytest


@pytest.fixture(autouse=True)
def _recycle_db_engine():
    yield


@pytest.fixture(autouse=True)
def _fresh_cost_exclusions():
    yield
