import pytest
from tests.cc_chat.conftest import seed_task_with_trials, ORG
from api.services.cc_chat.task_files import collect_task_version_files

pytestmark = pytest.mark.asyncio


class _FakeStorage:
    """Returns one file per trial prefix."""
    def __init__(self):
        self.downloads = 0
    async def list_keys(self, prefix):
        return [f"{prefix}trial.log", f"{prefix}agent/output.txt"]
    async def download_bytes(self, key):
        self.downloads += 1
        return b"x" * 10


async def test_collects_trials_grouped_by_version_with_files(db):
    seeded = await seed_task_with_trials(db, versions=(1, 2), trials_per_version=1)
    storage = _FakeStorage()
    async with db() as s:
        current_version, version_trials, files, truncated = await collect_task_version_files(
            s, storage, task_id="task_1", org_id=ORG,
        )
    assert current_version == 2
    assert set(version_trials.keys()) == {1, 2}
    # each trial contributes 2 files, organized under jobs/v{version}/{trial_id}/
    paths = {rel for rel, _ in files}
    assert any(p.startswith("jobs/v2/") and p.endswith("trial.log") for p in paths)
    assert any(p.startswith("jobs/v1/") for p in paths)
    assert truncated is False


async def test_respects_byte_cap(db):
    await seed_task_with_trials(db, versions=(1,), trials_per_version=1)
    storage = _FakeStorage()
    async with db() as s:
        _, _, files, truncated = await collect_task_version_files(
            s, storage, task_id="task_1", org_id=ORG, max_total_bytes=5,
        )
    assert truncated is True
    assert len(files) == 0  # cap hit before the first 10-byte file
