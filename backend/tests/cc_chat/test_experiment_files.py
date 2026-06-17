import pytest
from sqlalchemy import text
from tests.cc_chat.conftest import seed_task_with_trials, ORG
from api.services.cc_chat.experiment_files import collect_experiment_files

pytestmark = pytest.mark.asyncio


class _FakeStorage:
    """Returns one file per trial prefix."""
    def __init__(self):
        self.downloads = 0
    async def list_keys(self, prefix):
        return [f"{prefix}result.json", f"{prefix}agent/trajectory.json"]
    async def download_bytes(self, key):
        self.downloads += 1
        return b"x" * 10


async def test_collects_experiment_trials_with_files(db):
    await seed_task_with_trials(db, versions=(1, 2), trials_per_version=1)
    storage = _FakeStorage()
    async with db() as s:
        trial_ids, files, truncated, probe_trial_ids = await collect_experiment_files(
            s, storage, experiment_id="exp_task_1", org_id=ORG,
        )
    # both versions' trials belong to the same experiment → flat by experiment
    assert set(trial_ids) == {"task_1-10", "task_1-20"}
    paths = {rel for rel, _ in files}
    # laid out under jobs/{experiment_id}/{trial_id}/ to match the CLAUDE.md template
    assert any(p.startswith("jobs/exp_task_1/task_1-10/") and p.endswith("result.json") for p in paths)
    assert any(p.endswith("agent/trajectory.json") for p in paths)
    assert truncated is False
    assert probe_trial_ids == set()


async def test_respects_byte_cap(db):
    await seed_task_with_trials(db, versions=(1,), trials_per_version=1)
    storage = _FakeStorage()
    async with db() as s:
        _, files, truncated, _ = await collect_experiment_files(
            s, storage, experiment_id="exp_task_1", org_id=ORG, max_total_bytes=5,
        )
    assert truncated is True
    assert len(files) == 0


async def test_flags_probe_trials(db):
    seeded = await seed_task_with_trials(db, versions=(2,), trials_per_version=2)
    probe_id = seeded[2][0]
    async with db() as s:
        await s.execute(
            text("update trials set is_probe = true where id = :id"), {"id": probe_id}
        )
        await s.commit()
    storage = _FakeStorage()
    async with db() as s:
        _, _, _, probe_trial_ids = await collect_experiment_files(
            s, storage, experiment_id="exp_task_1", org_id=ORG,
        )
    assert probe_trial_ids == {probe_id}


async def test_empty_experiment_yields_nothing(db):
    storage = _FakeStorage()
    async with db() as s:
        trial_ids, files, truncated, probe_trial_ids = await collect_experiment_files(
            s, storage, experiment_id="exp_does_not_exist", org_id=ORG,
        )
    assert trial_ids == []
    assert files == []
    assert truncated is False
    assert probe_trial_ids == set()
