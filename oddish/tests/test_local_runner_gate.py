"""Integration tests for the baseline gate on the ODDISH_LOCAL_MODE path.

The in-process ``run_trial_locally`` is local mode's only dispatch entrypoint
and has no Modal dispatcher/reconciler behind it. These tests assert it (a)
honors ``BLOCKED`` worker_jobs via an atomic self-claim so gated LLM trials
don't run prematurely, (b) can't double-dispatch the same trial, and (c) drives
the baseline gate on baseline completion -- releasing valid tasks' LLM trials
and cancelling faulty ones -- exactly as the cloud trial handler does.

Runs against a real Postgres (``ODDISH_DATABASE_URL``), like the other queue
tests.
"""

from __future__ import annotations

import asyncio
import sys
import uuid
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import select, update

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from oddish.config import settings  # noqa: E402
from oddish.core import quota_enforcement  # noqa: E402
from oddish.core.baseline_gate import GATE_SKIP_MESSAGE  # noqa: E402
from oddish.db import (  # noqa: E402
    AnalysisStatus,
    TaskModel,
    TrialModel,
    TrialStatus,
    WorkerJobModel,
    WorkerJobStatus,
    get_session,
    utcnow,
)
from oddish.queue import create_task  # noqa: E402
from oddish.schemas import TaskSubmission, TrialSpec  # noqa: E402
from oddish.worker import local_runner  # noqa: E402
from oddish.worker.local_runner import run_trial_locally  # noqa: E402

_RUN = uuid.uuid4().hex[:8]
_LLM_AGENT = "claude-code"
_LLM_MODEL = "claude-sonnet-4-5"


def _mixed_submission(name: str) -> TaskSubmission:
    return TaskSubmission(
        name=name,
        task_path="s3://test-bucket/local-runner-gate-fake-task",
        user="test",
        trials=[
            TrialSpec(agent="oracle", model=None),
            TrialSpec(agent="nop", model=None),
            TrialSpec(agent=_LLM_AGENT, model=_LLM_MODEL),
        ],
    )


@pytest_asyncio.fixture
async def cleanup_task_ids():
    ids: list[str] = []
    yield ids
    async with get_session() as s:
        for tid in ids:
            await s.execute(
                WorkerJobModel.__table__.delete().where(
                    WorkerJobModel.subject_id == tid
                )
            )
            await s.execute(TaskModel.__table__.delete().where(TaskModel.id == tid))


async def _trial_id(task_id: str, agent: str) -> str:
    async with get_session() as session:
        return (
            await session.execute(
                select(TrialModel.id).where(
                    TrialModel.task_id == task_id, TrialModel.agent == agent
                )
            )
        ).scalar_one()


async def _trial_status(trial_id: str) -> TrialStatus:
    async with get_session() as session:
        return (
            await session.execute(
                select(TrialModel.status).where(TrialModel.id == trial_id)
            )
        ).scalar_one()


async def _wj_status(trial_id: str) -> WorkerJobStatus:
    async with get_session() as session:
        return (
            await session.execute(
                select(WorkerJobModel.status).where(
                    WorkerJobModel.subject_id == trial_id
                )
            )
        ).scalar_one()


async def _drain_pending() -> None:
    """Let any fire-and-forget run_trial_locally re-dispatch tasks settle."""
    for _ in range(5):
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_local_runner_skips_blocked_trial(monkeypatch, cleanup_task_ids):
    """A gated (BLOCKED) LLM trial must not run when dispatched locally."""
    monkeypatch.setattr(settings, "gate_llm_on_baselines", True)
    ran: list[str] = []

    async def _spy(trial_id: str) -> None:
        ran.append(trial_id)

    monkeypatch.setattr(local_runner, "_run_harbor_trial", _spy)

    task_id = f"local-blocked-{_RUN}"
    cleanup_task_ids.append(task_id)
    async with get_session() as session:
        await create_task(session, _mixed_submission("blocked"), task_id=task_id)

    llm_id = await _trial_id(task_id, _LLM_AGENT)
    assert await _wj_status(llm_id) == WorkerJobStatus.BLOCKED

    await run_trial_locally(llm_id, dry_run=False)

    assert ran == []  # Harbor never invoked
    assert await _trial_status(llm_id) == TrialStatus.QUEUED
    assert await _wj_status(llm_id) == WorkerJobStatus.BLOCKED


