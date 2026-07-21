"""End-to-end coverage for the task-registry query surface added to
``browse_tasks_core``: version-scoped runtime filters (``allow_internet``,
``gpus_min``/``max``, ``cpus_min``, ``memory_mb_min``), the task-scoped
``expert_hours_min``/``max``, and the provenance-scoped
``source_repo``/``ci_pr_number``/``uploader_is_ci`` filters over
``task_upload_events``.

This branch has hit the same "accepted at one layer, silently dropped at the
next" defect repeatedly, so each filter here is driven through
``browse_tasks_core`` (not just checked for a kwarg in the signature) and
asserted to both include the matching task and exclude a non-matching one.
"""

from __future__ import annotations

import uuid
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from oddish.core.endpoints import browse_tasks_core
from oddish.db.models import TaskModel, TaskUploadEventModel, TaskVersionModel


def _org() -> str:
    return f"taskreg-query-{uuid.uuid4().hex[:8]}"


async def _make_task(
    session,
    *,
    org_id: str,
    name: str,
    expert_time_hours: float | None = None,
    allow_internet: bool | None = None,
    gpus: int | None = None,
    cpus: int | None = None,
    memory_mb: int | None = None,
) -> TaskModel:
    task = TaskModel(
        name=name,
        org_id=org_id,
        user="tester",
        task_path=f"s3://tasks/{name}",
        expert_time_hours=expert_time_hours,
    )
    session.add(task)
    await session.flush()
    version = TaskVersionModel(
        id=f"{task.id}-v1",
        task_id=task.id,
        version=1,
        task_path=task.task_path,
        allow_internet=allow_internet,
        gpus=gpus,
        cpus=cpus,
        memory_mb=memory_mb,
    )
    session.add(version)
    await session.flush()
    task.current_version_id = version.id
    await session.flush()
    return task


async def _add_upload_event(
    session,
    *,
    task: TaskModel,
    source_repo: str | None = None,
    ci_pr_number: int | None = None,
    uploader_is_ci: bool | None = None,
) -> TaskUploadEventModel:
    event = TaskUploadEventModel(
        task_id=task.id,
        task_version_id=task.current_version_id,
        created_version=True,
        source_repo=source_repo,
        ci_pr_number=ci_pr_number,
        uploader_is_ci=uploader_is_ci,
    )
    session.add(event)
    await session.flush()
    return event


# ---------------------------------------------------------------------------
# Version-scoped: allow_internet
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_browse_filters_by_allow_internet(session):
    org_id = _org()
    online = await _make_task(
        session, org_id=org_id, name=f"online-{org_id}", allow_internet=True
    )
    offline = await _make_task(
        session, org_id=org_id, name=f"offline-{org_id}", allow_internet=False
    )
    await session.flush()

    resp = await browse_tasks_core(session, org_id=org_id, allow_internet=True)
    ids = {item.id for item in resp.items}
    assert online.id in ids
    assert offline.id not in ids

    item = next(i for i in resp.items if i.id == online.id)
    assert item.allow_internet is True


@pytest.mark.asyncio
async def test_browse_filters_by_allow_internet_false(session):
    # A predicate written as truthiness (``if allow_internet:``) rather than
    # an identity check (``if allow_internet is not None:``) would pass the
    # True-only test above but silently ignore this case.
    org_id = _org()
    online = await _make_task(
        session, org_id=org_id, name=f"online-{org_id}", allow_internet=True
    )
    offline = await _make_task(
        session, org_id=org_id, name=f"offline-{org_id}", allow_internet=False
    )
    await session.flush()

    resp = await browse_tasks_core(session, org_id=org_id, allow_internet=False)
    ids = {item.id for item in resp.items}
    assert offline.id in ids
    assert online.id not in ids

    item = next(i for i in resp.items if i.id == offline.id)
    assert item.allow_internet is False


