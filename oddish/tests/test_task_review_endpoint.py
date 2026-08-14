"""Contract tests for the bounded, read-only task review endpoint."""

from __future__ import annotations

import os
import uuid
from datetime import timedelta

import pytest
import pytest_asyncio
from fastapi import HTTPException

from oddish.analyze.models import ActionTier
from oddish.core.endpoints.task_review import get_task_review_core
from oddish.core.qa_scope import analysis_fingerprint
from oddish.db import (
    AnalysisStatus,
    ExperimentModel,
    TaskModel,
    TaskQaRunDisposition,
    TaskQaRunModel,
    TaskStatus,
    TaskVersionModel,
    TrialModel,
    TrialStatus,
    VerdictStatus,
    WorkerJobKind,
    WorkerJobModel,
    WorkerJobStatus,
    get_session,
    utcnow,
)

pytestmark = pytest.mark.skipif(
    not os.environ.get("ODDISH_DATABASE_URL"),
    reason="ODDISH_DATABASE_URL not set",
)


def _finding(
    finding_id: str,
    *,
    source: str = "pre_trial",
    tier: str = "must_fix",
    title: str = "Verifier trusts a caller-controlled marker",
    links_to: str | None = None,
    exploited: bool = False,
    evidence: str | None = None,
) -> dict:
    return {
        "id": finding_id,
        "source": source,
        "problem_type": "mismatch",
        "dimension": "verifier",
        "file": "scripts/check.sh",
        "line_start": 41,
        "line_end": 48,
        "title": title,
        "detail": f"Exact detail for {finding_id}.",
        "recommendation": f"Exact recommendation for {finding_id}.",
        "tier": tier,
        "links_to": links_to,
        "exploited": exploited,
        "exploit_evidence": evidence,
        "causal": exploited,
    }


def _analysis(
    classification: str,
    *,
    action_items: list[dict] | None = None,
    exploitation: list[dict] | None = None,
) -> dict:
    return {
        "classification": classification,
        "subtype": "Contract fixture",
        "evidence": f"evidence for {classification}",
        "root_cause": f"root cause for {classification}",
        "recommendation": "N/A",
        "action_items": action_items or [],
        "exploitation": exploitation or [],
    }


def _verdict(label: str = "accept") -> dict:
    return {
        "verdict": label,
        "is_good": label == "accept",
        "confidence": "high",
        "primary_issue": None if label == "accept" else "Task issue",
        "reasoning": "Stored version verdict.",
        "recommendations": [],
        "task_problem_count": 2,
        "agent_problem_count": 1,
        "success_count": 1,
        "harness_error_count": 0,
    }


def _trial(
    *,
    trial_id: str,
    task_id: str,
    version_id: str,
    experiment_id: str,
    agent: str,
    reward: float | None,
    analysis: dict | None = None,
    status: TrialStatus = TrialStatus.SUCCESS,
    is_probe: bool = False,
    imported_at=None,
) -> TrialModel:
    now = utcnow()
    return TrialModel(
        id=trial_id,
        name=trial_id,
        task_id=task_id,
        task_version_id=version_id,
        experiment_id=experiment_id,
        agent=agent,
        provider="openai" if agent == "codex" else "local",
        queue_key="openai/gpt-5.6" if agent == "codex" else "nop_oracle",
        model="openai/gpt-5.6" if agent == "codex" else "default",
        environment="docker",
        harbor_config={
            "resolved_sha": "15c40ac",
            "agent_config": {
                "env": {"OPENAI_API_KEY": "not-in-fingerprint", "SAFE": "kept"}
            },
        },
        harbor_sha="15c40ac",
        status=status,
        harbor_stage="completed",
        reward=reward,
        analysis=analysis,
        analysis_status=(AnalysisStatus.SUCCESS if analysis is not None else None),
        is_probe=is_probe,
        imported_at=imported_at,
        cost_usd=0.41 if agent == "codex" else 0,
        trajectory_duration_seconds=237.2,
        started_at=now - timedelta(minutes=4),
        finished_at=now,
        created_at=now,
    )


