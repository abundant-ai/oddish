"""Operator model aliases on the published share path.

Two things must hold at once: an aliased model reads as its display name on
the public endpoints, and nothing derived from the real model id (cost, most
of all) changes because an alias exists.
"""

from __future__ import annotations

from datetime import datetime, timezone
import uuid

import pytest

from oddish.core.helpers import build_trial_response
from oddish.core.model_display_names import (
    apply_model_display_names,
    load_model_display_names,
)
from oddish.core.sharing.helpers import (
    ensure_experiment_public,
    list_experiment_trials_for_org,
    list_task_trials_for_public_experiment,
)
from oddish.core.sharing.public import get_public_task_status
from oddish.db import (
    ExperimentModel,
    ModelDisplayNameModel,
    TaskModel,
    TrialModel,
    generate_id,
    get_session,
    task_experiments,
)
from oddish.schemas import TrialOrigin, TrialStatus


def _unique(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


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


@pytest.mark.asyncio
async def test_public_share_renders_the_alias_and_org_export_does_not():
    model = _unique("spiffy-balloon")
    task_id: str | None = None
    exp_id: str | None = None
    alias_id: str | None = None
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

            exp = ExperimentModel(name=_unique("alias-exp"), org_id="org1")
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

            alias = ModelDisplayNameModel(
                model_name=model, display_name="bananas", created_by_user_id="tester"
            )
            setup.add(alias)
            await setup.flush()
            alias_id = alias.id

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
        async with get_session() as cleanup:
            if alias_id:
                await cleanup.execute(
                    ModelDisplayNameModel.__table__.delete().where(
                        ModelDisplayNameModel.id == alias_id
                    )
                )
            if task_id:
                await cleanup.execute(
                    TrialModel.__table__.delete().where(TrialModel.task_id == task_id)
                )
                await cleanup.execute(
                    task_experiments.delete().where(
                        task_experiments.c.task_id == task_id
                    )
                )
                await cleanup.execute(
                    TaskModel.__table__.delete().where(TaskModel.id == task_id)
                )
            if exp_id:
                await cleanup.execute(
                    ExperimentModel.__table__.delete().where(
                        ExperimentModel.id == exp_id
                    )
                )


@pytest.mark.asyncio
async def test_removed_alias_stops_renaming():
    model = _unique("retired-model")
    alias_id: str | None = None
    try:
        async with get_session() as setup:
            alias = ModelDisplayNameModel(model_name=model, display_name="bananas")
            setup.add(alias)
            await setup.flush()
            alias_id = alias.id

        async with get_session() as session:
            assert (await load_model_display_names(session)).get(model) == "bananas"

        async with get_session() as session:
            row = await session.get(ModelDisplayNameModel, alias_id)
            from oddish.db import utcnow

            row.deleted_at = utcnow()

        async with get_session() as session:
            assert model not in await load_model_display_names(session)
    finally:
        if alias_id:
            async with get_session() as cleanup:
                await cleanup.execute(
                    ModelDisplayNameModel.__table__.delete().where(
                        ModelDisplayNameModel.id == alias_id
                    )
                )
