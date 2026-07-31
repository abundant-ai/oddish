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