@pytest_asyncio.fixture
async def review_world():
    token = uuid.uuid4().hex[:10]
    task_id = f"review-{token}"
    task_name = f"log-rotation-{token}"
    org_id = f"org-{token}"
    exp_a = f"exp-a-{token}"
    exp_b = f"exp-b-{token}"
    v17 = f"{task_id}-v17"
    v18 = f"{task_id}-v18"
    pre_linked = _finding("pre-linked")
    pre_should = _finding(
        "pre-should",
        tier="should_fix",
        title="Instruction could state the retention window",
    )
    post_only = _finding(
        "post-only",
        source="post_trial",
        tier="optional",
        title="Add a diagnostic assertion",
    )
    model_1 = f"{task_id}-model-a"
    model_2 = f"{task_id}-model-b"
    model_3 = f"{task_id}-model-c"

    async with get_session() as session:
        session.add_all(
            [
                ExperimentModel(id=exp_a, name=f"exp-a-{token}", org_id=org_id),
                ExperimentModel(id=exp_b, name=f"exp-b-{token}", org_id=org_id),
            ]
        )
        task = TaskModel(
            id=task_id,
            name=task_name,
            org_id=org_id,
            user="review-test",
            status=TaskStatus.COMPLETED,
            task_path=f"s3://review/{task_id}",
            run_analysis=True,
        )
        session.add(task)
        await session.flush()
        session.add_all(
            [
                TaskVersionModel(
                    id=v17,
                    task_id=task_id,
                    version=17,
                    task_path=f"s3://review/{v17}",
                    content_hash="hash-v17",
                    pre_trial={"items": [pre_linked, pre_should]},
                    pre_trial_status=VerdictStatus.SUCCESS,
                ),
                TaskVersionModel(
                    id=v18,
                    task_id=task_id,
                    version=18,
                    task_path=f"s3://review/{v18}",
                    content_hash="hash-v18",
                    pre_trial={
                        "items": [
                            _finding(
                                "v18-only",
                                title="Only the newer version has this finding",
                            )
                        ]
                    },
                    pre_trial_status=VerdictStatus.SUCCESS,
                ),
            ]
        )
        await session.flush()
        task.current_version_id = v17

        trials = [
            _trial(
                trial_id=f"{task_id}-nop",
                task_id=task_id,
                version_id=v17,
                experiment_id=exp_a,
                agent="nop",
                reward=0,
            ),
            _trial(
                trial_id=f"{task_id}-oracle",
                task_id=task_id,
                version_id=v17,
                experiment_id=exp_a,
                agent="oracle",
                reward=1,
            ),
            _trial(
                trial_id=model_1,
                task_id=task_id,
                version_id=v17,
                experiment_id=exp_a,
                agent="codex",
                reward=0,
                analysis=_analysis(
                    "GOOD_FAILURE",
                    action_items=[
                        _finding(
                            "linked-post",
                            source="post_trial",
                            links_to="pre-linked",
                        )
                    ],
                    exploitation=[
                        {
                            "links_to": "pre-linked",
                            "exploited": True,
                            "exploit_evidence": "trajectory step 19",
                            "causal": True,
                        }
                    ],
                ),
            ),
            _trial(
                trial_id=model_2,
                task_id=task_id,
                version_id=v17,
                experiment_id=exp_b,
                agent="codex",
                reward=1,
                analysis=_analysis("BAD_SUCCESS", action_items=[post_only]),
            ),
            _trial(
                trial_id=model_3,
                task_id=task_id,
                version_id=v17,
                experiment_id=exp_b,
                agent="codex",
                reward=0,
                analysis=_analysis("BAD_FAILURE", action_items=[post_only]),
            ),
            _trial(
                trial_id=f"{task_id}-old-retry",
                task_id=task_id,
                version_id=v17,
                experiment_id=exp_b,
                agent="codex",
                reward=1,
                analysis=_analysis("BAD_SUCCESS"),
            ),
            _trial(
                trial_id=f"{task_id}-probe",
                task_id=task_id,
                version_id=v17,
                experiment_id=exp_a,
                agent="codex",
                reward=1,
                analysis=_analysis("BAD_SUCCESS"),
                is_probe=True,
            ),
            _trial(
                trial_id=f"{task_id}-skipped",
                task_id=task_id,
                version_id=v17,
                experiment_id=exp_a,
                agent="codex",
                reward=None,
                status=TrialStatus.SKIPPED,
            ),
            _trial(
                trial_id=f"{task_id}-imported",
                task_id=task_id,
                version_id=v17,
                experiment_id=exp_a,
                agent="codex",
                reward=1,
                analysis=_analysis("BAD_SUCCESS"),
                imported_at=utcnow(),
            ),
            _trial(
                trial_id=f"{task_id}-v18-model",
                task_id=task_id,
                version_id=v18,
                experiment_id=exp_a,
                agent="codex",
                reward=1,
                analysis=_analysis("GOOD_SUCCESS"),
            ),
        ]
        trials[5].superseded_by_trial_id = model_3
        session.add_all(trials)
        await session.flush()

        job_id = f"job-{token}"
        run_id = f"qa-{token}"
        job = WorkerJobModel(
            id=job_id,
            kind=WorkerJobKind.QA,
            status=WorkerJobStatus.SUCCESS,
            queue_key="qa",
            subject_table="tasks",
            subject_id=task_id,
            payload={"task_id": task_id, "task_version_id": v17, "qa_run_id": run_id},
            started_at=utcnow() - timedelta(minutes=5),
            finished_at=utcnow(),
        )
        session.add(job)
        await session.flush()
        fingerprints = {
            trial.id: analysis_fingerprint(trial.analysis)
            for trial in trials
            if trial.id in {model_1, model_2, model_3}
        }
        run = TaskQaRunModel(
            id=run_id,
            task_id=task_id,
            task_version_id=v17,
            worker_job_id=job_id,
            disposition=TaskQaRunDisposition.PUBLISHED,
            input_trial_ids=sorted(fingerprints),
            input_analysis_fingerprints=fingerprints,
            verdict=_verdict(),
            started_at=job.started_at,
            finished_at=job.finished_at,
            created_at=job.started_at,
        )
        session.add(run)
        await session.flush()
        task.verdict = run.verdict
        task.verdict_status = VerdictStatus.SUCCESS
        task.published_qa_run_id = run_id
        task.verdict_version_id = v17

    world = {
        "task_id": task_id,
        "task_name": task_name,
        "org_id": org_id,
        "experiments": [exp_a, exp_b],
        "exp_a": exp_a,
        "exp_b": exp_b,
        "v17": v17,
        "v18": v18,
        "run_id": run_id,
        "job_id": job_id,
        "models": [model_1, model_2, model_3],
    }
    yield world

    async with get_session() as session:
        await session.execute(
            TaskModel.__table__.delete().where(TaskModel.id == task_id)
        )
        await session.execute(
            WorkerJobModel.__table__.delete().where(
                WorkerJobModel.subject_id == task_id
            )
        )
        await session.execute(
            ExperimentModel.__table__.delete().where(
                ExperimentModel.id.in_([exp_a, exp_b])
            )
        )


