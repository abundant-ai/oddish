import pytest

from oddish.worker import probe_staging


class _Trial:
    def __init__(self, id, task_id, experiment_id):
        self.id = id
        self.task_id = task_id
        self.experiment_id = experiment_id
        self.harbor_config = {}


class _FakeStorage:
    def __init__(self):
        self.prefixes = []

    async def list_keys(self, prefix):
        self.prefixes.append(prefix)
        return []  # no files -> nothing staged, but prefixes are recorded

    async def download_bytes(self, key):  # pragma: no cover
        return b""


def _fake_loader(current, trials):
    async def _loader(current_trial_id):
        return current, trials

    return _loader


@pytest.mark.asyncio
async def test_experiment_scope_uses_per_trial_task_prefix(tmp_path, monkeypatch):
    # Two trials in the same experiment but DIFFERENT tasks.
    current = _Trial("cur", "task-A", "exp-1")
    other = _Trial("sib", "task-B", "exp-1")

    monkeypatch.setattr(
        probe_staging, "_load_experiment_trials", _fake_loader(current, [current, other])
    )
    storage = _FakeStorage()
    monkeypatch.setattr(probe_staging, "get_storage_client", lambda: storage)

    await probe_staging.stage_related_trial_logs(
        tmp_path, "task-A", "cur", probe_scope="experiment"
    )

    # The sibling is staged under its OWN task_id, not the host task-A.
    assert "tasks/task-B/trials/sib/" in storage.prefixes
    # The current trial is excluded from its own related set.
    assert "tasks/task-A/trials/cur/" not in storage.prefixes


@pytest.mark.asyncio
async def test_experiment_scope_no_trials_returns_false(tmp_path, monkeypatch):
    monkeypatch.setattr(
        probe_staging, "_load_experiment_trials", _fake_loader(None, [])
    )
    storage = _FakeStorage()
    monkeypatch.setattr(probe_staging, "get_storage_client", lambda: storage)

    staged = await probe_staging.stage_related_trial_logs(
        tmp_path, "task-A", "cur", probe_scope="experiment"
    )
    assert staged is False
    assert storage.prefixes == []
