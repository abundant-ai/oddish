"""Public (share-token) analysis reads: trajectory summary and cohort comparison.

The load-bearing property here is what these routes *cannot* do. Their
authenticated counterparts generate on a cache miss -- a Claude call per
summary, a claude-code run per comparison -- and no unauthenticated caller may
reach that. Every miss test therefore also asserts the generator was never
called, not merely that the status was 404.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from api.app import create_app
from api.services.summarize_trajectory import SCHEMA_VERSION

TOKEN = "share-tok"
SUMMARY_URL = f"/public/experiments/{TOKEN}/trials/t-1/trajectory/summary"
COMPARISON_URL = f"/public/experiments/{TOKEN}/tasks/task-1/cohort-comparison"


@pytest.fixture
def client():
    return TestClient(create_app())


@pytest.fixture
def fake_session():
    session = MagicMock()
    session.execute = AsyncMock()
    return session


@pytest.fixture
def patched_session(fake_session):
    @asynccontextmanager
    async def _fake_get_session():
        yield fake_session

    return patch("api.routers.public_analysis.get_session", new=_fake_get_session)


@pytest.fixture
def no_display_names():
    return patch(
        "api.routers.public_analysis.load_model_display_names",
        new=AsyncMock(return_value={}),
    )


# --------------------------------------------------------------------------
# Trajectory summary
# --------------------------------------------------------------------------


def test_summary_returns_stored_block(client, patched_session):
    summary = {"schema_version": "7", "summary": "ok", "highlights": []}
    with patched_session, patch(
        "api.routers.public_analysis.get_public_trial_for_experiment",
        new=AsyncMock(return_value=SimpleNamespace(id="t-1", trajectory_summary=None)),
    ), patch(
        "api.services.summarize_trajectory.load_stored_summary",
        new=AsyncMock(return_value=summary),
    ):
        resp = client.get(SUMMARY_URL)
    assert resp.status_code == 200
    assert resp.json() == summary


def test_summary_falls_back_to_the_trial_mirror(client, patched_session):
    """`preview_seed` copies trials but not `analyzer_blocks`, so a block-only
    read is empty on every preview deploy while the summary sits on the trial
    row. The authenticated route hides that by generating; this one cannot."""
    mirror = {"schema_version": SCHEMA_VERSION, "summary": "from the mirror"}
    trial = SimpleNamespace(id="t-1", trajectory_summary=mirror)
    with patched_session, patch(
        "api.routers.public_analysis.get_public_trial_for_experiment",
        new=AsyncMock(return_value=trial),
    ), patch(
        "api.services.summarize_trajectory._load_fresh_summary_block",
        new=AsyncMock(return_value=None),
    ):
        resp = client.get(SUMMARY_URL)
    assert resp.status_code == 200
    assert resp.json() == mirror


def test_summary_ignores_a_stale_mirror(client, patched_session):
    """The mirror is held to the same freshness bar as the block: an old
    schema would render the wrong vocabulary rather than nothing."""
    trial = SimpleNamespace(
        id="t-1", trajectory_summary={"schema_version": "1", "summary": "old"}
    )
    with patched_session, patch(
        "api.routers.public_analysis.get_public_trial_for_experiment",
        new=AsyncMock(return_value=trial),
    ), patch(
        "api.services.summarize_trajectory._load_fresh_summary_block",
        new=AsyncMock(return_value=None),
    ):
        resp = client.get(SUMMARY_URL)
    assert resp.status_code == 404


def test_summary_prefers_the_block_over_the_mirror(client, patched_session):
    trial = SimpleNamespace(
        id="t-1", trajectory_summary={"schema_version": SCHEMA_VERSION, "s": "mirror"}
    )
    block = {"schema_version": SCHEMA_VERSION, "s": "block"}
    with patched_session, patch(
        "api.routers.public_analysis.get_public_trial_for_experiment",
        new=AsyncMock(return_value=trial),
    ), patch(
        "api.services.summarize_trajectory._load_fresh_summary_block",
        new=AsyncMock(return_value=block),
    ):
        resp = client.get(SUMMARY_URL)
    assert resp.json() == block


def test_summary_miss_is_404_and_never_generates(client, patched_session):
    generate = AsyncMock()
    with patched_session, patch(
        "api.routers.public_analysis.get_public_trial_for_experiment",
        new=AsyncMock(return_value=SimpleNamespace(id="t-1", trajectory_summary=None)),
    ), patch(
        "api.services.summarize_trajectory.load_stored_summary",
        new=AsyncMock(return_value=None),
    ), patch(
        "api.services.summarize_trajectory.get_or_generate_summary", new=generate
    ):
        resp = client.get(SUMMARY_URL)
    assert resp.status_code == 404
    generate.assert_not_awaited()


def test_summary_404_when_token_does_not_expose_the_trial(client, patched_session):
    """An unshared trial must not be readable, and must not be looked up."""
    load = AsyncMock()
    with patched_session, patch(
        "api.routers.public_analysis.get_public_trial_for_experiment",
        new=AsyncMock(return_value=None),
    ), patch("api.services.summarize_trajectory.load_stored_summary", new=load):
        resp = client.get(SUMMARY_URL)
    assert resp.status_code == 404
    load.assert_not_awaited()


# --------------------------------------------------------------------------
# Cohort comparison
# --------------------------------------------------------------------------


def _public_task(current_version_id: str | None = "tv-current"):
    task = SimpleNamespace(
        id="task-1", name="task-one", current_version_id=current_version_id
    )
    return AsyncMock(return_value=(SimpleNamespace(id="exp-1"), task, set()))


def test_comparison_returns_stored_block_and_stamps_version(
    client, patched_session, no_display_names
):
    stored = {"schema_version": 3, "categories": [], "cohort_success": []}
    with patched_session, no_display_names, patch(
        "api.routers.public_analysis.get_public_task_for_experiment",
        new=_public_task(),
    ), patch(
        "api.services.cohort_comparison.load_stored_comparison",
        new=AsyncMock(return_value=stored),
    ):
        resp = client.get(COMPARISON_URL)
    assert resp.status_code == 200
    # The version the comparison covers is stamped at serve time, matching the
    # authenticated route: the UI addresses a version by id, this route by number.
    assert resp.json() == {**stored, "task_version_id": "tv-current"}


def test_comparison_resolves_the_requested_version_number(
    client, fake_session, patched_session, no_display_names
):
    fake_session.execute.return_value = MagicMock(
        scalar_one_or_none=MagicMock(return_value="tv-2")
    )
    load = AsyncMock(return_value={"categories": []})
    with patched_session, no_display_names, patch(
        "api.routers.public_analysis.get_public_task_for_experiment",
        new=_public_task(),
    ), patch("api.services.cohort_comparison.load_stored_comparison", new=load):
        resp = client.get(f"{COMPARISON_URL}?version=2")
    assert resp.status_code == 200
    assert resp.json()["task_version_id"] == "tv-2"
    # The pinned version, not the task's current one — a share page shows the
    # version its trials ran on.
    assert load.await_args.args[1] == "tv-2"


def test_comparison_404_for_unknown_version_number(
    client, fake_session, patched_session
):
    fake_session.execute.return_value = MagicMock(
        scalar_one_or_none=MagicMock(return_value=None)
    )
    load = AsyncMock()
    with patched_session, patch(
        "api.routers.public_analysis.get_public_task_for_experiment",
        new=_public_task(),
    ), patch("api.services.cohort_comparison.load_stored_comparison", new=load):
        resp = client.get(f"{COMPARISON_URL}?version=99")
    assert resp.status_code == 404
    load.assert_not_awaited()


def test_comparison_miss_is_404_and_never_generates(client, patched_session):
    generate = AsyncMock()
    with patched_session, patch(
        "api.routers.public_analysis.get_public_task_for_experiment",
        new=_public_task(),
    ), patch(
        "api.services.cohort_comparison.load_stored_comparison",
        new=AsyncMock(return_value=None),
    ), patch(
        "api.services.cohort_comparison.get_or_generate_comparison", new=generate
    ):
        resp = client.get(COMPARISON_URL)
    assert resp.status_code == 404
    generate.assert_not_awaited()


def test_comparison_404_when_token_does_not_expose_the_task(client, patched_session):
    load = AsyncMock()
    with patched_session, patch(
        "api.routers.public_analysis.get_public_task_for_experiment",
        new=AsyncMock(return_value=None),
    ), patch("api.services.cohort_comparison.load_stored_comparison", new=load):
        resp = client.get(COMPARISON_URL)
    assert resp.status_code == 404
    load.assert_not_awaited()


def test_comparison_masks_model_ids_with_operator_aliases(client, patched_session):
    """The share view hides real model ids in the trial grid (#1097). The
    comparison names models too, and must be masked by the same table or the
    page hides the id above and prints it below."""
    stored = {
        "categories": [],
        "models": {
            "successful": [{"model": "anthropic/claude-opus-4-8", "trials": 4}],
            "failing": [{"model": "openai/gpt-5.4", "trials": 3}],
        },
        "trial_models": {"t-1": "anthropic/claude-opus-4-8", "t-2": "openai/gpt-5.4"},
    }
    aliases = {"anthropic/claude-opus-4-8": "Model A", "openai/gpt-5.4": "Model B"}
    with patched_session, patch(
        "api.routers.public_analysis.load_model_display_names",
        new=AsyncMock(return_value=aliases),
    ), patch(
        "api.routers.public_analysis.get_public_task_for_experiment",
        new=_public_task(),
    ), patch(
        "api.services.cohort_comparison.load_stored_comparison",
        new=AsyncMock(return_value=stored),
    ):
        resp = client.get(COMPARISON_URL)
    assert resp.status_code == 200
    body = resp.json()
    assert body["models"]["successful"] == [{"model": "Model A", "trials": 4}]
    assert body["models"]["failing"] == [{"model": "Model B", "trials": 3}]
    assert body["trial_models"] == {"t-1": "Model A", "t-2": "Model B"}
    # The stored block is an ORM row's `output` on a live session — masking
    # must not write through to it.
    assert stored["trial_models"]["t-1"] == "anthropic/claude-opus-4-8"