async def _review(world, **overrides):
    async with get_session() as session:
        return await get_task_review_core(
            session,
            task_ref=world["task_id"],
            org_id=world["org_id"],
            **overrides,
        )


@pytest.mark.asyncio
async def test_review_resolves_id_name_org_and_selected_default(review_world):
    by_id = await _review(review_world)
    async with get_session() as session:
        by_name = await get_task_review_core(
            session,
            task_ref=review_world["task_name"],
            org_id=review_world["org_id"],
        )
        with pytest.raises(HTTPException) as cross_org:
            await get_task_review_core(
                session,
                task_ref=review_world["task_name"],
                org_id="another-org",
            )

    assert by_id.task == by_name.task
    assert by_id.task.version == 17
    assert by_id.task.version_id == review_world["v17"]
    assert cross_org.value.status_code == 404


@pytest.mark.asyncio
async def test_review_isolates_requested_version(review_world):
    response = await _review(review_world, version=18)

    assert response.task.version_id == review_world["v18"]
    assert [finding.id for finding in response.findings] == ["v18-only"]
    assert [trial.id for trial in response.trials] == [
        f"{review_world['task_id']}-v18-model"
    ]
    assert response.qa.result_run is None
    assert response.verdict is None


@pytest.mark.asyncio
async def test_review_merges_cross_experiment_findings_and_separates_baselines(
    review_world,
):
    response = await _review(review_world)

    assert response.baselines.outcome == "valid"
    assert response.baselines.nop.trial_count == 1
    assert response.baselines.oracle.trial_count == 1
    assert [trial.role for trial in response.trials[:2]] == ["nop", "oracle"]
    assert response.trial_counts.eligible == 3
    assert response.trial_counts.analyzed == 3
    assert response.trial_counts.classifications.GOOD_FAILURE == 1
    assert response.trial_counts.classifications.BAD_SUCCESS == 1
    assert response.trial_counts.classifications.BAD_FAILURE == 1

    linked = next(finding for finding in response.findings if finding.id == "pre-linked")
    post_only = next(finding for finding in response.findings if finding.id == "post-only")
    assert linked.from_pre_trial is True
    assert linked.title == "Verifier trusts a caller-controlled marker"
    assert linked.trial_ids == [review_world["models"][0]]
    assert linked.experiment_ids == [review_world["exp_a"]]
    assert linked.exploited is True
    assert linked.exploit_evidence == "trajectory step 19"
    assert post_only.from_pre_trial is False
    assert post_only.trial_ids == sorted(review_world["models"][1:])
    assert post_only.experiment_ids == [review_world["exp_b"]]