@pytest.mark.asyncio
async def test_browse_filters_by_gpus_and_cpus_and_memory(session):
    org_id = _org()
    beefy = await _make_task(
        session,
        org_id=org_id,
        name=f"beefy-{org_id}",
        gpus=4,
        cpus=16,
        memory_mb=65536,
    )
    tiny = await _make_task(
        session,
        org_id=org_id,
        name=f"tiny-{org_id}",
        gpus=0,
        cpus=1,
        memory_mb=512,
    )
    await session.flush()

    resp = await browse_tasks_core(session, org_id=org_id, gpus_min=2, gpus_max=8)
    ids = {item.id for item in resp.items}
    assert beefy.id in ids
    assert tiny.id not in ids

    resp = await browse_tasks_core(session, org_id=org_id, cpus_min=8)
    ids = {item.id for item in resp.items}
    assert beefy.id in ids
    assert tiny.id not in ids

    resp = await browse_tasks_core(session, org_id=org_id, memory_mb_min=32768)
    ids = {item.id for item in resp.items}
    assert beefy.id in ids
    assert tiny.id not in ids


# ---------------------------------------------------------------------------
# Task-scoped: expert_hours_min / expert_hours_max
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_browse_filters_by_expert_hours_range(session):
    org_id = _org()
    easy = await _make_task(
        session, org_id=org_id, name=f"easy-{org_id}", expert_time_hours=1.0
    )
    hard = await _make_task(
        session, org_id=org_id, name=f"hard-{org_id}", expert_time_hours=40.0
    )
    await session.flush()

    resp = await browse_tasks_core(
        session, org_id=org_id, expert_hours_min=10.0, expert_hours_max=100.0
    )
    ids = {item.id for item in resp.items}
    assert hard.id in ids
    assert easy.id not in ids


# ---------------------------------------------------------------------------
# Provenance-scoped: source_repo / ci_pr_number / uploader_is_ci (ANY event)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_browse_filters_by_source_repo_any_upload_event(session):
    """THE MOTIVATING semantics: a task that has EVER had an upload event
    from ``source_repo`` matches, even if a LATER upload event came from
    somewhere else -- proving this isn't a latest-only comparison.
    """
    org_id = _org()
    from_harbor_lh = await _make_task(
        session, org_id=org_id, name=f"from-harbor-lh-{org_id}"
    )
    from_elsewhere = await _make_task(
        session, org_id=org_id, name=f"from-elsewhere-{org_id}"
    )
    await _add_upload_event(
        session, task=from_harbor_lh, source_repo="abundant-ai/harbor-lh"
    )
    # A later, non-matching re-upload must not make the task stop matching.
    await _add_upload_event(session, task=from_harbor_lh, source_repo="someone/laptop")
    await _add_upload_event(session, task=from_elsewhere, source_repo="someone/laptop")
    await session.flush()

    resp = await browse_tasks_core(
        session, org_id=org_id, source_repo="abundant-ai/harbor-lh"
    )
    ids = {item.id for item in resp.items}
    assert from_harbor_lh.id in ids
    assert from_elsewhere.id not in ids


@pytest.mark.asyncio
async def test_browse_filters_by_ci_pr_number_and_uploader_is_ci(session):
    org_id = _org()
    ci_task = await _make_task(session, org_id=org_id, name=f"ci-task-{org_id}")
    manual_task = await _make_task(session, org_id=org_id, name=f"manual-task-{org_id}")
    await _add_upload_event(
        session, task=ci_task, ci_pr_number=412, uploader_is_ci=True
    )
    await _add_upload_event(session, task=manual_task, uploader_is_ci=False)
    await session.flush()

    resp = await browse_tasks_core(session, org_id=org_id, ci_pr_number=412)
    ids = {item.id for item in resp.items}
    assert ci_task.id in ids
    assert manual_task.id not in ids

    resp = await browse_tasks_core(session, org_id=org_id, uploader_is_ci=True)
    ids = {item.id for item in resp.items}
    assert ci_task.id in ids
    assert manual_task.id not in ids


@pytest.mark.asyncio
async def test_browse_item_exposes_category_and_allow_internet(session):
    """``description`` is deliberately NOT part of TaskBrowseItem: it's an
    8192-char Text column and the browse query already sorts/materializes
    every task in the org before pagination (see ``ranked_tasks`` in
    tasks_query.py) -- carrying it there is pure cost with no consumer.
    ``category``/``allow_internet`` are cheap and rendered on the task card.
    """
    org_id = _org()
    task = await _make_task(
        session, org_id=org_id, name=f"meta-{org_id}", allow_internet=True
    )
    task.category = "ml-training"
    await session.flush()

    resp = await browse_tasks_core(session, org_id=org_id)
    item = next(i for i in resp.items if i.id == task.id)
    assert not hasattr(item, "description")
    assert item.category == "ml-training"
    assert item.allow_internet is True
