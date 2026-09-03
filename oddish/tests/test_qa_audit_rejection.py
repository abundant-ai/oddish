"""Audit output repair and task decisions, including reruns on the same version."""

import copy
import json
import os
import subprocess
import uuid
from datetime import timedelta
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from sqlalchemy import select, text

from oddish.core.analysis_payload import audit_fingerprint, audit_snapshot_matches
from oddish.core.endpoints.qa import cancel_task_qa_core, rerun_pre_trial_audit_core
from oddish.core.verdict_state import queue_verdict
from oddish.core.verdict_sync import apply_deterministic_verdict_rules
from oddish.db import (
    ExperimentModel,
    TaskModel,
    TaskStatus,
    TaskVersionModel,
    TrialModel,
    TrialStatus,
    VerdictStatus,
    get_session,
    init_db,
    utcnow,
)
from oddish.queue import maybe_start_task_qa_stage, start_qa_for_task
from oddish.worker.probe_staging import apply_analysis_overlay, stage_cli_mount
from oddish.workers import analysis_trials
from test_analysis_trials import _good_qa_entry


FINDING = {
    "source": "pre_trial",
    "problem_type": "incompleteness",
    "dimension": "verifier",
    "file": "tests/verify.py",
    "line_start": 4,
    "line_end": 6,
    "title": "The verifier ignores the exit code",
    "detail": "It never asserts returncode.",
    "recommendation": "Assert returncode == 0.",
    "tier": "must_fix",
}


@pytest.mark.parametrize("failure_after_repair", ["invalid", "missing", "limit"])
def test_audit_submission_keeps_attempts_and_never_publishes_invalid_output(
    tmp_path, failure_after_repair
):
    task = tmp_path / "task"
    task.mkdir()
    expected = analysis_trials.analysis_check_payload("audit", None)
    apply_analysis_overlay(
        task, brief="audit", artifact="audit_result.json", check_payload=expected
    )
    harness = tmp_path / "harness"
    stage_cli_mount(harness, analysis_task_dir=task)
    logs = tmp_path / "logs"
    draft = tmp_path / "draft.json"
    missing_source = {k: v for k, v in FINDING.items() if k != "source"}
    draft.write_text(json.dumps({"items": [missing_source]}))
    original = draft.read_bytes()
    env = {
        **os.environ,
        "ODDISH_ANALYSIS_LOG_DIR": str(logs),
        "ODDISH_ANALYSIS_ATTEMPTS_FILE": str(tmp_path / "attempts"),
    }

    def submit():
        return subprocess.run(
            [str(harness / "submit-analysis-result"), str(draft)],
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )

    invalid = submit()
    assert invalid.returncode == 1
    assert "source" in invalid.stderr
    assert not (logs / "audit_result.json").exists()
    history = logs / "audit_result.json.submissions"
    assert (history / "attempt-1.json").read_bytes() == original
    assert "source" in (history / "attempt-1.errors.txt").read_text()

    draft.write_text(json.dumps({"items": [FINDING]}))
    valid = submit()
    assert valid.returncode == 0, valid.stderr
    assert (logs / "audit_result.json").read_bytes() == draft.read_bytes()
    assert (history / "attempt-1.json").read_bytes() == original

    if failure_after_repair == "invalid":
        draft.write_bytes(original)
    elif failure_after_repair == "missing":
        draft.unlink()
    else:
        assert submit().returncode == 0  # Third permitted attempt.
    assert submit().returncode == 1
    assert not (logs / "audit_result.json").exists()


def test_fingerprint_pins_the_audit_but_not_later_exploitation_annotations():
    version = TaskVersionModel(
        id="v1",
        content_hash="source",
        pre_trial_status=VerdictStatus.SUCCESS,
        pre_trial_started_at=utcnow(),
        pre_trial_finished_at=utcnow(),
        pre_trial={"items": [{"id": "finding", **FINDING}]},
    )
    expected = {"audit_fingerprint": audit_fingerprint(version)}
    version.pre_trial["items"][0].update(exploited=True, exploit_evidence="step 12")
    assert audit_snapshot_matches(version, expected)
    version.pre_trial_status = VerdictStatus.QUEUED
    assert not audit_snapshot_matches(
        version, {"audit_fingerprint": audit_fingerprint(version)}
    )
    version.pre_trial_status = VerdictStatus.SUCCESS
    version.pre_trial_started_at += timedelta(seconds=1)
    assert not audit_snapshot_matches(version, expected)


