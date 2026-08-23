"""Version an append pins its new trials to.

A sweep re-submission that uploads nothing must stay on the version the target
experiment already runs, so appended trials land on the same pivot the
experiment grid displays instead of dragging the view onto a newer default that
some unrelated run set.
"""

from __future__ import annotations

from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from oddish.core.endpoints import sweep as sweep_mod  # noqa: E402


def _task(current_version_id: str | None = "task-a-v16"):
    return SimpleNamespace(id="task-a", current_version_id=current_version_id)


@pytest.fixture
def effective_versions(monkeypatch):
    """Stub the experiment pivot lookup and record the args it was called with."""
    calls: list[dict] = []
    mapping: dict[str, str] = {}

    async def fake_fetch(session, *, experiment_id, task_ids):
        calls.append({"experiment_id": experiment_id, "task_ids": list(task_ids)})
        return dict(mapping)

    monkeypatch.setattr(
        "oddish.core.helpers.fetch_experiment_effective_version_ids", fake_fetch
    )
    return SimpleNamespace(calls=calls, mapping=mapping)


@pytest.mark.asyncio
async def test_no_upload_pins_to_the_experiments_version(effective_versions):
    effective_versions.mapping["task-a"] = "task-a-v15"

    version_id = await sweep_mod.resolve_append_version_id(
        None,
        task=_task(current_version_id="task-a-v16"),
        experiment_id="exp-1",
        uploaded_content_hash=None,
    )

    assert version_id == "task-a-v15"
    assert effective_versions.calls == [
        {"experiment_id": "exp-1", "task_ids": ["task-a"]}
    ]


@pytest.mark.asyncio
async def test_upload_keeps_the_task_default(effective_versions):
    effective_versions.mapping["task-a"] = "task-a-v15"

    version_id = await sweep_mod.resolve_append_version_id(
        None,
        task=_task(current_version_id="task-a-v16"),
        experiment_id="exp-1",
        uploaded_content_hash="deadbeef",
    )

    assert version_id == "task-a-v16"
    assert effective_versions.calls == []


@pytest.mark.asyncio
async def test_task_new_to_the_experiment_keeps_the_task_default(effective_versions):
    version_id = await sweep_mod.resolve_append_version_id(
        None,
        task=_task(current_version_id="task-a-v16"),
        experiment_id="exp-1",
        uploaded_content_hash=None,
    )

    assert version_id == "task-a-v16"


@pytest.mark.asyncio
async def test_no_target_experiment_keeps_the_task_default(effective_versions):
    version_id = await sweep_mod.resolve_append_version_id(
        None,
        task=_task(current_version_id="task-a-v16"),
        experiment_id=None,
        uploaded_content_hash=None,
    )

    assert version_id == "task-a-v16"
    assert effective_versions.calls == []


@pytest.mark.asyncio
async def test_versionless_task_stays_versionless(effective_versions):
    version_id = await sweep_mod.resolve_append_version_id(
        None,
        task=_task(current_version_id=None),
        experiment_id="exp-1",
        uploaded_content_hash=None,
    )

    assert version_id is None


@pytest.mark.asyncio
async def test_use_default_version_overrides_the_experiments_version(
    effective_versions,
):
    effective_versions.mapping["task-a"] = "task-a-v15"

    version_id = await sweep_mod.resolve_append_version_id(
        None,
        task=_task(current_version_id="task-a-v16"),
        experiment_id="exp-1",
        uploaded_content_hash=None,
        use_default_version=True,
    )

    assert version_id == "task-a-v16"
    assert effective_versions.calls == []


def test_payload_carries_use_default_version():
    from oddish.cli.api import build_sweep_payload

    def _payload(**kw):
        return build_sweep_payload(
            task_id="task-a",
            configs=[],
            environment=None,
            user=None,
            priority="low",
            experiment_id="exp-1",
            append_to_task=True,
            **kw,
        )

    assert _payload(use_default_version=True)["use_default_version"] is True
    assert "use_default_version" not in _payload()


def test_submission_defaults_to_the_experiments_version():
    from oddish.schemas import TaskSweepSubmission

    submission = TaskSweepSubmission(task_id="task-a", configs=[])

    assert submission.use_default_version is False


@pytest.mark.asyncio
async def test_implicit_primary_experiment_keeps_the_task_default(effective_versions):
    """A submission with no ``experiment_id`` must not adopt the primary's version.

    ``oddish probe`` submits with ``experiment_id=None``, and the sweep falls back
    to the task's primary experiment for trial ownership. Pinning to that
    experiment's version would probe older content than the task now holds.
    """
    effective_versions.mapping["task-a"] = "task-a-v15"

    version_id = await sweep_mod.resolve_append_version_id(
        None,
        task=_task(current_version_id="task-a-v16"),
        experiment_id=None,
        uploaded_content_hash=None,
    )

    assert version_id == "task-a-v16"
    assert effective_versions.calls == []


@pytest.mark.asyncio
async def test_auto_probe_follows_the_pinned_version(monkeypatch):
    """The probe must inspect the version the trials were pinned to.

    Auto-probe defaults to ``task.current_version_id``. When an append pins an
    older version, a probe on the default inspects content no trial ran, is
    filtered out of the experiment grid, and leaves the pinned version
    unprobed while marking the default as already probed.
    """
    from oddish.core.probe import auto_probe

    seen: dict = {}

    async def fake_already_probed(session, version_id):
        seen["checked_version"] = version_id
        return True  # stop before the skill lookup; the version is the assertion

    monkeypatch.setattr(auto_probe, "_version_already_probed", fake_already_probed)

    await auto_probe.maybe_enqueue_auto_probe(
        None,
        task=SimpleNamespace(id="task-a", current_version_id="task-a-v16"),
        experiment=None,
        org_id=None,
        task_version_id="task-a-v15",
    )

    assert seen["checked_version"] == "task-a-v15"


@pytest.mark.asyncio
async def test_auto_probe_defaults_to_the_task_version(monkeypatch):
    from oddish.core.probe import auto_probe

    seen: dict = {}

    async def fake_already_probed(session, version_id):
        seen["checked_version"] = version_id
        return True

    monkeypatch.setattr(auto_probe, "_version_already_probed", fake_already_probed)

    await auto_probe.maybe_enqueue_auto_probe(
        None,
        task=SimpleNamespace(id="task-a", current_version_id="task-a-v16"),
        experiment=None,
        org_id=None,
    )

    assert seen["checked_version"] == "task-a-v16"
