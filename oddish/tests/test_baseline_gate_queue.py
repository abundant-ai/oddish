"""Integration tests for the baseline gate on the task-create / completion path.

When ``settings.gate_llm_on_baselines`` is on and a task mixes nop/oracle
baselines with LLM agents, the LLM trials are enqueued ``BLOCKED`` and only
released once the baselines finish: ``QUEUED`` if the baselines validate the
task, ``CANCELLED`` (trial mirrored to ``FAILED``) if they prove it faulty.

Runs against a real Postgres (``ODDISH_DATABASE_URL``), like the other
queue tests.
"""

from __future__ import annotations

import sys
import uuid
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import select, update
from sqlalchemy.orm import selectinload

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from oddish.config import settings  # noqa: E402
from oddish.core.baseline_gate import GATE_SKIP_MESSAGE  # noqa: E402
from oddish.core.cost_basis import CANCELLED_HARBOR_STAGE  # noqa: E402
from oddish.db import (  # noqa: E402
    TaskModel,
    TaskStatus,
    TrialModel,
    TrialStatus,
    WorkerJobKind,
    WorkerJobModel,
    WorkerJobStatus,
    get_session,
    utcnow,
)
from oddish.queue import (  # noqa: E402
    append_trials_to_task,
    create_task,
    get_or_create_experiment,
    maybe_gate_llm_trials,
)
from oddish.schemas import TaskSubmission, TrialSpec  # noqa: E402

_RUN = uuid.uuid4().hex[:8]
_LLM_AGENT = "claude-code"
_LLM_MODEL = "claude-sonnet-4-5"