@pytest.mark.parametrize(
    "must_fix,baselines,rejected",
    [
        ([], [], False),
        (["finding"], [], True),
        ([], [{"agent": "oracle", "reward": 0.0}], True),
        ([], [{"agent": "nop", "reward": 1.0}], True),
    ],
)
def test_decisive_evidence_does_not_require_a_model_verdict(
    must_fix, baselines, rejected
):
    verdict = apply_deterministic_verdict_rules(
        None, must_fix_ids=must_fix, baseline_evidence=baselines
    )
    assert (verdict is not None) == rejected
    if rejected:
        assert verdict.verdict == "reject"


@pytest_asyncio.fixture
async def audit_task(monkeypatch):
    if not os.environ.get("ODDISH_DATABASE_URL"):
        pytest.skip("ODDISH_DATABASE_URL not set")
    await init_db()
    task_id = "audit-rejection-" + uuid.uuid4().hex[:12]
    version_id = task_id + "-v1"
    async with get_session() as session:
        experiment = ExperimentModel(name=task_id)
        task = TaskModel(
            id=task_id,
            name=task_id,
            user="test",
            task_path="p",
            status=TaskStatus.RUNNING,
            run_analysis=True,
        )
        session.add_all([experiment, task])
        await session.flush()
        version = TaskVersionModel(
            id=version_id,
            task_id=task_id,
            version=1,
            task_path="p",
            content_hash="source",
            pre_trial_status=VerdictStatus.SUCCESS,
            pre_trial_started_at=utcnow(),
            pre_trial_finished_at=utcnow(),
            pre_trial={"items": [{"id": "finding", **FINDING}]},
        )
        session.add(version)
        await session.flush()
        task.current_version_id = version.id
        await session.execute(
            text(
                "INSERT INTO task_experiments (task_id, experiment_id, created_at) "
                "VALUES (:task, :experiment, NOW())"
            ),
            {"task": task.id, "experiment": experiment.id},
        )
        source = TrialModel(
            id=task_id + "-solver",
            name="solver",
            task_id=task_id,
            task_version_id=version_id,
            experiment_id=experiment.id,
            agent="claude-code",
            provider="local",
            queue_key="test",
            kind="agent",
            status=TrialStatus.SUCCESS,
            reward=0.0,
            has_trajectory=True,
        )
        session.add(source)
    # Only artifact transport and telemetry are replaced. Admission, import,
    # task/version locks, worker-job creation, and verdict writes use Postgres.
    artifacts = {}

    async def read_artifact(trial, filename):
        return copy.deepcopy(artifacts.get(trial.id))

    monkeypatch.setattr(analysis_trials, "read_analysis_artifact", read_artifact)
    monkeypatch.setattr(
        analysis_trials, "read_own_trajectory", AsyncMock(return_value=None)
    )
    monkeypatch.setattr(analysis_trials, "_fire_qa_imported", AsyncMock())
    monkeypatch.setattr(
        "oddish.core.trial_io.read_trial_trajectory", AsyncMock(return_value=None)
    )
    yield task_id, version_id, source.id, artifacts


async def create_qa(task_id):
    async with get_session() as session:
        task = await session.get(TaskModel, task_id, with_for_update=True)
        assert await start_qa_for_task(session, task)
        qa = await session.scalar(
            select(TrialModel)
            .where(TrialModel.task_id == task_id, TrialModel.kind == "qa")
            .order_by(TrialModel.created_at.desc(), TrialModel.id.desc())
            .limit(1)
        )
        assert qa.harbor_config["analysis_payload"]["with_verdict"] is False
        return qa.id


def qa_artifact(source_id, *, findings=True):
    entry = _good_qa_entry(source_id)
    entry["analysis"].update(classification="GOOD_FAILURE", subtype="misdiagnosis")
    if findings:
        entry["analysis"]["exploitation"] = [
            {
                "links_to": "finding",
                "exploited": False,
                "evidence": "Unrelated to this failure.",
            }
        ]
    return {"trials": [entry], "verdict": None}