@pytest.mark.asyncio
@pytest.mark.parametrize("dry_run", [False, True])
async def test_local_runner_no_double_dispatch(monkeypatch, cleanup_task_ids, dry_run):
    monkeypatch.setattr(settings, "gate_llm_on_baselines", False)
    calls: list[str] = []
    quota_checks = []
    settlement_order = []
    local_dispatches = []

    async def _spy(trial_id: str) -> None:
        # Yield so both coroutines reach the claim before either finishes.
        await asyncio.sleep(0)
        calls.append(trial_id)

    async def _check_quota(**kwargs) -> int:
        after_check = kwargs.pop("after_check")
        after_gate_release = kwargs.pop("after_gate_release")
        quota_checks.append(kwargs)
        settlement_order.append("quota_checked")
        await after_gate_release(["released-by-quota"])
        await after_check()
        settlement_order.append("remote_teardown")
        return 0

    async def _post_hooks(*_args, **_kwargs) -> None:
        settlement_order.append("post_hooks")

    async def _dispatch_spy(trial_id: str, *, dry_run: bool = False) -> None:
        local_dispatches.append((trial_id, dry_run))

    monkeypatch.setattr(local_runner, "_run_harbor_trial", _spy)
    monkeypatch.setattr(local_runner, "_local_post_trial_hooks", _post_hooks)
    monkeypatch.setattr(local_runner, "run_trial_locally", _dispatch_spy)
    monkeypatch.setattr(
        quota_enforcement, "enforce_trial_quotas_until_checked", _check_quota
    )

    task_id = f"local-double-{_RUN}"
    cleanup_task_ids.append(task_id)
    async with get_session() as session:
        await create_task(
            session,
            _mixed_submission("double"),
            task_id=task_id,
            org_id="org-local",
            billed_user_id="user-local",
        )

    oracle_id = await _trial_id(task_id, "oracle")
    await asyncio.gather(
        run_trial_locally(oracle_id, dry_run=dry_run),
        run_trial_locally(oracle_id, dry_run=dry_run),
    )
    await _drain_pending()

    assert calls == ([] if dry_run else [oracle_id])
    assert quota_checks == [
        {
            "org_id": "org-local",
            "billed_user_id": "user-local",
            "caller_trial_id": oracle_id,
        }
    ]
    assert settlement_order == ["quota_checked", "post_hooks", "remote_teardown"]
    assert local_dispatches == [("released-by-quota", dry_run)]
    assert await _trial_status(oracle_id) == TrialStatus.SUCCESS


@pytest.mark.asyncio
async def test_local_runner_preserves_concurrent_cancellation(
    monkeypatch, cleanup_task_ids
):
    monkeypatch.setattr(settings, "gate_llm_on_baselines", False)
    started = asyncio.Event()
    release = asyncio.Event()
    quota_checks = []
    post_hooks = []

    async def _run(_trial_id: str) -> None:
        started.set()
        await release.wait()

    async def _check_quota(**kwargs) -> int:
        after_check = kwargs.pop("after_check")
        after_gate_release = kwargs.pop("after_gate_release")
        quota_checks.append(kwargs)
        await after_gate_release([])
        await after_check()
        return 0

    async def _post_hooks(*_args, **_kwargs) -> None:
        post_hooks.append(True)

    monkeypatch.setattr(local_runner, "_run_harbor_trial", _run)
    monkeypatch.setattr(local_runner, "_local_post_trial_hooks", _post_hooks)
    monkeypatch.setattr(
        quota_enforcement, "enforce_trial_quotas_until_checked", _check_quota
    )

    task_id = f"local-cancel-{_RUN}"
    cleanup_task_ids.append(task_id)
    async with get_session() as session:
        await create_task(
            session,
            _mixed_submission("cancel"),
            task_id=task_id,
            org_id="org-local",
            billed_user_id="user-local",
        )

    oracle_id = await _trial_id(task_id, "oracle")
    runner = asyncio.create_task(run_trial_locally(oracle_id, dry_run=False))
    await asyncio.wait_for(started.wait(), timeout=10)
    async with get_session() as session:
        trial = await session.get(TrialModel, oracle_id, with_for_update=True)
        trial.status = TrialStatus.FAILED
        trial.error_message = "Cancelled because quota was reached"
        trial.analysis_status = AnalysisStatus.FAILED
        trial.analysis_error = "Cancelled because quota was reached"
        trial.finished_at = utcnow()
        trial.harbor_stage = "cancelled"

    release.set()
    await asyncio.wait_for(runner, timeout=10)

    async with get_session() as session:
        trial = await session.get(TrialModel, oracle_id)
        assert trial.status == TrialStatus.FAILED
        assert trial.analysis_status == AnalysisStatus.FAILED
        assert trial.analysis_error == "Cancelled because quota was reached"
    assert quota_checks == [
        {
            "org_id": "org-local",
            "billed_user_id": "user-local",
            "caller_trial_id": oracle_id,
        }
    ]
    assert post_hooks == []