def _mixed_submission(name: str) -> TaskSubmission:
    return TaskSubmission(
        name=name,
        task_path="s3://test-bucket/baseline-gate-fake-task",
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
            # ON DELETE CASCADE removes trials + task_versions + worker_jobs.
            await s.execute(
                WorkerJobModel.__table__.delete().where(
                    WorkerJobModel.subject_id == tid
                )
            )
            await s.execute(TaskModel.__table__.delete().where(TaskModel.id == tid))


async def _job_status_by_agent(task_id: str) -> dict[str, WorkerJobStatus]:
    """Map each trial's agent -> its TRIAL worker_job status."""
    async with get_session() as session:
        rows = (
            await session.execute(
                select(TrialModel.agent, WorkerJobModel.status)
                .join(
                    WorkerJobModel,
                    WorkerJobModel.subject_id == TrialModel.id,
                )
                .where(TrialModel.task_id == task_id)
            )
        ).all()
    return {agent: status for agent, status in rows}


async def _set_baseline_outcomes(
    task_id: str, *, oracle_reward: float, nop_reward: float
) -> str:
    """Drive the baseline trials terminal; return a baseline trial id."""
    async with get_session() as session:
        for agent, reward in (("oracle", oracle_reward), ("nop", nop_reward)):
            await session.execute(
                update(TrialModel)
                .where(TrialModel.task_id == task_id, TrialModel.agent == agent)
                .values(
                    status=TrialStatus.SUCCESS,
                    reward=reward,
                    finished_at=utcnow(),
                )
            )
        baseline_id = (
            await session.execute(
                select(TrialModel.id).where(
                    TrialModel.task_id == task_id, TrialModel.agent == "oracle"
                )
            )
        ).scalar_one()
    return baseline_id


@pytest.mark.asyncio
async def test_llm_trials_blocked_when_flag_on(monkeypatch, cleanup_task_ids):
    monkeypatch.setattr(settings, "gate_llm_on_baselines", True)
    task_id = f"gate-blocked-{_RUN}"
    cleanup_task_ids.append(task_id)
    async with get_session() as session:
        await create_task(session, _mixed_submission("blocked"), task_id=task_id)

    statuses = await _job_status_by_agent(task_id)
    assert statuses["oracle"] == WorkerJobStatus.QUEUED
    assert statuses["nop"] == WorkerJobStatus.QUEUED
    assert statuses[_LLM_AGENT] == WorkerJobStatus.BLOCKED


@pytest.mark.asyncio
async def test_no_gating_when_flag_off(monkeypatch, cleanup_task_ids):
    monkeypatch.setattr(settings, "gate_llm_on_baselines", False)
    task_id = f"gate-off-{_RUN}"
    cleanup_task_ids.append(task_id)
    async with get_session() as session:
        await create_task(session, _mixed_submission("off"), task_id=task_id)

    statuses = await _job_status_by_agent(task_id)
    assert set(statuses.values()) == {WorkerJobStatus.QUEUED}


@pytest.mark.asyncio
async def test_valid_baselines_unblock_llm(monkeypatch, cleanup_task_ids):
    monkeypatch.setattr(settings, "gate_llm_on_baselines", True)
    task_id = f"gate-valid-{_RUN}"
    cleanup_task_ids.append(task_id)
    async with get_session() as session:
        await create_task(session, _mixed_submission("valid"), task_id=task_id)

    baseline_id = await _set_baseline_outcomes(
        task_id, oracle_reward=1.0, nop_reward=0.0
    )
    async with get_session() as session:
        gated = await maybe_gate_llm_trials(session, baseline_id)
    assert gated is True

    statuses = await _job_status_by_agent(task_id)
    assert statuses[_LLM_AGENT] == WorkerJobStatus.QUEUED

    async with get_session() as session:
        llm_trial = (
            await session.execute(
                select(TrialModel).where(
                    TrialModel.task_id == task_id, TrialModel.agent == _LLM_AGENT
                )
            )
        ).scalar_one()
    assert llm_trial.status != TrialStatus.FAILED


@pytest.mark.asyncio
async def test_faulty_baselines_cancel_llm(monkeypatch, cleanup_task_ids):
    monkeypatch.setattr(settings, "gate_llm_on_baselines", True)
    task_id = f"gate-faulty-{_RUN}"
    cleanup_task_ids.append(task_id)
    async with get_session() as session:
        await create_task(session, _mixed_submission("faulty"), task_id=task_id)

    # Oracle fails -> task faulty.
    baseline_id = await _set_baseline_outcomes(
        task_id, oracle_reward=0.0, nop_reward=0.0
    )
    async with get_session() as session:
        gated = await maybe_gate_llm_trials(session, baseline_id)
    assert gated is True

    statuses = await _job_status_by_agent(task_id)
    assert statuses[_LLM_AGENT] == WorkerJobStatus.CANCELLED

    async with get_session() as session:
        llm_trial = (
            await session.execute(
                select(TrialModel).where(
                    TrialModel.task_id == task_id, TrialModel.agent == _LLM_AGENT
                )
            )
        ).scalar_one()
    assert llm_trial.status == TrialStatus.SKIPPED
    assert GATE_SKIP_MESSAGE in (llm_trial.error_message or "")


def _baseline_submission(name: str) -> TaskSubmission:
    return TaskSubmission(
        name=name,
        task_path="s3://test-bucket/baseline-gate-fake-task",
        user="test",
        trials=[TrialSpec(agent="nop", model=None)],
    )


def _append_submission() -> TaskSubmission:
    return TaskSubmission(
        name="append",
        task_path="s3://test-bucket/baseline-gate-fake-task",
        user="test",
        trials=[
            TrialSpec(agent="oracle", model=None),
            TrialSpec(agent="nop", model=None),
            TrialSpec(agent=_LLM_AGENT, model=_LLM_MODEL),
        ],
    )


async def _load_task(task_id: str) -> TaskModel:
    async with get_session() as session:
        task = (
            await session.execute(
                select(TaskModel)
                .options(selectinload(TaskModel.experiments))
                .where(TaskModel.id == task_id)
            )
        ).scalar_one()
        await append_trials_to_task(session, task=task, submission=_append_submission())
    return task


@pytest.mark.asyncio
async def test_appended_llm_blocked_with_new_baselines(monkeypatch, cleanup_task_ids):
    # Initial task created without gating (single nop baseline, flag off).
    monkeypatch.setattr(settings, "gate_llm_on_baselines", False)
    task_id = f"gate-append-{_RUN}"
    cleanup_task_ids.append(task_id)
    async with get_session() as session:
        await create_task(session, _baseline_submission("seed"), task_id=task_id)

    # Append oracle + nop + LLM with gating on: the appended LLM trial blocks.
    monkeypatch.setattr(settings, "gate_llm_on_baselines", True)
    await _load_task(task_id)

    statuses = await _job_status_by_agent(task_id)
    assert statuses[_LLM_AGENT] == WorkerJobStatus.BLOCKED
    assert statuses["oracle"] == WorkerJobStatus.QUEUED

    # Appended baselines validate the task -> the appended LLM trial releases.
    baseline_id = await _set_baseline_outcomes(
        task_id, oracle_reward=1.0, nop_reward=0.0
    )
    async with get_session() as session:
        assert await maybe_gate_llm_trials(session, baseline_id) is True

    statuses = await _job_status_by_agent(task_id)
    assert statuses[_LLM_AGENT] == WorkerJobStatus.QUEUED


@pytest.mark.asyncio
async def test_gate_is_experiment_scoped(monkeypatch, cleanup_task_ids):
    """A faulty verdict in one experiment must not cancel another experiment's
    blocked LLM trials on the same task."""
    monkeypatch.setattr(settings, "gate_llm_on_baselines", True)
    task_id = f"gate-expscope-{_RUN}"
    cleanup_task_ids.append(task_id)

    # Experiment A: created with the task (oracle + nop + kimi).
    async with get_session() as session:
        await create_task(session, _mixed_submission("expA"), task_id=task_id)

    # Experiment B: append the same trio under a second experiment.
    async with get_session() as session:
        exp_a = (
            await session.execute(
                select(TrialModel.experiment_id)
                .where(TrialModel.task_id == task_id)
                .limit(1)
            )
        ).scalar_one()
        exp_b = (
            await get_or_create_experiment(session, name=f"gate-expscope-B-{_RUN}")
        ).id
        task = (
            await session.execute(
                select(TaskModel)
                .options(selectinload(TaskModel.experiments))
                .where(TaskModel.id == task_id)
            )
        ).scalar_one()
        await append_trials_to_task(
            session,
            task=task,
            submission=_mixed_submission("expB"),
            experiment_id=exp_b,
        )

    async def _llm_status_by_exp() -> dict[str, set]:
        async with get_session() as session:
            rows = (
                await session.execute(
                    select(TrialModel.experiment_id, WorkerJobModel.status)
                    .join(WorkerJobModel, WorkerJobModel.subject_id == TrialModel.id)
                    .where(
                        TrialModel.task_id == task_id, TrialModel.agent == _LLM_AGENT
                    )
                )
            ).all()
        out: dict[str, set] = {}
        for exp, st in rows:
            out.setdefault(exp, set()).add(st)
        return out

    before = await _llm_status_by_exp()
    assert before[exp_a] == {WorkerJobStatus.BLOCKED}
    assert before[exp_b] == {WorkerJobStatus.BLOCKED}

    # Drive ONLY experiment A's baselines faulty (oracle fails).
    async with get_session() as session:
        await session.execute(
            update(TrialModel)
            .where(
                TrialModel.task_id == task_id,
                TrialModel.experiment_id == exp_a,
                TrialModel.queue_key == "nop_oracle",
            )
            .values(status=TrialStatus.SUCCESS, reward=0.0, finished_at=utcnow())
        )
        a_oracle_id = (
            await session.execute(
                select(TrialModel.id).where(
                    TrialModel.task_id == task_id,
                    TrialModel.experiment_id == exp_a,
                    TrialModel.agent == "oracle",
                )
            )
        ).scalar_one()

    async with get_session() as session:
        assert await maybe_gate_llm_trials(session, a_oracle_id) is True

    # A's kimi cancelled; B's kimi untouched (still blocked).
    after = await _llm_status_by_exp()
    assert after[exp_a] == {WorkerJobStatus.CANCELLED}
    assert after[exp_b] == {WorkerJobStatus.BLOCKED}


# ---------------------------------------------------------------------------
# Pull-path: an agent-only append/retry consults the scope's EXISTING baselines
# ---------------------------------------------------------------------------


def _baselines_only_submission() -> TaskSubmission:
    return TaskSubmission(
        name="baselines",
        task_path="s3://test-bucket/baseline-gate-fake-task",
        user="test",
        trials=[
            TrialSpec(agent="oracle", model=None),
            TrialSpec(agent="nop", model=None),
        ],
    )


def _llm_only_submission() -> TaskSubmission:
    return TaskSubmission(
        name="llm-only",
        task_path="s3://test-bucket/baseline-gate-fake-task",
        user="test",
        trials=[TrialSpec(agent=_LLM_AGENT, model=_LLM_MODEL)],
    )


async def _append_llm_only(task_id: str) -> str:
    """Append a single agent-only LLM trial; return its id."""
    async with get_session() as session:
        task = (
            await session.execute(
                select(TaskModel)
                .options(selectinload(TaskModel.experiments))
                .where(TaskModel.id == task_id)
            )
        ).scalar_one()
        new = await append_trials_to_task(
            session, task=task, submission=_llm_only_submission()
        )
    return new[0].id


async def _wj_status(trial_id: str) -> WorkerJobStatus:
    async with get_session() as session:
        return (
            await session.execute(
                select(WorkerJobModel.status).where(
                    WorkerJobModel.subject_id == trial_id,
                    WorkerJobModel.kind == WorkerJobKind.TRIAL,
                )
            )
        ).scalar_one()


@pytest.mark.asyncio
async def test_agent_only_append_runs_when_baselines_valid(
    monkeypatch, cleanup_task_ids
):
    monkeypatch.setattr(settings, "gate_llm_on_baselines", True)
    task_id = f"pull-valid-{_RUN}"
    cleanup_task_ids.append(task_id)
    async with get_session() as session:
        await create_task(session, _baselines_only_submission(), task_id=task_id)
    await _set_baseline_outcomes(task_id, oracle_reward=1.0, nop_reward=0.0)

    kimi = await _append_llm_only(task_id)
    assert await _wj_status(kimi) == WorkerJobStatus.QUEUED


@pytest.mark.asyncio
async def test_agent_only_append_cancelled_when_baselines_faulty(
    monkeypatch, cleanup_task_ids
):
    monkeypatch.setattr(settings, "gate_llm_on_baselines", True)
    task_id = f"pull-faulty-{_RUN}"
    cleanup_task_ids.append(task_id)
    async with get_session() as session:
        await create_task(session, _baselines_only_submission(), task_id=task_id)
    await _set_baseline_outcomes(task_id, oracle_reward=0.0, nop_reward=0.0)

    kimi = await _append_llm_only(task_id)
    assert await _wj_status(kimi) == WorkerJobStatus.CANCELLED
    async with get_session() as session:
        tr = (
            await session.execute(select(TrialModel).where(TrialModel.id == kimi))
        ).scalar_one()
    assert tr.status == TrialStatus.SKIPPED
    assert GATE_SKIP_MESSAGE in (tr.error_message or "")


@pytest.mark.asyncio
async def test_agent_only_append_blocked_when_baselines_pending(
    monkeypatch, cleanup_task_ids
):
    monkeypatch.setattr(settings, "gate_llm_on_baselines", True)
    task_id = f"pull-pending-{_RUN}"
    cleanup_task_ids.append(task_id)
    # Baselines created but left active (not driven terminal).
    async with get_session() as session:
        await create_task(session, _baselines_only_submission(), task_id=task_id)

    kimi = await _append_llm_only(task_id)
    assert await _wj_status(kimi) == WorkerJobStatus.BLOCKED


@pytest.mark.asyncio
async def test_agent_only_append_ungated_without_baselines(
    monkeypatch, cleanup_task_ids
):
    monkeypatch.setattr(settings, "gate_llm_on_baselines", True)
    task_id = f"pull-none-{_RUN}"
    cleanup_task_ids.append(task_id)
    # Task has no baselines at all -> appended LLM trial runs ungated.
    async with get_session() as session:
        await create_task(session, _llm_only_submission(), task_id=task_id)

    kimi = await _append_llm_only(task_id)
    assert await _wj_status(kimi) == WorkerJobStatus.QUEUED


# ---------------------------------------------------------------------------
# Review fixes: retry stays gated + QA excludes gate-skipped trials
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_retry_of_gated_llm_trial_reports_skipped(monkeypatch, cleanup_task_ids):
    from oddish.core.endpoints.trials import retry_trial_core

    monkeypatch.setattr(settings, "gate_llm_on_baselines", True)
    task_id = f"retry-gate-{_RUN}"
    cleanup_task_ids.append(task_id)
    async with get_session() as session:
        await create_task(session, _mixed_submission("retry"), task_id=task_id)
    # Faulty baselines -> the create-blocked kimi is cancelled.
    baseline_id = await _set_baseline_outcomes(
        task_id, oracle_reward=0.0, nop_reward=0.0
    )
    async with get_session() as session:
        await maybe_gate_llm_trials(session, baseline_id)

    async with get_session() as session:
        kimi_id = (
            await session.execute(
                select(TrialModel.id).where(
                    TrialModel.task_id == task_id, TrialModel.agent == _LLM_AGENT
                )
            )
        ).scalar_one()

    # Retrying the gate-cancelled trial must re-gate: faulty baselines -> the
    # new trial is cancelled too, and the endpoint reports its real state.
    async with get_session() as session:
        result = await retry_trial_core(session, trial_id=kimi_id, org_id=None)
    assert result["status"] == "skipped"

    # ...and the task is advanced, not left stuck RUNNING after the cancel.
    async with get_session() as session:
        task = (
            await session.execute(select(TaskModel).where(TaskModel.id == task_id))
        ).scalar_one()
    assert task.status not in (TaskStatus.PENDING, TaskStatus.RUNNING)


@pytest.mark.asyncio
async def test_qa_classification_excludes_gate_skipped_and_cancelled(
    monkeypatch, cleanup_task_ids
):
    from oddish.workers.queue.qa_handler import (
        _load_live_trials_for_classification,
    )

    monkeypatch.setattr(settings, "gate_llm_on_baselines", True)
    task_id = f"qa-skip-{_RUN}"
    cleanup_task_ids.append(task_id)
    async with get_session() as session:
        await create_task(session, _mixed_submission("qa"), task_id=task_id)
    baseline_id = await _set_baseline_outcomes(
        task_id, oracle_reward=0.0, nop_reward=0.0
    )
    async with get_session() as session:
        await maybe_gate_llm_trials(session, baseline_id)
        cancelled_baseline = await session.get(TrialModel, baseline_id)
        cancelled_baseline.harbor_stage = CANCELLED_HARBOR_STAGE

    async with get_session() as session:
        kimi_id = (
            await session.execute(
                select(TrialModel.id).where(
                    TrialModel.task_id == task_id, TrialModel.agent == _LLM_AGENT
                )
            )
        ).scalar_one()

    live_ids = {tid for tid, _ in await _load_live_trials_for_classification(task_id)}
    # Neither the gate-skipped (never-run) kimi nor the cancelled baseline has
    # an outcome to classify; the other completed baseline remains eligible.
    assert kimi_id not in live_ids
    assert baseline_id not in live_ids
    assert live_ids


# ---------------------------------------------------------------------------
# Re-review fixes: flag-rollback release + advance-QA after a synchronous cancel
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_release_still_works_after_flag_disabled(monkeypatch, cleanup_task_ids):
    """Disabling the flag while trials are armed must NOT strand them: the
    release path runs whenever something is BLOCKED, regardless of the flag."""
    monkeypatch.setattr(settings, "gate_llm_on_baselines", True)
    task_id = f"flag-rollback-{_RUN}"
    cleanup_task_ids.append(task_id)
    async with get_session() as session:
        await create_task(session, _mixed_submission("rollback"), task_id=task_id)
    assert (await _job_status_by_agent(task_id))[_LLM_AGENT] == WorkerJobStatus.BLOCKED

    # Operator rolls the flag back off while the kimi trial is still BLOCKED.
    monkeypatch.setattr(settings, "gate_llm_on_baselines", False)
    baseline_id = await _set_baseline_outcomes(
        task_id, oracle_reward=1.0, nop_reward=0.0
    )
    async with get_session() as session:
        assert await maybe_gate_llm_trials(session, baseline_id) is True
    assert (await _job_status_by_agent(task_id))[_LLM_AGENT] == WorkerJobStatus.QUEUED


@pytest.mark.asyncio
async def test_agent_only_append_faulty_advances_task(monkeypatch, cleanup_task_ids):
    """A gate-cancel on the append/retry path advances the task in the same
    request instead of leaving it stuck RUNNING/PENDING."""
    monkeypatch.setattr(settings, "gate_llm_on_baselines", True)
    task_id = f"pull-advance-{_RUN}"
    cleanup_task_ids.append(task_id)
    async with get_session() as session:
        await create_task(session, _baselines_only_submission(), task_id=task_id)
    await _set_baseline_outcomes(task_id, oracle_reward=0.0, nop_reward=0.0)  # faulty

    kimi = await _append_llm_only(task_id)  # gated -> cancelled, and task advanced
    assert await _wj_status(kimi) == WorkerJobStatus.CANCELLED
    async with get_session() as session:
        task = (
            await session.execute(select(TaskModel).where(TaskModel.id == task_id))
        ).scalar_one()
    assert task.status not in (TaskStatus.PENDING, TaskStatus.RUNNING)


# ---------------------------------------------------------------------------
# Per-run opt-out (--no-baseline-gate / gate_baselines=False)
# ---------------------------------------------------------------------------


def _no_gate(sub: TaskSubmission) -> TaskSubmission:
    """Copy a submission with the per-run baseline gate opted out."""
    return sub.model_copy(update={"gate_baselines": False})


@pytest.mark.asyncio
async def test_opt_out_create_never_blocks_llm(monkeypatch, cleanup_task_ids):
    """--no-baseline-gate on create: LLM trials run ungated (baselines still run)."""
    monkeypatch.setattr(settings, "gate_llm_on_baselines", True)
    task_id = f"optout-create-{_RUN}"
    cleanup_task_ids.append(task_id)
    async with get_session() as session:
        await create_task(
            session, _no_gate(_mixed_submission("optout")), task_id=task_id
        )
    statuses = await _job_status_by_agent(task_id)
    # Baselines still run; the LLM agent is QUEUED (not BLOCKED) despite the
    # global flag being on and baselines being present.
    assert statuses["oracle"] == WorkerJobStatus.QUEUED
    assert statuses["nop"] == WorkerJobStatus.QUEUED
    assert statuses[_LLM_AGENT] == WorkerJobStatus.QUEUED


@pytest.mark.asyncio
async def test_opt_out_append_not_blocked(monkeypatch, cleanup_task_ids):
    """--no-baseline-gate on an append: the new LLM trial isn't blocked."""
    monkeypatch.setattr(settings, "gate_llm_on_baselines", True)
    task_id = f"optout-append-{_RUN}"
    cleanup_task_ids.append(task_id)
    async with get_session() as session:
        await create_task(session, _baselines_only_submission(), task_id=task_id)
    # Baselines are still pending -> a gated append would BLOCK; opting out runs it.
    async with get_session() as session:
        task = (
            await session.execute(
                select(TaskModel)
                .options(selectinload(TaskModel.experiments))
                .where(TaskModel.id == task_id)
            )
        ).scalar_one()
        new = await append_trials_to_task(
            session, task=task, submission=_no_gate(_llm_only_submission())
        )
        kimi = new[0].id
    assert await _wj_status(kimi) == WorkerJobStatus.QUEUED


@pytest.mark.asyncio
async def test_retry_opt_out_not_regated(monkeypatch, cleanup_task_ids):
    """Retry with gate_baselines=False re-runs ungated even on a faulty task."""
    from oddish.core.endpoints.trials import retry_trial_core

    monkeypatch.setattr(settings, "gate_llm_on_baselines", True)
    task_id = f"optout-retry-{_RUN}"
    cleanup_task_ids.append(task_id)
    async with get_session() as session:
        await create_task(session, _mixed_submission("optout-retry"), task_id=task_id)
    # Faulty baselines -> the create-blocked kimi is cancelled.
    baseline_id = await _set_baseline_outcomes(
        task_id, oracle_reward=0.0, nop_reward=0.0
    )
    async with get_session() as session:
        await maybe_gate_llm_trials(session, baseline_id)
    async with get_session() as session:
        kimi_id = (
            await session.execute(
                select(TrialModel.id).where(
                    TrialModel.task_id == task_id, TrialModel.agent == _LLM_AGENT
                )
            )
        ).scalar_one()

    # A default retry would re-gate and cancel (see test_retry_of_gated_llm_trial_
    # reports_skipped); opting out on the retry runs it ungated instead.
    async with get_session() as session:
        result = await retry_trial_core(
            session, trial_id=kimi_id, org_id=None, gate_baselines=False
        )
    assert result["status"] == "queued"
    assert await _wj_status(result["trial_id"]) == WorkerJobStatus.QUEUED
