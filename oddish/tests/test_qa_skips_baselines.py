"""QA never runs on nop/oracle baselines.

Three independent guards, one per path that would otherwise spend on a
deterministic baseline: the per-trial classifier, the assignment-driven
post-trial analyzer, and the pre-trial task-source audit.

Runs against a real Postgres (``ODDISH_DATABASE_URL``), like the other queue
tests.
"""

from __future__ import annotations

import sys
import uuid
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import select

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from oddish.db import (  # noqa: E402
    TaskModel,
    TrialModel,
    TrialStatus,
    WorkerJobModel,
    get_session,
)
from oddish.queue import append_trials_to_task, create_task  # noqa: E402
from oddish.schemas import TaskSubmission, TrialSpec  # noqa: E402
from oddish.workers.queue.qa_handler import (  # noqa: E402
    _load_live_trials_for_classification,
)

_RUN = uuid.uuid4().hex[:8]
_LLM_AGENT = "claude-code"
_LLM_MODEL = "claude-sonnet-4-5"

# Suffixed/prefixed variants must be caught too -- an "oracle-v2" is still a
# baseline, and matching only the bare names would silently keep paying for it.
_BASELINE_AGENTS = ("nop", "oracle", "oracle-v2", "agent-nop", "NOP")


@pytest_asyncio.fixture
async def cleanup_task_ids():
    task_ids: list[str] = []
    yield task_ids
    async with get_session() as session:
        for task_id in task_ids:
            await session.execute(
                WorkerJobModel.__table__.delete().where(
                    WorkerJobModel.subject_id.like(f"{task_id}%")
                )
            )
            await session.execute(
                TaskModel.__table__.delete().where(TaskModel.id == task_id)
            )


def _submission(name: str, agents: list[tuple[str, str | None]]) -> TaskSubmission:
    return TaskSubmission(
        name=name,
        task_path="s3://test-bucket/qa-skip-fake-task",
        user="test",
        trials=[TrialSpec(agent=a, model=m) for a, m in agents],
    )


async def _finish_all_trials(task_id: str) -> None:
    """Mark every trial SUCCESS so it is otherwise QA-eligible."""
    async with get_session() as session:
        trials = (
            await session.execute(
                select(TrialModel).where(TrialModel.task_id == task_id)
            )
        ).scalars().all()
        for trial in trials:
            trial.status = TrialStatus.SUCCESS


@pytest.mark.asyncio
async def test_classifier_skips_baselines_including_variants(cleanup_task_ids):
    """The QA classifier loads the LLM trial and none of the baselines."""
    task_id = f"qa-skip-cls-{_RUN}"
    cleanup_task_ids.append(task_id)
    agents = [(a, None) for a in _BASELINE_AGENTS] + [(_LLM_AGENT, _LLM_MODEL)]
    async with get_session() as session:
        await create_task(session, _submission("cls", agents), task_id=task_id)
    await _finish_all_trials(task_id)

    async with get_session() as session:
        agent_by_id = dict(
            (
                await session.execute(
                    select(TrialModel.id, TrialModel.agent).where(
                        TrialModel.task_id == task_id
                    )
                )
            ).all()
        )

    live_ids = {tid for tid, _ in await _load_live_trials_for_classification(task_id)}

    assert [agent_by_id[tid] for tid in live_ids] == [_LLM_AGENT]
    # Named individually so a regression says WHICH variant leaked through.
    for tid, agent in agent_by_id.items():
        if agent != _LLM_AGENT:
            assert tid not in live_ids, f"baseline {agent!r} was queued for QA"


@pytest.mark.asyncio
async def test_post_trial_hooks_skip_baseline_trials(monkeypatch, cleanup_task_ids):
    """A finished baseline enqueues no post-trial analyzer; an LLM trial does."""
    from oddish.workers.queue import trial_handler

    task_id = f"qa-skip-post-{_RUN}"
    cleanup_task_ids.append(task_id)
    async with get_session() as session:
        await create_task(
            session,
            _submission("post", [("oracle", None), (_LLM_AGENT, _LLM_MODEL)]),
            task_id=task_id,
        )
    await _finish_all_trials(task_id)

    enqueued_for: list[str] = []

    async def _fake_enqueue(session, **kwargs):
        enqueued_for.append(kwargs["trial_id"])

    monkeypatch.setattr(
        "oddish.core.qa_assignments.enqueue_qa_assignment_runs_core", _fake_enqueue
    )
    # The gate and stage transition are exercised elsewhere; stub them so this
    # test fails only on the enqueue decision.
    monkeypatch.setattr(
        "oddish.queue.maybe_gate_llm_trials", lambda *a, **k: _noop_true(False)
    )
    monkeypatch.setattr(
        "oddish.queue.maybe_start_qa_stage", lambda *a, **k: _noop_true(False)
    )

    async with get_session() as session:
        rows = (
            await session.execute(
                select(TrialModel.id, TrialModel.agent).where(
                    TrialModel.task_id == task_id
                )
            )
        ).all()
    by_agent = {agent: tid for tid, agent in rows}

    await trial_handler._run_post_trial_hooks(by_agent["oracle"])
    assert enqueued_for == [], "post-trial QA was enqueued for an oracle baseline"

    await trial_handler._run_post_trial_hooks(by_agent[_LLM_AGENT])
    assert enqueued_for == [by_agent[_LLM_AGENT]]


async def _noop_true(value: bool) -> bool:
    return value


@pytest.mark.asyncio
async def test_pre_trial_audit_skipped_for_baseline_only_append(
    monkeypatch, cleanup_task_ids
):
    """A baseline-only append audits nothing; adding an LLM agent audits."""
    from oddish import queue as queue_mod

    task_id = f"qa-skip-pre-{_RUN}"
    cleanup_task_ids.append(task_id)
    async with get_session() as session:
        task = await create_task(
            session,
            _submission("pre", [(_LLM_AGENT, _LLM_MODEL)]),
            task_id=task_id,
        )

    stages: list[str] = []

    async def _fake_enqueue(session, **kwargs):
        stages.append(kwargs["stage"])

    monkeypatch.setattr(
        "oddish.core.qa_assignments.enqueue_qa_assignment_runs_core", _fake_enqueue
    )

    async with get_session() as session:
        task = await session.get(queue_mod.TaskModel, task_id)
        await append_trials_to_task(
            session,
            task=task,
            submission=_submission("pre", [("nop", None), ("oracle-v2", None)]),
        )
    assert stages == [], "a baseline-only append triggered a pre-trial audit"

    async with get_session() as session:
        task = await session.get(queue_mod.TaskModel, task_id)
        await append_trials_to_task(
            session,
            task=task,
            submission=_submission("pre", [("nop", None), (_LLM_AGENT, _LLM_MODEL)]),
        )
    assert stages == ["pre_trial"]
