from types import SimpleNamespace

from oddish.core.delivery_reviews import delivery_reviews
from oddish.db import VerdictStatus
from oddish.schemas import DeliveryQAStatus, DeliveryTaskBoardRow


def facts():
    task = SimpleNamespace(
        verdict=None,
        verdict_status=VerdictStatus.FAILED,
        verdict_error="No verdict produced",
    )
    version = SimpleNamespace(
        id="v2",
        pre_trial_status=VerdictStatus.SUCCESS,
        pre_trial_finished_at=None,
        pre_trial_error=None,
    )
    return task, version


def test_finished_execution_without_verdict_is_not_a_failed_execution():
    task, version = facts()
    result = delivery_reviews(
        task,
        version,
        DeliveryQAStatus(
            run_status="SUCCESS",
            status="error",
            detail="QA produced no current verdict",
        ),
        None,
    )
    assert result.pre_trial.status == "completed"
    assert result.post_trial.status == "completed"
    assert result.verdict.status == "unavailable"
    assert result.verdict.detail == "No verdict produced"


def test_failure_keeps_execution_error_separate_from_source_completion():
    task, version = facts()
    result = delivery_reviews(
        task,
        version,
        DeliveryQAStatus(
            run_status="FAILED", run_error="Provider timeout", trial_id="qa2"
        ),
        None,
    )
    assert result.pre_trial.status == "completed"
    assert result.post_trial.status == "failed"
    assert result.post_trial.detail == "Provider timeout"
    assert result.post_trial.trial_id == "qa2"


def test_old_accept_is_explicitly_outdated():
    task, version = facts()
    task.verdict_status = VerdictStatus.SUCCESS
    task.verdict = {"is_good": True, "reasoning": "Earlier review accepted"}
    result = delivery_reviews(
        task, version, DeliveryQAStatus(run_status="SUCCESS", status="outdated"), "v1"
    )
    assert result.verdict.status == "accept"
    assert result.verdict.outdated
    assert result.post_trial.outdated


def test_withdrawn_verdict_does_not_resurrect_old_accept():
    task, version = facts()
    task.verdict = {"is_good": True}
    result = delivery_reviews(
        task, version, DeliveryQAStatus(run_status="RUNNING", status="running"), "v2"
    )
    assert result.verdict.status == "unavailable"
    assert result.post_trial.status == "running"


def test_legacy_snapshots_do_not_invent_stage_results():
    assert DeliveryTaskBoardRow.model_fields["reviews"].default is None


def test_qa_evaluation_retains_run_failure_when_version_is_outdated():
    from oddish.core.delivery_qa import evaluate_delivery_qa
    from oddish.db import TrialStatus

    task, version = facts()
    run = SimpleNamespace(
        id="old-qa",
        task_version_id="v1",
        status=TrialStatus.FAILED,
        finished_at=None,
        error_message="Worker crashed",
        analysis_error=None,
    )
    qa = evaluate_delivery_qa(task=task, version=version, qa=run, sources=[])
    assert qa.status == "outdated"
    result = delivery_reviews(task, version, qa, None)
    assert result.post_trial.status == "failed"
    assert result.post_trial.outdated
    assert result.post_trial.detail == "Worker crashed"