async def settle(trial_id):
    async with get_session() as session:
        trial = await session.get(TrialModel, trial_id)
        trial.status = TrialStatus.SUCCESS
    await analysis_trials.handle_analysis_trial_settled(trial_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("tier", ["must_fix", "should_fix", None])
async def test_one_trial_rejection_preserves_trial_classification(audit_task, tier):
    task_id, version_id, source_id, artifacts = audit_task
    async with get_session() as session:
        version = await session.get(TaskVersionModel, version_id)
        version.pre_trial = (
            {"items": [{"id": "finding", **FINDING, "tier": tier}]}
            if tier
            else {"items": []}
        )
    qa_id = await create_qa(task_id)
    artifacts[qa_id] = qa_artifact(source_id, findings=tier is not None)
    await settle(qa_id)
    await analysis_trials.handle_analysis_trial_settled(qa_id)  # Duplicate delivery.
    async with get_session() as session:
        task = await session.get(TaskModel, task_id)
        source = await session.get(TrialModel, source_id)
        assert task.status == TaskStatus.COMPLETED
        assert task.verdict_status == VerdictStatus.SUCCESS
        assert source.analysis["classification"] == "GOOD_FAILURE"
        assert source.reward == 0.0
        assert source.trajectory_summary["_graded_by"] == qa_id
        if tier == "must_fix":
            assert task.verdict["verdict"] == "reject"
            assert task.verdict["is_good"] is False
        else:
            assert task.verdict is None


@pytest.mark.asyncio
@pytest.mark.parametrize("has_finding", [True, False])
async def test_zero_eligible_trials_wait_for_audit_then_finish(audit_task, has_finding):
    task_id, version_id, source_id, artifacts = audit_task
    async with get_session() as session:
        source = await session.get(TrialModel, source_id)
        source.status = TrialStatus.SKIPPED
        await rerun_pre_trial_audit_core(session, task_id=task_id)
    async with get_session() as session:
        await maybe_start_task_qa_stage(session, task_id)
        task = await session.get(TaskModel, task_id)
        assert task.status == TaskStatus.RUNNING
        assert task.verdict is None
        audit = await session.scalar(
            select(TrialModel).where(
                TrialModel.task_id == task_id, TrialModel.kind == "audit"
            )
        )
    artifacts[audit.id] = {"items": [FINDING] if has_finding else []}
    await settle(audit.id)
    async with get_session() as session:
        task = await session.get(TaskModel, task_id)
        assert task.status == TaskStatus.COMPLETED
        assert (task.verdict or {}).get("verdict") == (
            "reject" if has_finding else None
        )
        assert (
            await session.scalar(
                select(TrialModel.id).where(
                    TrialModel.task_id == task_id, TrialModel.kind == "qa"
                )
            )
            is None
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("audit_finishes_first", [True, False])
async def test_same_version_audit_rerun_discards_old_qa_and_creates_one_replacement(
    audit_task, audit_finishes_first
):
    task_id, version_id, source_id, artifacts = audit_task
    old_qa = await create_qa(task_id)
    artifacts[old_qa] = qa_artifact(source_id)
    async with get_session() as session:
        await rerun_pre_trial_audit_core(session, task_id=task_id)
    async with get_session() as session:
        audit = await session.scalar(
            select(TrialModel).where(
                TrialModel.task_id == task_id, TrialModel.kind == "audit"
            )
        )
        task = await session.get(TaskModel, task_id)
        assert task.verdict is None
    artifacts[audit.id] = {"items": []}
    first, last = (audit.id, old_qa) if audit_finishes_first else (old_qa, audit.id)
    await settle(first)
    async with get_session() as session:
        task = await session.get(TaskModel, task_id)
        assert task.status == TaskStatus.RUNNING
        assert task.verdict is None
    await settle(last)
    # Duplicate settlement cannot erase the new audit or create extra QA.
    await analysis_trials.handle_analysis_trial_settled(audit.id)
    await analysis_trials.handle_analysis_trial_settled(old_qa)
    async with get_session() as session:
        source = await session.get(TrialModel, source_id)
        assert source.analysis is None
        qas = list(
            (
                await session.scalars(
                    select(TrialModel).where(
                        TrialModel.task_id == task_id, TrialModel.kind == "qa"
                    )
                )
            ).all()
        )
        assert len(qas) == 2
        fresh = next(qa for qa in qas if qa.id != old_qa)
        assert fresh.harbor_config["analysis_payload"]["pre_trial_must_fix_ids"] == []
    artifacts[fresh.id] = qa_artifact(source_id, findings=False)
    await settle(fresh.id)
    async with get_session() as session:
        task = await session.get(TaskModel, task_id)
        assert task.status == TaskStatus.COMPLETED
        assert task.verdict is None
        assert (await session.get(TrialModel, source_id)).analysis[
            "_graded_by"
        ] == fresh.id


@pytest.mark.asyncio
@pytest.mark.parametrize("qa_already_settled", [True, False])
async def test_cancelled_audit_replacement_cannot_resurrect_qa(
    audit_task, qa_already_settled
):
    task_id, _, source_id, artifacts = audit_task
    qa_id = await create_qa(task_id)
    artifacts[qa_id] = qa_artifact(source_id)
    async with get_session() as session:
        await rerun_pre_trial_audit_core(session, task_id=task_id)
    async with get_session() as session:
        if qa_already_settled:
            (await session.get(TrialModel, qa_id)).status = TrialStatus.SUCCESS
        await cancel_task_qa_core(session, task_id=task_id)
    await analysis_trials.handle_analysis_trial_settled(qa_id)
    async with get_session() as session:
        await maybe_start_task_qa_stage(session, task_id)
        task = await session.get(TaskModel, task_id)
        assert task.status == TaskStatus.FAILED
        assert task.verdict is None
        assert task.verdict_status == VerdictStatus.FAILED
        assert (await session.get(TrialModel, source_id)).analysis is None


@pytest.mark.asyncio
async def test_cleanup_replaces_stale_audit_snapshot_instead_of_reimporting_forever(
    audit_task,
):
    from oddish.workers.queue.cleanup import _heal_stale_verdict_pending

    task_id, version_id, _, _ = audit_task
    old_qa = await create_qa(task_id)
    async with get_session() as session:
        (await session.get(TrialModel, old_qa)).status = TrialStatus.SUCCESS
        version = await session.get(TaskVersionModel, version_id)
        version.pre_trial = {"items": []}
        version.pre_trial_status = VerdictStatus.QUEUED
        task = await session.get(TaskModel, task_id)
        queue_verdict(task)
    async with get_session() as session:
        _, to_import = await _heal_stale_verdict_pending(session)
        assert old_qa not in to_import
        assert (
            await session.get(TaskModel, task_id)
        ).status == TaskStatus.VERDICT_PENDING
        assert (
            len(
                list(
                    await session.scalars(
                        select(TrialModel.id).where(
                            TrialModel.task_id == task_id, TrialModel.kind == "qa"
                        )
                    )
                )
            )
            == 1
        )
        (
            await session.get(TaskVersionModel, version_id)
        ).pre_trial_status = VerdictStatus.SUCCESS
    async with get_session() as session:
        _, to_import = await _heal_stale_verdict_pending(session)
        assert old_qa not in to_import
        qas = list(
            (
                await session.scalars(
                    select(TrialModel).where(
                        TrialModel.task_id == task_id, TrialModel.kind == "qa"
                    )
                )
            ).all()
        )
        assert len(qas) == 2
        assert sum(qa.status == TrialStatus.QUEUED for qa in qas) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("has_solver", [False, True])
@pytest.mark.parametrize(
    "import_status",
    [
        VerdictStatus.PENDING,
        VerdictStatus.QUEUED,
        VerdictStatus.RUNNING,
    ],
)
async def test_cleanup_waits_for_audit_import_before_completing_task(
    audit_task, has_solver, import_status
):
    from fastapi import HTTPException
    from oddish.core.endpoints.qa import backfill_task_analysis_core
    from oddish.workers.queue.cleanup import (
        _advance_running_tasks_to_analysis,
        _heal_stale_audit_imports,
    )

    task_id, version_id, source_id, artifacts = audit_task
    async with get_session() as session:
        if not has_solver:
            await session.delete(await session.get(TrialModel, source_id))
        await rerun_pre_trial_audit_core(session, task_id=task_id)
    async with get_session() as session:
        audit = await session.scalar(
            select(TrialModel).where(
                TrialModel.task_id == task_id, TrialModel.kind == "audit"
            )
        )
        audit.status = TrialStatus.SUCCESS
        (
            await session.get(TaskVersionModel, version_id)
        ).pre_trial_status = import_status
    artifacts[audit.id] = {"items": [FINDING]}

    # The job finished, but its findings have not been imported. Cleanup runs
    # admission before scheduling stale imports, matching the production order.
    async with get_session() as session:
        await _advance_running_tasks_to_analysis(session, [])
        task = await session.get(TaskModel, task_id)
        assert task.status == TaskStatus.RUNNING
        assert task.verdict is None
        assert audit.id in await _heal_stale_audit_imports(session)
        assert (
            await session.scalar(
                select(TrialModel.id).where(
                    TrialModel.task_id == task_id, TrialModel.kind == "qa"
                )
            )
            is None
        )
    if has_solver:
        async with get_session() as session:
            with pytest.raises(HTTPException, match="audit") as error:
                await backfill_task_analysis_core(session, task_id=task_id)
            assert error.value.status_code == 400

    await analysis_trials.handle_analysis_trial_settled(audit.id)
    if has_solver:
        async with get_session() as session:
            qa = await session.scalar(
                select(TrialModel).where(
                    TrialModel.task_id == task_id, TrialModel.kind == "qa"
                )
            )
            finding_id = qa.harbor_config["analysis_payload"]["pre_trial_must_fix_ids"][
                0
            ]
        artifacts[qa.id] = qa_artifact(source_id)
        artifacts[qa.id]["trials"][0]["analysis"]["exploitation"][0]["links_to"] = (
            finding_id
        )
        await settle(qa.id)
    async with get_session() as session:
        task = await session.get(TaskModel, task_id)
        assert task.status == TaskStatus.COMPLETED
        assert task.verdict["verdict"] == "reject"


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["source_bytes", "new_version", "active_solver"])
async def test_qa_cannot_publish_over_changed_source_or_active_solver(
    audit_task, change
):
    task_id, version_id, source_id, artifacts = audit_task
    qa_id = await create_qa(task_id)
    artifacts[qa_id] = qa_artifact(source_id)
    async with get_session() as session:
        version = await session.get(TaskVersionModel, version_id)
        task = await session.get(TaskModel, task_id)
        if change == "source_bytes":
            version.content_hash = "replacement bytes"
        elif change == "new_version":
            replacement = TaskVersionModel(
                id=version_id + "-new",
                task_id=task_id,
                version=2,
                task_path="p",
                content_hash="new bytes",
                pre_trial_status=VerdictStatus.SUCCESS,
                pre_trial={"items": []},
            )
            session.add(replacement)
            await session.flush()
            task.current_version_id = replacement.id
        else:
            (await session.get(TrialModel, source_id)).status = TrialStatus.RUNNING
    await settle(qa_id)
    async with get_session() as session:
        task = await session.get(TaskModel, task_id)
        assert task.verdict is None
        assert task.status == TaskStatus.VERDICT_PENDING
        assert (await session.get(TrialModel, source_id)).analysis is None


@pytest.mark.asyncio
async def test_failed_audit_replacement_does_not_restore_old_rejection(audit_task):
    task_id, _, source_id, artifacts = audit_task
    qa_id = await create_qa(task_id)
    artifacts[qa_id] = qa_artifact(source_id)
    await settle(qa_id)
    async with get_session() as session:
        assert (await session.get(TaskModel, task_id)).verdict["verdict"] == "reject"
        await rerun_pre_trial_audit_core(session, task_id=task_id)
    async with get_session() as session:
        audit = await session.scalar(
            select(TrialModel).where(
                TrialModel.task_id == task_id, TrialModel.kind == "audit"
            )
        )
        audit.status = TrialStatus.FAILED
    await analysis_trials.handle_analysis_trial_settled(audit.id)
    async with get_session() as session:
        task = await session.get(TaskModel, task_id)
        assert task.verdict is None
        fresh = await session.scalar(
            select(TrialModel).where(
                TrialModel.task_id == task_id,
                TrialModel.kind == "qa",
                TrialModel.id != qa_id,
            )
        )
        assert fresh.harbor_config["analysis_payload"]["with_verdict"] is False
        assert fresh.harbor_config["analysis_payload"]["pre_trial_must_fix_ids"] == []
    artifacts[fresh.id] = qa_artifact(source_id, findings=False)
    await settle(fresh.id)
    async with get_session() as session:
        assert (await session.get(TaskModel, task_id)).verdict is None


@pytest.mark.asyncio
async def test_cancel_between_audit_settlement_and_import_remains_cancelled(audit_task):
    task_id, version_id, _, artifacts = audit_task
    async with get_session() as session:
        await rerun_pre_trial_audit_core(session, task_id=task_id)
    async with get_session() as session:
        audit = await session.scalar(
            select(TrialModel).where(
                TrialModel.task_id == task_id, TrialModel.kind == "audit"
            )
        )
        audit.status = TrialStatus.SUCCESS
    artifacts[audit.id] = {"items": [FINDING]}
    async with get_session() as session:
        await cancel_task_qa_core(session, task_id=task_id)
    await analysis_trials.handle_analysis_trial_settled(audit.id)
    async with get_session() as session:
        version = await session.get(TaskVersionModel, version_id)
        task = await session.get(TaskModel, task_id)
        assert version.pre_trial_status == VerdictStatus.FAILED
        assert version.pre_trial is None
        assert task.status == TaskStatus.FAILED
        assert task.verdict is None
        assert (
            await session.scalar(
                select(TrialModel.id).where(
                    TrialModel.task_id == task_id, TrialModel.kind == "qa"
                )
            )
            is None
        )
