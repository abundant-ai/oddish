from __future__ import annotations

import pytest

from oddish.db import ExperimentModel
from oddish.queue import get_or_create_experiment


class _Result:
    def __init__(self, experiment: ExperimentModel | None) -> None:
        self.experiment = experiment

    def scalar_one_or_none(self) -> ExperimentModel | None:
        return self.experiment


class _Session:
    def __init__(self, experiment: ExperimentModel | None = None) -> None:
        self.experiment = experiment
        self.added: ExperimentModel | None = None

    async def execute(self, _query) -> _Result:
        return _Result(self.experiment)

    def add(self, experiment: ExperimentModel) -> None:
        self.added = experiment

    async def flush(self) -> None:
        return None


@pytest.mark.asyncio
async def test_new_experiment_is_constructed_with_provenance() -> None:
    session = _Session()

    experiment = await get_or_create_experiment(
        session,  # type: ignore[arg-type]
        "new-experiment",
        "org-1",
        owner_user_id="user-1",
        owner="octocat",
        link="https://example.com/pull/1",
    )

    assert session.added is experiment
    assert experiment.owner_user_id == "user-1"
    assert experiment.owner == "octocat"
    assert experiment.link == "https://example.com/pull/1"


@pytest.mark.asyncio
async def test_existing_experiment_provenance_is_never_claimed() -> None:
    existing = ExperimentModel(
        id="experiment-1",
        name="existing",
        org_id="org-1",
        owner_user_id=None,
        owner=None,
        link=None,
    )
    session = _Session(existing)

    experiment = await get_or_create_experiment(
        session,  # type: ignore[arg-type]
        "existing",
        "org-1",
        owner_user_id="later-user",
        owner="later-owner",
        link="https://example.com/later",
    )

    assert experiment is existing
    assert session.added is None
    assert experiment.owner_user_id is None
    assert experiment.owner is None
    assert experiment.link is None