@pytest.mark.asyncio
async def test_experiment_filter_narrows_evidence_but_not_run_provenance(review_world):
    response = await _review(review_world, experiment_id=review_world["exp_a"])

    assert response.scope.same_version_across_experiments is False
    assert response.trial_counts.eligible == 1
    assert [trial.role for trial in response.trials] == ["nop", "oracle", "model"]
    assert response.qa.result_run.input_trial_count == 3
    assert response.verdict.verdict == "accept"
    assert "post-only" not in {finding.id for finding in response.findings}
    linked = next(finding for finding in response.findings if finding.id == "pre-linked")
    assert linked.trial_ids == [review_world["models"][0]]


@pytest.mark.asyncio
async def test_tier_filtering_precedes_stable_independent_pagination(review_world):
    first = await _review(
        review_world,
        tiers=[ActionTier.SHOULD_FIX, ActionTier.OPTIONAL],
        finding_limit=1,
        trial_limit=2,
    )
    second = await _review(
        review_world,
        tiers=[ActionTier.SHOULD_FIX, ActionTier.OPTIONAL],
        finding_limit=1,
        finding_cursor=first.findings_page.next_cursor,
        trial_limit=0,
    )
    trial_second = await _review(
        review_world,
        finding_limit=0,
        trial_limit=2,
        trial_cursor=first.trials_page.next_cursor,
    )

    assert first.finding_counts.unfiltered_total == 3
    assert first.finding_counts.filtered_total == 2
    assert first.finding_counts.must_fix == 1
    assert first.finding_counts.should_fix == 1
    assert first.finding_counts.optional == 1
    finding_ids = [first.findings[0].id, second.findings[0].id]
    assert finding_ids == ["pre-should", "post-only"]
    assert not set(trial.id for trial in first.trials) & set(
        trial.id for trial in trial_second.trials
    )


@pytest.mark.asyncio
async def test_analysis_fingerprint_mismatch_is_visible(review_world):
    async with get_session() as session:
        trial = await session.get(TrialModel, review_world["models"][0])
        trial.analysis = {**trial.analysis, "evidence": "changed after publication"}
        await session.flush()
        response = await get_task_review_core(
            session,
            task_ref=review_world["task_id"],
            org_id=review_world["org_id"],
        )

    assert response.qa.input_analysis_changed_after_run is True
    assert response.qa.result_run.input_analysis_changed_count == 1
    changed = next(
        trial for trial in response.trials if trial.id == review_world["models"][0]
    )
    assert changed.included_in_result_run is True
    assert changed.analysis_matches_result_run is False