@pytest.mark.asyncio
async def test_local_runner_releases_llm_on_valid_baselines(
    monkeypatch, cleanup_task_ids
):
    """Completing the last baseline locally releases the BLOCKED LLM trial."""
    monkeypatch.setattr(settings, "gate_llm_on_baselines", True)
    task_id = f"local-valid-{_RUN}"
    cleanup_task_ids.append(task_id)
    async with get_session() as session:
        await create_task(session, _mixed_submission("valid"), task_id=task_id)

    llm_id = await _trial_id(task_id, _LLM_AGENT)
    oracle_id = await _trial_id(task_id, "oracle")

    # nop already done cleanly; pre-stamp oracle's reward (dry-run skips Harbor,
    # so the runner won't set it) and let run_trial_locally drive it terminal.
    async with get_session() as session:
        await session.execute(
            update(TrialModel)
            .where(TrialModel.task_id == task_id, TrialModel.agent == "nop")
            .values(status=TrialStatus.SUCCESS, reward=0.0, finished_at=utcnow())
        )
        await session.execute(
            update(TrialModel).where(TrialModel.id == oracle_id).values(reward=1.0)
        )

    await run_trial_locally(oracle_id, dry_run=True)
    await _drain_pending()

    # Gate validated the task -> LLM job released to QUEUED, never cancelled.
    assert await _wj_status(llm_id) == WorkerJobStatus.QUEUED
    assert await _trial_status(llm_id) != TrialStatus.FAILED


@pytest.mark.asyncio
async def test_local_runner_cancels_llm_on_faulty_baselines(
    monkeypatch, cleanup_task_ids
):
    """A faulty oracle cancels the BLOCKED LLM trial when run locally."""
    monkeypatch.setattr(settings, "gate_llm_on_baselines", True)
    task_id = f"local-faulty-{_RUN}"
    cleanup_task_ids.append(task_id)
    async with get_session() as session:
        await create_task(session, _mixed_submission("faulty"), task_id=task_id)

    llm_id = await _trial_id(task_id, _LLM_AGENT)
    oracle_id = await _trial_id(task_id, "oracle")

    # nop fails cleanly, oracle ALSO scores 0 -> task is faulty.
    async with get_session() as session:
        await session.execute(
            update(TrialModel)
            .where(TrialModel.task_id == task_id, TrialModel.agent == "nop")
            .values(status=TrialStatus.SUCCESS, reward=0.0, finished_at=utcnow())
        )
        await session.execute(
            update(TrialModel).where(TrialModel.id == oracle_id).values(reward=0.0)
        )

    await run_trial_locally(oracle_id, dry_run=True)
    await _drain_pending()

    assert await _wj_status(llm_id) == WorkerJobStatus.CANCELLED
    assert await _trial_status(llm_id) == TrialStatus.SKIPPED
    async with get_session() as session:
        msg = (
            await session.execute(
                select(TrialModel.error_message).where(TrialModel.id == llm_id)
            )
        ).scalar_one()
    assert GATE_SKIP_MESSAGE in (msg or "")
