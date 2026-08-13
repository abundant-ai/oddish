"""Public (share-token) analysis reads and queued capability generation."""

from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from api.app import create_app
from api.services.agent_capabilities import StoredComparison
from api.services.summarize_trajectory import SCHEMA_VERSION

TOKEN = "share-tok"
SUMMARY_URL = f"/public/experiments/{TOKEN}/trials/t-1/trajectory/summary"
COMPARISON_URL = f"/public/experiments/{TOKEN}/tasks/task-1/agent-capabilities"


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
    """An old schema is not rendered when no trajectory is eligible."""
    trial = SimpleNamespace(
        id="t-1",
        task_version_id="tv-1",
        trajectory_summary={"schema_version": "1", "summary": "old"},
    )
    with patched_session, patch(
        "api.routers.public_analysis.get_public_trial_for_experiment",
        new=AsyncMock(return_value=trial),
    ), patch(
        "api.services.summarize_trajectory._load_fresh_summary_block",
        new=AsyncMock(return_value=None),
    ), patch(
        "api.services.agent_capabilities.analysis_is_eligible",
        new=AsyncMock(return_value=False),
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


def test_summary_miss_enqueues_capability_analysis(client, fake_session, patched_session):
    trial = SimpleNamespace(
        id="t-1",
        task_id="task-1",
        task_version_id="tv-1",
        trajectory_summary=None,
    )
    task = SimpleNamespace(id="task-1", name="task-one", org_id="org-1")
    task_result = MagicMock()
    task_result.scalar_one.return_value = task
    fake_session.execute.side_effect = [task_result, MagicMock()]
    enqueue = AsyncMock()
    with patched_session, patch(
        "api.routers.public_analysis.get_public_trial_for_experiment",
        new=AsyncMock(return_value=trial),
    ), patch(
        "api.services.summarize_trajectory.load_stored_summary",
        new=AsyncMock(return_value=None),
    ), patch(
        "api.services.agent_capabilities.analysis_is_eligible",
        new=AsyncMock(return_value=True),
    ), patch("api.services.agent_capabilities.enqueue_analysis", new=enqueue):
        resp = client.get(SUMMARY_URL)
    assert resp.status_code == 202
    assert resp.json() == {"status": "queued", "task_version_id": "tv-1"}
    enqueue.assert_awaited_once_with(
        fake_session,
        task_id="task-1",
        task_version_id="tv-1",
        task_name="task-one",
        org_id="org-1",
        triggered_by_user_id=None,
    )


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


@pytest.fixture
def version_in_experiment():
    """Default the membership check to true; the scoping tests override it."""
    return patch(
        "api.routers.public_analysis._version_is_in_experiment",
        new=AsyncMock(return_value=True),
    )


def _public_task(current_version_id: str | None = "tv-current"):
    task = SimpleNamespace(
        id="task-1",
        name="task-one",
        org_id="org-1",
        current_version_id=current_version_id,
    )
    return AsyncMock(return_value=(SimpleNamespace(id="exp-1"), task, set()))


def _stored(output: dict, *, stale: bool = False):
    return AsyncMock(return_value=StoredComparison(output, stale=stale))


def test_comparison_returns_stored_block_and_stamps_version(
    client, patched_session, no_display_names, version_in_experiment
):
    stored = {"schema_version": 3, "categories": [], "cohort_success": []}
    with patched_session, no_display_names, version_in_experiment, patch(
        "api.routers.public_analysis.get_public_task_for_experiment",
        new=_public_task(),
    ), patch(
        "api.services.agent_capabilities.load_stored_analysis", new=_stored(stored)
    ):
        resp = client.get(COMPARISON_URL)
    assert resp.status_code == 200
    # The version the comparison covers is stamped at serve time, matching the
    # authenticated route: the UI addresses a version by id, this route by number.
    assert resp.json() == {
        **stored,
        "task_version_id": "tv-current",
        "stale": False,
        "regenerating": False,
    }


def test_comparison_resolves_the_requested_version_number(
    client, fake_session, patched_session, no_display_names, version_in_experiment
):
    fake_session.execute.return_value = MagicMock(
        scalar_one_or_none=MagicMock(return_value="tv-2")
    )
    load = _stored({"categories": []})
    with patched_session, no_display_names, version_in_experiment, patch(
        "api.routers.public_analysis.get_public_task_for_experiment",
        new=_public_task(),
    ), patch("api.services.agent_capabilities.load_stored_analysis", new=load):
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
    ), patch("api.services.agent_capabilities.load_stored_analysis", new=load):
        resp = client.get(f"{COMPARISON_URL}?version=99")
    assert resp.status_code == 404
    load.assert_not_awaited()