async def _add_run(
    session,
    world,
    *,
    suffix: str,
    disposition: TaskQaRunDisposition | None,
    worker_status: WorkerJobStatus,
    minute: int,
):
    job_id = f"{world['job_id']}-{suffix}"
    run_id = f"{world['run_id']}-{suffix}"
    created_at = utcnow() + timedelta(minutes=minute)
    session.add(
        WorkerJobModel(
            id=job_id,
            kind=WorkerJobKind.QA,
            status=worker_status,
            queue_key="qa",
            subject_table="tasks",
            subject_id=world["task_id"],
            payload={
                "task_id": world["task_id"],
                "task_version_id": world["v17"],
                "qa_run_id": run_id,
            },
            created_at=created_at,
        )
    )
    await session.flush()
    session.add(
        TaskQaRunModel(
            id=run_id,
            task_id=world["task_id"],
            task_version_id=world["v17"],
            worker_job_id=job_id,
            disposition=disposition,
            input_trial_ids=[],
            input_analysis_fingerprints={},
            verdict=_verdict() if disposition == TaskQaRunDisposition.PUBLISHED else None,
            created_at=created_at,
            finished_at=(created_at if disposition is not None else None),
        )
    )
    await session.flush()
    return run_id


@pytest.mark.asyncio
async def test_cancelled_later_run_restores_result_and_active_run_retains_it(review_world):
    async with get_session() as session:
        await _add_run(
            session,
            review_world,
            suffix="cancelled",
            disposition=TaskQaRunDisposition.CANCELLED,
            worker_status=WorkerJobStatus.CANCELLED,
            minute=1,
        )
        active_id = await _add_run(
            session,
            review_world,
            suffix="active",
            disposition=None,
            worker_status=WorkerJobStatus.RUNNING,
            minute=2,
        )
        response = await get_task_review_core(
            session,
            task_ref=review_world["task_id"],
            org_id=review_world["org_id"],
        )

    assert response.qa.result_run.id == review_world["run_id"]
    assert response.qa.active_run.id == active_id
    assert response.qa.status == VerdictStatus.RUNNING


@pytest.mark.asyncio
async def test_failed_later_run_invalidates_prior_result(review_world):
    async with get_session() as session:
        await _add_run(
            session,
            review_world,
            suffix="failed",
            disposition=TaskQaRunDisposition.FAILED,
            worker_status=WorkerJobStatus.FAILED,
            minute=1,
        )
        response = await get_task_review_core(
            session,
            task_ref=review_world["task_id"],
            org_id=review_world["org_id"],
        )

    assert response.qa.result_run is None
    assert response.qa.status == VerdictStatus.FAILED
    assert response.verdict is None


@pytest.mark.asyncio
async def test_legacy_unscoped_verdict_is_warned_not_guessed(review_world):
    async with get_session() as session:
        task = await session.get(TaskModel, review_world["task_id"])
        run = await session.get(TaskQaRunModel, review_world["run_id"])
        task.published_qa_run_id = None
        task.verdict_version_id = None
        run.deleted_at = utcnow()
        await session.flush()
        response = await get_task_review_core(
            session,
            task_ref=review_world["task_id"],
            org_id=review_world["org_id"],
        )

    assert response.qa.legacy_unscoped_verdict_available is True
    assert response.qa.result_run is None
    assert response.verdict is None


@pytest.mark.asyncio
async def test_review_is_bounded_constant_query_and_read_only(review_world):
    async with get_session() as session:
        for index in range(100):
            session.add(
                _trial(
                    trial_id=f"{review_world['task_id']}-bulk-{index:03d}",
                    task_id=review_world["task_id"],
                    version_id=review_world["v17"],
                    experiment_id=review_world["exp_b"],
                    agent="codex",
                    reward=0,
                    analysis=_analysis("GOOD_FAILURE"),
                )
            )
        await session.flush()

        statements = []

        class RecordingSession:
            async def execute(self, statement, *args, **kwargs):
                statements.append(str(statement))
                return await session.execute(statement, *args, **kwargs)

        response = await get_task_review_core(
            RecordingSession(),
            task_ref=review_world["task_id"],
            org_id=review_world["org_id"],
        )

        assert len(session.new) == 0
        assert len(session.dirty) == 0
        assert len(session.deleted) == 0

    assert len(response.findings) <= 20
    assert len(response.trials) <= 20
    assert len(response.model_dump_json()) < 100_000
    assert len(statements) <= 7
    trial_sql = "\n".join(statement for statement in statements if "trials" in statement)
    assert "trials.result AS" not in trial_sql
    # The shared eligibility predicate may inspect the legacy gate-skip prefix,
    # but the response query must never select the wide error body.
    assert "trials.error_message AS" not in trial_sql
