from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from oddish.core.probe import auto_probe


def test_default_probe_skill_id_is_a_seed_skill():
    # The auto-probe must reference a skill, not a preset.
    assert hasattr(auto_probe, "DEFAULT_PROBE_SKILL_ID")
    assert not hasattr(auto_probe, "DEFAULT_PROBE_PRESET_ID")


def test_auto_probe_does_not_import_probe_preset_model():
    import inspect

    src = inspect.getsource(auto_probe)
    assert "ProbePresetModel" not in src


@pytest.mark.asyncio
async def test_auto_probe_admits_before_append(monkeypatch):
    events: list[tuple] = []

    class Session:
        async def scalar(self, _stmt):
            return SimpleNamespace(
                id=auto_probe.DEFAULT_PROBE_SKILL_ID,
                name="Cheat Detector",
                operator_prompt="try to cheat",
                result_focus=None,
                evaluation_metric=None,
            )

    task = SimpleNamespace(
        id="task-1",
        name="task-1",
        current_version_id="task-1-v1",
        task_path="s3://bucket/task.tar.gz",
        user="alice",
    )

    async def fake_already_probed(_session, _version_id):
        return False

    async def fake_probed_count(_session, _org_id):
        return 0

    async def fake_admit(_session, org_id, billed_user_id, count):
        events.append(("admit", org_id, billed_user_id, count))

    async def fake_append(_session, *, billed_user_id, **_kwargs):
        events.append(("append", billed_user_id))
        return []

    monkeypatch.setattr(auto_probe, "_version_already_probed", fake_already_probed)
    monkeypatch.setattr(auto_probe, "_probed_version_count", fake_probed_count)
    monkeypatch.setattr(auto_probe, "next_probe_model", lambda _count: "openai/gpt-5")
    monkeypatch.setattr(auto_probe, "admit_trials", fake_admit)
    monkeypatch.setattr(auto_probe, "append_trials_to_task", fake_append)

    await auto_probe.maybe_enqueue_auto_probe(
        Session(),
        task=task,
        experiment=SimpleNamespace(id="exp-1"),
        org_id="org-1",
        billed_user_id="user-1",
    )

    assert events == [
        ("admit", "org-1", "user-1", 1),
        ("append", "user-1"),
    ]


@pytest.mark.asyncio
async def test_auto_probe_skips_append_when_admission_blocks(monkeypatch):
    events: list[tuple] = []

    class Session:
        async def scalar(self, _stmt):
            return SimpleNamespace(
                id=auto_probe.DEFAULT_PROBE_SKILL_ID,
                name="Cheat Detector",
                operator_prompt="try to cheat",
                result_focus=None,
                evaluation_metric=None,
            )

    task = SimpleNamespace(
        id="task-1",
        name="task-1",
        current_version_id="task-1-v1",
        task_path="s3://bucket/task.tar.gz",
        user="alice",
    )

    async def fake_already_probed(_session, _version_id):
        return False

    async def fake_probed_count(_session, _org_id):
        return 0

    async def fake_admit(_session, org_id, billed_user_id, count):
        events.append(("admit", org_id, billed_user_id, count))
        raise HTTPException(status_code=402, detail="over budget")

    async def fake_append(*_args, **_kwargs):
        events.append(("append",))
        return []

    monkeypatch.setattr(auto_probe, "_version_already_probed", fake_already_probed)
    monkeypatch.setattr(auto_probe, "_probed_version_count", fake_probed_count)
    monkeypatch.setattr(auto_probe, "next_probe_model", lambda _count: "openai/gpt-5")
    monkeypatch.setattr(auto_probe, "admit_trials", fake_admit)
    monkeypatch.setattr(auto_probe, "append_trials_to_task", fake_append)

    await auto_probe.maybe_enqueue_auto_probe(
        Session(),
        task=task,
        experiment=None,
        org_id="org-1",
        billed_user_id="user-1",
    )

    assert events == [("admit", "org-1", "user-1", 1)]
