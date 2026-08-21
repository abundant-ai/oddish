"""Per-experiment operator model aliases on the published share path.

Two things must hold at once: an aliased model reads as its display name on
the public endpoints, and nothing derived from the real model id (cost, most
of all) changes because an alias exists.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import uuid

import pytest

from oddish.core.helpers import build_trial_response
from oddish.core.model_display_names import (
    apply_model_display_names,
    canonical_model_key,
    display_model_name,
    experiment_display_names,
    mask_trajectory_model_names,
)
from oddish.core.sharing.helpers import (
    ensure_experiment_public,
    list_experiment_trials_for_org,
    list_task_trials_for_public_experiment,
)
from oddish.core.sharing.public import get_public_task_status
from oddish.db import (
    ExperimentModel,
    TaskModel,
    TrialModel,
    generate_id,
    get_session,
    task_experiments,
)
from oddish.schemas import TrialOrigin, TrialStatus


def _unique(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


class _Exp:
    """Anything ``experiment_display_names`` reads: a ``public_model_renames``."""

    def __init__(self, renames):
        self.public_model_renames = renames


def test_migration_drops_global_aliases_without_copying_them():
    migration = (
        Path(__file__).resolve().parents[1]
        / "alembic/versions/expmodelrename01_experiment_model_renames.py"
    ).read_text()

    assert "DROP TABLE IF EXISTS model_display_names" in migration
    assert "UPDATE experiments" not in migration


def test_followup_migration_removes_only_learnability_aliases():
    migration = (
        Path(__file__).resolve().parents[1]
        / "alembic/versions/expmodelrename02_remove_learnability_renames.py"
    ).read_text()

    assert "public_model_renames - ARRAY" in migration
    assert "key ILIKE '%learnability%'" in migration
    assert "value ILIKE '%learnability%'" in migration


def _trial_response(
    *, model: str, queue_key: str, input_tokens: int = 1_000_000
) -> object:
    trial = TrialModel(
        id=generate_id(),
        name="t",
        task_id="task",
        agent="codex",
        provider="openai",
        queue_key=queue_key,
        model=model,
        status=TrialStatus.SUCCESS,
        origin=TrialOrigin.ODDISH,
        attempts=1,
        max_attempts=1,
        is_probe=False,
        created_at=datetime(2026, 8, 4, tzinfo=timezone.utc),
        input_tokens=input_tokens,
        output_tokens=0,
        cache_tokens=0,
    )
    return build_trial_response(trial, "s3://tasks/t")


async def _cleanup_task_and_experiment(
    task_id: str | None, exp_id: str | None
) -> None:
    async with get_session() as cleanup:
        if task_id:
            await cleanup.execute(
                TrialModel.__table__.delete().where(TrialModel.task_id == task_id)
            )
            await cleanup.execute(
                task_experiments.delete().where(task_experiments.c.task_id == task_id)
            )
            await cleanup.execute(
                TaskModel.__table__.delete().where(TaskModel.id == task_id)
            )
        if exp_id:
            await cleanup.execute(
                ExperimentModel.__table__.delete().where(ExperimentModel.id == exp_id)
            )


def test_alias_replaces_model_and_matching_queue_key():
    response = _trial_response(model="spiffy-balloon", queue_key="spiffy-balloon")
    apply_model_display_names([response], {"spiffy-balloon": "bananas"})

    assert response.model == "bananas"
    assert response.queue_key == "bananas"


def test_alias_replaces_only_the_model_segment_of_a_queue_key():
    response = _trial_response(model="spiffy-balloon", queue_key="xai/spiffy-balloon")
    apply_model_display_names([response], {"spiffy-balloon": "bananas"})

    assert response.queue_key == "xai/bananas"


def test_unrelated_queue_key_is_left_alone():
    response = _trial_response(model="spiffy-balloon", queue_key="shared-pool")
    apply_model_display_names([response], {"spiffy-balloon": "bananas"})

    assert response.model == "bananas"
    assert response.queue_key == "shared-pool"


def test_unmapped_models_pass_through_untouched():
    response = _trial_response(model="gpt-5.5", queue_key="openai/gpt-5.5")
    apply_model_display_names([response], {"spiffy-balloon": "bananas"})

    assert response.model == "gpt-5.5"
    assert response.queue_key == "openai/gpt-5.5"


def test_alias_matching_is_case_and_spelling_insensitive():
    response = _trial_response(model="Spiffy Balloon", queue_key="q")
    apply_model_display_names([response], {"spiffy-balloon": "bananas"})

    assert response.model == "bananas"


def test_alias_does_not_change_resolved_cost():
    """The invariant: cost resolves from the real model id, before aliasing.

    An alias that reached ``_resolve_trial_cost`` would miss the price table
    and silently zero the trial out.
    """
    priced = _trial_response(model="gpt-5.5", queue_key="openai/gpt-5.5")
    assert priced.cost_usd, "fixture must have a priced model to guard"
    before = priced.cost_usd

    apply_model_display_names([priced], {"gpt-5.5": "bananas"})

    assert priced.model == "bananas"
    assert priced.cost_usd == before


@pytest.mark.parametrize(
    "typed", ["Spiffy-Balloon", "spiffy-balloon", "  SPIFFY  BALLOON  "]
)
def test_writers_canonicalize_to_one_key(typed):
    """Case/whitespace variants collapse to one stored key, and that key is one
    the read side looks up."""
    assert canonical_model_key(typed) == "spiffy-balloon"


def test_experiment_display_names_expands_stored_keys():
    """A stored canonical key resolves under whatever spelling a trial carries."""
    exp = _Exp({canonical_model_key("Spiffy Balloon"): "bananas"})
    assert experiment_display_names(exp).get("spiffy-balloon") == "bananas"


def test_experiment_display_names_empty_when_unset_or_blank():
    assert experiment_display_names(_Exp(None)) == {}
    assert experiment_display_names(_Exp({})) == {}
    assert experiment_display_names(_Exp({"spiffy-balloon": ""})) == {}


@pytest.mark.asyncio
async def test_public_share_renders_the_alias_and_org_export_does_not():
    model = _unique("spiffy-balloon")
    task_id: str | None = None
    exp_id: str | None = None
    try:
        async with get_session() as setup:
            task = TaskModel(
                name=_unique("alias-task"),
                org_id="org1",
                user="tester",
                task_path="s3://tasks/alias",
            )
            setup.add(task)
            await setup.flush()
            task_id = task.id

            exp = ExperimentModel(
                name=_unique("alias-exp"),
                org_id="org1",
                public_model_renames={canonical_model_key(model): "bananas"},
            )
            setup.add(exp)
            await setup.flush()
            exp_id = exp.id
            await setup.execute(
                task_experiments.insert(),
                {"task_id": task.id, "experiment_id": exp.id},
            )

            trial_id = generate_id()
            setup.add(
                TrialModel(
                    id=trial_id,
                    name=trial_id,
                    task_id=task.id,
                    experiment_id=exp.id,
                    org_id="org1",
                    agent="codex",
                    provider="openai",
                    queue_key=model,
                    model=model,
                )
            )

            await ensure_experiment_public(setup, exp)
            await setup.flush()
            token = exp.public_token
            assert token

        async with get_session() as session:
            public_trials = await list_task_trials_for_public_experiment(
                session, token, task_id
            )
        assert [t.model for t in public_trials] == ["bananas"]

        status = await get_public_task_status(token, task_id)
        assert [t.model for t in status.trials] == ["bananas"]

        async with get_session() as session:
            org_trials = await list_experiment_trials_for_org(session, exp_id, "org1")
        assert [t.model for t in org_trials] == [model]
    finally:
        await _cleanup_task_and_experiment(task_id, exp_id)


@pytest.mark.asyncio
async def test_experiment_without_a_map_shows_real_model_ids():
    """No renames set is not "hide it" -- the real id is what the share shows."""
    model = _unique("plain-model")
    task_id: str | None = None
    exp_id: str | None = None
    try:
        async with get_session() as setup:
            task = TaskModel(
                name=_unique("plain-task"),
                org_id="org1",
                user="tester",
                task_path="s3://tasks/plain",
            )
            setup.add(task)
            await setup.flush()
            task_id = task.id

            exp = ExperimentModel(name=_unique("plain-exp"), org_id="org1")
            setup.add(exp)
            await setup.flush()
            exp_id = exp.id
            await setup.execute(
                task_experiments.insert(),
                {"task_id": task.id, "experiment_id": exp.id},
            )

            trial_id = generate_id()
            setup.add(
                TrialModel(
                    id=trial_id,
                    name=trial_id,
                    task_id=task.id,
                    experiment_id=exp.id,
                    org_id="org1",
                    agent="codex",
                    provider="openai",
                    queue_key=model,
                    model=model,
                )
            )

            await ensure_experiment_public(setup, exp)
            await setup.flush()
            token = exp.public_token
            assert token

        status = await get_public_task_status(token, task_id)
        assert [t.model for t in status.trials] == [model]
    finally:
        await _cleanup_task_and_experiment(task_id, exp_id)


def test_display_model_name_masks_through_the_same_keys():
    """A model id carried outside a TrialResponse resolves through the same
    lookup, so a share page never masks the id in one place and prints it in
    another."""
    names = {canonical_model_key("anthropic/claude-opus-4-8"): "Model A"}
    assert display_model_name("anthropic/claude-opus-4-8", names) == "Model A"
    # Same spelling rules the trial path gets: case and surrounding whitespace
    # collapse into the one stored key.
    assert display_model_name("  Anthropic/Claude-Opus-4-8 ", names) == "Model A"


def test_display_model_name_passes_unaliased_ids_through():
    """No alias set is not the same as "hide it" -- the id is what the page
    showed before this feature, and still shows."""
    names = {canonical_model_key("anthropic/claude-opus-4-8"): "Model A"}
    assert display_model_name("openai/gpt-5.4", names) == "openai/gpt-5.4"
    assert display_model_name("openai/gpt-5.4", {}) == "openai/gpt-5.4"
    assert display_model_name(None, names) is None


def test_mask_trajectory_model_names_masks_agent_and_steps():
    """A share page renders the trajectory's own model_name in every step
    header, so leaving it raw prints the real id under the aliased one."""
    names = {canonical_model_key("xai/v9m-rl-learnability-tp8"): "xai/Grok-4.5"}
    trajectory = {
        "agent": {"name": "grok-build", "model_name": "xai/v9m-rl-learnability-tp8"},
        "steps": [
            {"step_id": 0, "model_name": "xai/v9m-rl-learnability-tp8"},
            {"step_id": 1, "model_name": None},
        ],
    }

    masked = mask_trajectory_model_names(trajectory, names)

    assert masked["agent"]["model_name"] == "xai/Grok-4.5"
    assert [step["model_name"] for step in masked["steps"]] == ["xai/Grok-4.5", None]


def test_mask_trajectory_model_names_leaves_the_cached_document_alone():
    """read_trial_trajectory memoizes the parsed document and the
    authenticated route serves that same object -- masking must copy."""
    names = {canonical_model_key("xai/v9m-rl-learnability-tp8"): "xai/Grok-4.5"}
    trajectory = {
        "agent": {"model_name": "xai/v9m-rl-learnability-tp8"},
        "steps": [{"step_id": 0, "model_name": "xai/v9m-rl-learnability-tp8"}],
    }

    mask_trajectory_model_names(trajectory, names)

    assert trajectory["agent"]["model_name"] == "xai/v9m-rl-learnability-tp8"
    assert trajectory["steps"][0]["model_name"] == "xai/v9m-rl-learnability-tp8"


def test_mask_trajectory_model_names_no_aliases_is_a_passthrough():
    """The common case pays nothing: no renames, same object back."""
    trajectory = {"agent": {"model_name": "openai/gpt-5.4"}, "steps": []}
    assert mask_trajectory_model_names(trajectory, {}) is trajectory
    assert mask_trajectory_model_names(None, {"a": "b"}) is None


def test_mask_trajectory_model_names_tolerates_a_non_string_model_name():
    """The ATIF document is agent-written JSON, not a validated schema. A
    stray non-string must not 500 the whole trajectory read."""
    names = {canonical_model_key("xai/v9m-rl-learnability-tp8"): "xai/Grok-4.5"}
    trajectory = {
        "agent": {"model_name": 7},
        "steps": [{"step_id": 0, "model_name": {"nested": "junk"}}],
    }

    masked = mask_trajectory_model_names(trajectory, names)

    assert masked["agent"]["model_name"] == 7
    assert masked["steps"][0]["model_name"] == {"nested": "junk"}