def test_comparison_miss_enqueues_and_returns_202(
    client, patched_session, version_in_experiment
):
    enqueue = AsyncMock()
    with patched_session, version_in_experiment, patch(
        "api.routers.public_analysis.get_public_task_for_experiment",
        new=_public_task(),
    ), patch(
        "api.services.agent_capabilities.load_stored_analysis",
        new=AsyncMock(return_value=None),
    ), patch(
        "api.services.agent_capabilities.analysis_is_eligible",
        new=AsyncMock(return_value=True),
    ), patch(
        "api.services.agent_capabilities.enqueue_analysis", new=enqueue
    ):
        resp = client.get(COMPARISON_URL)
    assert resp.status_code == 202
    assert resp.json() == {"status": "queued", "task_version_id": "tv-current"}
    enqueue.assert_awaited_once()


def test_comparison_404_when_token_does_not_expose_the_task(client, patched_session):
    load = AsyncMock()
    with patched_session, patch(
        "api.routers.public_analysis.get_public_task_for_experiment",
        new=AsyncMock(return_value=None),
    ), patch("api.services.agent_capabilities.load_stored_analysis", new=load):
        resp = client.get(COMPARISON_URL)
    assert resp.status_code == 404
    load.assert_not_awaited()


def test_comparison_masks_model_ids_with_operator_aliases(
    client, patched_session, version_in_experiment
):
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
    with patched_session, version_in_experiment, patch(
        "api.routers.public_analysis.load_model_display_names",
        new=AsyncMock(return_value=aliases),
    ), patch(
        "api.routers.public_analysis.get_public_task_for_experiment",
        new=_public_task(),
    ), patch(
        "api.services.agent_capabilities.load_stored_analysis", new=_stored(stored)
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


def test_comparison_masks_the_SHORT_names_the_block_actually_stores(
    client, patched_session, version_in_experiment
):
    """The production shape, which the full-id test above does not cover.

    `models[].model` and `trial_models` are written through `short_model_name`,
    so the block holds `claude-opus-4-8` while the alias table keys on
    `global.anthropic.claude-opus-4-8`. Masking only the full id matches nothing
    and publishes the real names under a page that hides them in the grid.
    """
    stored = {
        "categories": [],
        "models": {
            "successful": [{"model": "claude-opus-4-8", "trials": 4}],
            "failing": [{"model": "gemini-3.1-pro-preview", "trials": 3}],
        },
        "trial_models": {"t-1": "claude-opus-4-8", "t-2": "gemini-3.1-pro-preview"},
    }
    aliases = {
        "global.anthropic.claude-opus-4-8": "Model A",
        "google/gemini-3.1-pro-preview": "Model B",
    }
    with patched_session, version_in_experiment, patch(
        "api.routers.public_analysis.load_model_display_names",
        new=AsyncMock(return_value=aliases),
    ), patch(
        "api.routers.public_analysis.get_public_task_for_experiment",
        new=_public_task(),
    ), patch(
        "api.services.agent_capabilities.load_stored_analysis", new=_stored(stored)
    ):
        resp = client.get(COMPARISON_URL)
    body = resp.json()
    assert body["models"]["successful"] == [{"model": "Model A", "trials": 4}]
    assert body["models"]["failing"] == [{"model": "Model B", "trials": 3}]
    assert body["trial_models"] == {"t-1": "Model A", "t-2": "Model B"}


def test_comparison_leaves_a_colliding_short_name_unmasked(
    client, patched_session, version_in_experiment
):
    """Two regions reduce to one short name. Guessing between disagreeing
    aliases would misattribute the behaviour the analysis describes."""
    stored = {"categories": [], "trial_models": {"t-1": "claude-opus-4-8"}}
    aliases = {
        "global.anthropic.claude-opus-4-8": "Model A",
        "us.anthropic.claude-opus-4-8": "Model Z",
    }
    with patched_session, version_in_experiment, patch(
        "api.routers.public_analysis.load_model_display_names",
        new=AsyncMock(return_value=aliases),
    ), patch(
        "api.routers.public_analysis.get_public_task_for_experiment",
        new=_public_task(),
    ), patch(
        "api.services.agent_capabilities.load_stored_analysis", new=_stored(stored)
    ):
        resp = client.get(COMPARISON_URL)
    assert resp.json()["trial_models"] == {"t-1": "claude-opus-4-8"}


def test_comparison_asks_for_the_drift_tolerant_read(
    client, patched_session, no_display_names, version_in_experiment
):
    """A share has nothing to fall back on, so a comparison one trial out of
    date beats an empty pane. The authenticated route must NOT opt in -- it
    regenerates on the miss instead."""
    load = _stored({"categories": []})
    with patched_session, no_display_names, version_in_experiment, patch(
        "api.routers.public_analysis.get_public_task_for_experiment",
        new=_public_task(),
    ), patch("api.services.agent_capabilities.load_stored_analysis", new=load):
        client.get(COMPARISON_URL)
    assert load.await_args.kwargs["allow_cohort_drift"] is True


def test_a_stale_comparison_is_served_and_asks_for_a_rebuild(
    client, patched_session, no_display_names, version_in_experiment
):
    stored = {"categories": [{"category": "planning"}]}
    enqueue = AsyncMock()
    with patched_session, no_display_names, version_in_experiment, patch(
        "api.routers.public_analysis.get_public_task_for_experiment",
        new=_public_task(),
    ), patch(
        "api.services.agent_capabilities.load_stored_analysis",
        new=_stored(stored, stale=True),
    ), patch("api.services.agent_capabilities.enqueue_analysis", new=enqueue):
        resp = client.get(COMPARISON_URL)
    assert resp.status_code == 200
    body = resp.json()
    assert body["categories"] == stored["categories"]
    assert body["stale"] is True
    assert body["regenerating"] is True
    enqueue.assert_awaited_once()
    assert enqueue.await_args.kwargs["task_version_id"] == "tv-current"


def test_a_fresh_comparison_asks_for_no_rebuild(
    client, patched_session, no_display_names, version_in_experiment
):
    enqueue = AsyncMock()
    with patched_session, no_display_names, version_in_experiment, patch(
        "api.routers.public_analysis.get_public_task_for_experiment",
        new=_public_task(),
    ), patch(
        "api.services.agent_capabilities.load_stored_analysis",
        new=_stored({"categories": []}),
    ), patch("api.services.agent_capabilities.enqueue_analysis", new=enqueue):
        resp = client.get(COMPARISON_URL)
    assert resp.json()["regenerating"] is False
    enqueue.assert_not_awaited()


def test_comparison_404s_for_a_version_outside_the_shared_experiment(
    client, fake_session, patched_session
):
    """The token publishes an experiment, not the task's whole history. A
    version this experiment never ran carries trial ids, models and trajectory
    quotes the share was never meant to expose."""
    fake_session.execute.return_value = MagicMock(
        scalar_one_or_none=MagicMock(return_value="tv-9")
    )
    load = AsyncMock()
    with patched_session, patch(
        "api.routers.public_analysis.get_public_task_for_experiment",
        new=_public_task(),
    ), patch(
        "api.routers.public_analysis._version_is_in_experiment",
        new=AsyncMock(return_value=False),
    ), patch("api.services.agent_capabilities.load_stored_analysis", new=load):
        resp = client.get(f"{COMPARISON_URL}?version=9")
    assert resp.status_code == 404
    load.assert_not_awaited()
