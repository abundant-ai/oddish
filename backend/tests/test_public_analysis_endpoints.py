"""Public (share-token) trajectory-summary reads.

Summaries are written by the task's QA trial import onto
``trials.trajectory_summary``; the public route is a plain column read with
no on-demand generation, matching the authenticated route's contract.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from api.app import create_app

TOKEN = "share-tok"
SUMMARY_URL = f"/public/experiments/{TOKEN}/trials/t-1/trajectory/summary"


@pytest.fixture
def client():
    return TestClient(create_app())


@pytest.fixture
def patched_session():
    session = MagicMock()
    session.execute = AsyncMock()

    @asynccontextmanager
    async def _fake_get_session():
        yield session

    return patch("api.routers.public_analysis.get_session", new=_fake_get_session)


def _patched_trial(trial):
    return patch(
        "api.routers.public_analysis.get_public_trial_for_experiment",
        new=AsyncMock(return_value=trial),
    )


def test_summary_returns_the_stored_column(client, patched_session):
    summary = {"schema_version": "6", "summary": "ok", "components": []}
    trial = SimpleNamespace(id="t-1", trajectory_summary=summary)
    with patched_session, _patched_trial(trial):
        resp = client.get(SUMMARY_URL)
    assert resp.status_code == 200
    assert resp.json() == summary


def test_summary_miss_is_a_404_not_a_generation(client, patched_session):
    """No enqueue, no 202: a trial the QA import has not graded yet simply
    has no summary."""
    trial = SimpleNamespace(id="t-1", trajectory_summary=None)
    with patched_session, _patched_trial(trial):
        resp = client.get(SUMMARY_URL)
    assert resp.status_code == 404


def test_unknown_trial_is_a_404(client, patched_session):
    with patched_session, _patched_trial(None):
        resp = client.get(SUMMARY_URL)
    assert resp.status_code == 404
