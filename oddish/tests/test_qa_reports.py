from __future__ import annotations

import json
import uuid

import pytest
from fastapi import HTTPException
from sqlalchemy import select, update

from oddish.core.qa_reports import (
    _item_response,
    _public_item,
    _verdict_is_experiment_scoped,
    create_qa_report_core,
    generate_qa_public_token,
    get_qa_report_core,
    get_public_qa_report_core,
    get_public_qa_token_for_experiment,
    patch_qa_report_core,
    preview_qa_report_core,
    publish_qa_report_core,
    sync_qa_report_core,
    unpublish_qa_report_core,
)
from oddish.core.endpoints.deletion import unlink_task_from_experiment_core
from oddish.core.sharing.public import _public_task_payload, _public_trial_payload
from oddish.db import (
    ExperimentModel,
    QAReportItemModel,
    QAReportModel,
    QAReportPublicationModel,
    QAReportTaskModel,
    TaskModel,
    TaskVersionModel,
    TrialModel,
    TrialStatus,
    VerdictStatus,
    get_soft_delete_models,
    task_experiments,
    utcnow,
)
from oddish.schemas import (
    QAReportItemPatch,
    QAReportPatchRequest,
    TaskStatusResponse,
    TrialResponse,
)


def _slug(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


def _trial(
    *,
    trial_id: str,
    task: TaskModel,
    version: TaskVersionModel,
    experiment: ExperimentModel,
    kind: str = "agent",
    is_probe: bool = False,
    analysis: dict | None = None,
) -> TrialModel:
    return TrialModel(
        id=trial_id,
        name=trial_id,
        task_id=task.id,
        task_version_id=version.id,
        experiment_id=experiment.id,
        org_id=task.org_id,
        agent="codex",
        provider="openai",
        queue_key="openai/gpt-5",
        model="gpt-5",
        kind=kind,
        is_probe=is_probe,
        status=TrialStatus.SUCCESS,
        analysis=analysis,
        analysis_status=VerdictStatus.SUCCESS if analysis else None,
        analysis_finished_at=utcnow() if analysis else None,
        finished_at=utcnow(),
    )


def test_qa_public_token_is_a_256_bit_urlsafe_secret() -> None:
    token = generate_qa_public_token()
    assert len(token) == 43
    assert token.replace("-", "").replace("_", "").isalnum()


def test_qa_report_schema_guards_cross_report_and_source_type() -> None:
    constraints = {row.name: row for row in QAReportItemModel.__table__.constraints}
    assert "fk_qa_report_items_task_report" in constraints
    assert "ck_qa_report_items_source_type" in constraints
    source_check = str(constraints["ck_qa_report_items_source_type"].sqltext)
    assert "pre_trial" in source_check
    assert "verdict" in source_check
    assert "trial_analysis" in source_check

    task_constraints = {row.name for row in QAReportTaskModel.__table__.constraints}
    assert "uq_qa_report_tasks_id_report" in task_constraints
    registered = set(get_soft_delete_models())
    assert {QAReportModel, QAReportTaskModel, QAReportItemModel} <= registered
    assert QAReportPublicationModel not in registered
    assert QAReportPublicationModel.__table__.c.published_at.server_default is not None
    assert not QAReportPublicationModel.__table__.c.scope_task_ids.nullable


def test_private_evidence_stays_editable_when_public_evidence_is_off() -> None:
    row = QAReportItemModel(
        id="item",
        report_id="report",
        report_task_id="section",
        source_type="pre_trial",
        source_ref="pre_trial:v1:action:a",
        source_label="Private run 4128",
        source_title="Source title",
        source_evidence="Source evidence",
        title="Customer title",
        evidence="Edited evidence",
        file="private/tests/verify.py",
        line_start=10,
        line_end=12,
        include_evidence=False,
        is_visible=True,
        sort_order=0,
    )

    assert _item_response(row).evidence == "Edited evidence"
    public = _public_item(row).model_dump()
    assert set(public) == {
        "source_type",
        "title",
        "summary",
        "recommendation",
        "customer_note",
        "evidence",
        "tier",
        "dimension",
        "file",
        "line_start",
        "line_end",
        "outcome",
        "confidence",
    }
    assert public["evidence"] is None
    assert public["file"] is None
    assert public["line_start"] is None
    assert public["line_end"] is None
    assert "source_ref" not in public
    assert "source_label" not in public


def test_task_verdict_never_crosses_an_experiment_trial_boundary() -> None:
    grader = TrialModel(
        id="qa-grader",
        harbor_config={
            "analysis_payload": {
                "trial_ids": ["trial-in-view"],
                "with_verdict": True,
            }
        },
    )
    assert _verdict_is_experiment_scoped(grader, {"trial-in-view"})

    grader.harbor_config = {
        "analysis_payload": {
            "trial_ids": ["trial-in-view", "trial-in-private-experiment"],
            "with_verdict": True,
        }
    }
    assert not _verdict_is_experiment_scoped(grader, {"trial-in-view"})


def test_public_experiment_payload_omits_uncurated_qa_and_errors() -> None:
    trial = TrialResponse.model_construct(
        id="trial",
        name="trial",
        task_id="task",
        task_path="task",
        analysis={"secret": "HIDDEN-ANALYSIS"},
        analysis_error="HIDDEN-ERROR",
        pre_trial_findings=[{"secret": "HIDDEN-PRE"}],
        error_message="HIDDEN-TRIAL-ERROR",
        qa_cost_usd=99.0,
        jobs=[],
        result={"safe": True},
    )
    task = TaskStatusResponse.model_construct(
        id="task",
        name="Task",
        verdict={"secret": "HIDDEN-VERDICT"},
        verdict_error="HIDDEN-VERDICT-ERROR",
        jobs=[],
        trials=[trial],
    )

    trial_payload = _public_trial_payload(trial)
    task_payload = _public_task_payload(task)
    wire = json.dumps([trial_payload, task_payload], sort_keys=True)
    for secret in (
        "HIDDEN-ANALYSIS",
        "HIDDEN-ERROR",
        "HIDDEN-PRE",
        "HIDDEN-TRIAL-ERROR",
        "HIDDEN-VERDICT",
        "HIDDEN-VERDICT-ERROR",
    ):
        assert secret not in wire
    assert trial_payload["result"] == {"safe": True}


@pytest.mark.parametrize(
    "payload",
    [
        {"expected_draft_version": 1, "title": None},
        {
            "expected_draft_version": 1,
            "tasks": [{"id": "task", "name": None}],
        },
        {
            "expected_draft_version": 1,
            "items": [{"id": "item", "title": None}],
        },
    ],
)
def test_patch_rejects_null_required_copy(payload: dict) -> None:
    with pytest.raises(ValueError):
        QAReportPatchRequest.model_validate(payload)


@pytest.mark.asyncio
async def test_report_source_scope_sync_snapshot_and_token_lifecycle(session) -> None:
    org_id = _slug("org")
    parent = ExperimentModel(
        name="Customer experiment",
        org_id=org_id,
        is_public=True,
        public_token=_slug("experiment-token"),
    )
    other = ExperimentModel(name="Other experiment", org_id=org_id)
    session.add_all([parent, other])
    await session.flush()
    # Task-wide QA can be stored under another experiment's hidden shadow.
    # Exact grader coverage still lets this experiment use its own trial QA.
    shadow = ExperimentModel(name="Hidden QA", org_id=org_id, shadow_of=other.id)
    session.add(shadow)
    await session.flush()

    task = TaskModel(
        name="Task A",
        org_id=org_id,
        user="tester",
        task_path="tasks/a",
    )
    session.add(task)
    await session.flush()
    v1 = TaskVersionModel(
        id=f"{task.id}-v1",
        task_id=task.id,
        version=1,
        task_path=task.task_path,
        pre_trial={
            "items": [
                {
                    "id": "pre-1",
                    "source": "pre_trial",
                    "problem_type": "incompleteness",
                    "dimension": "verifier",
                    "file": "tests/verify.py",
                    "line_start": 10,
                    "line_end": 12,
                    "title": "Verifier ignores stderr",
                    "detail": "Only stdout is checked.",
                    "recommendation": "Check stderr too.",
                    "tier": "must_fix",
                }
            ]
        },
        pre_trial_status=VerdictStatus.SUCCESS,
        pre_trial_finished_at=utcnow(),
    )
    v2 = TaskVersionModel(
        id=f"{task.id}-v2",
        task_id=task.id,
        version=2,
        task_path=task.task_path,
    )
    session.add_all([v1, v2])
    await session.flush()
    task.current_version_id = v2.id
    await session.execute(
        task_experiments.insert().values(task_id=task.id, experiment_id=parent.id)
    )

    grader = _trial(
        trial_id=_slug("qa"),
        task=task,
        version=v1,
        experiment=shadow,
        kind="qa",
    )
    session.add(grader)
    await session.flush()
    analysis = {
        "_graded_by": grader.id,
        "classification": "BAD_SUCCESS",
        "subtype": "Verifier bug",
        "evidence": "The invalid result passed.",
        "root_cause": "The verifier checks stdout only.",
        "recommendation": "Check stderr too.",
        "action_items": [],
    }
    live = _trial(
        trial_id=_slug("agent"),
        task=task,
        version=v1,
        experiment=parent,
        analysis=analysis,
    )
    uncovered = _trial(
        trial_id=_slug("uncovered"),
        task=task,
        version=v1,
        experiment=parent,
        analysis={**analysis, "subtype": "UNCOVERED-SECRET"},
    )
    probe = _trial(
        trial_id=_slug("probe"),
        task=task,
        version=v2,
        experiment=parent,
        is_probe=True,
        analysis={**analysis, "subtype": "PROBE-SECRET"},
    )
    superseded = _trial(
        trial_id=_slug("old"),
        task=task,
        version=v1,
        experiment=parent,
        analysis={**analysis, "subtype": "SUPERSEDED-SECRET"},
    )
    foreign = _trial(
        trial_id=_slug("foreign"),
        task=task,
        version=v2,
        experiment=other,
        analysis={**analysis, "subtype": "FOREIGN-SECRET"},
    )
    session.add_all([live, uncovered, probe, superseded, foreign])
    await session.flush()
    grader.harbor_config = {
        "analysis_payload": {"trial_ids": [live.id], "with_verdict": True}
    }
    superseded.superseded_by_trial_id = live.id
    task.verdict = {
        "_graded_by": grader.id,
        "verdict": "reject",
        "confidence": "high",
        "primary_issue": "Weak verifier",
        "reasoning": "The verifier misses an invalid result.",
        "recommendations": ["Check stderr too."],
    }
    task.verdict_status = VerdictStatus.SUCCESS
    task.verdict_finished_at = utcnow()
    await session.flush()

    created = await create_qa_report_core(
        session,
        experiment_id=parent.id,
        org_id=org_id,
        created_by_user_id="user",
    )
    assert created.title == "QA"
    items = [item for section in created.tasks for item in section.items]
    assert {item.source_type for item in items} == {
        "pre_trial",
        "verdict",
        "trial_analysis",
    }
    trial_item = next(item for item in items if item.source_type == "trial_analysis")
    assert grader.id in trial_item.source_ref
    source_wire = json.dumps([item.model_dump(mode="json") for item in items])
    assert "PROBE-SECRET" not in source_wire
    assert "SUPERSEDED-SECRET" not in source_wire
    assert "FOREIGN-SECRET" not in source_wire
    assert "UNCOVERED-SECRET" not in source_wire

    with pytest.raises(HTTPException) as empty_publish:
        await publish_qa_report_core(
            session,
            experiment_id=parent.id,
            org_id=org_id,
            published_by_user_id="user",
            expected_draft_version=created.draft_version,
            expected_public_token=None,
        )
    assert empty_publish.value.status_code == 409
    assert "at least one QA check" in str(empty_publish.value.detail)

    first = items[0]
    await patch_qa_report_core(
        session,
        experiment_id=parent.id,
        org_id=org_id,
        payload=QAReportPatchRequest(
            expected_draft_version=created.draft_version,
            internal_note="INTERNAL-SECRET",
            items=[
                QAReportItemPatch(
                    id=first.id,
                    title="Customer-safe title",
                    evidence="CURATED-EVIDENCE",
                    is_visible=True,
                    include_evidence=False,
                )
            ],
        ),
    )
    with pytest.raises(HTTPException) as stale_patch:
        await patch_qa_report_core(
            session,
            experiment_id=parent.id,
            org_id=org_id,
            payload=QAReportPatchRequest(
                expected_draft_version=created.draft_version,
                summary="stale",
            ),
        )
    assert stale_patch.value.status_code == 409

    new_trial = _trial(
        trial_id=_slug("new-agent"),
        task=task,
        version=v1,
        experiment=parent,
        analysis={**analysis, "subtype": "New finished QA"},
    )
    rerun_grader = _trial(
        trial_id=_slug("qa-rerun"),
        task=task,
        version=v1,
        experiment=shadow,
        kind="qa",
    )
    session.add_all([new_trial, rerun_grader])
    await session.flush()
    grader.harbor_config = {
        "analysis_payload": {
            "trial_ids": [live.id, new_trial.id],
            "with_verdict": True,
        }
    }
    rerun_grader.harbor_config = {
        "analysis_payload": {"trial_ids": [live.id], "with_verdict": True}
    }
    live.analysis = {
        **analysis,
        "_graded_by": rerun_grader.id,
        "subtype": "Rerun of the same trial",
    }
    live.analysis_finished_at = utcnow()
    await session.flush()
    before_sync = await create_qa_report_core(
        session,
        experiment_id=parent.id,
        org_id=org_id,
        created_by_user_id="user",
    )
    assert before_sync.new_item_count == 2
    synced = await sync_qa_report_core(session, experiment_id=parent.id, org_id=org_id)
    assert synced.new_item_count == 0
    assert (
        next(
            item
            for section in synced.tasks
            for item in section.items
            if item.id == first.id
        ).title
        == "Customer-safe title"
    )
    version_after_sync = synced.draft_version
    synced_again = await sync_qa_report_core(
        session, experiment_id=parent.id, org_id=org_id
    )
    assert synced_again.draft_version == version_after_sync

    preview = await preview_qa_report_core(
        session, experiment_id=parent.id, org_id=org_id
    )
    assert set(preview.model_dump()) == {
        "title",
        "summary",
        "conclusion",
        "customer_note",
        "published_at",
        "experiment",
        "tasks",
    }
    preview_wire = json.dumps(preview.model_dump(mode="json"), sort_keys=True)
    assert preview.tasks
    assert all(section.items for section in preview.tasks)
    assert "Customer-safe title" in preview_wire
    assert "INTERNAL-SECRET" not in preview_wire
    assert "CURATED-EVIDENCE" not in preview_wire
    assert "source_ref" not in preview_wire
    assert "source_label" not in preview_wire

    parent.is_public = False
    await session.flush()
    with pytest.raises(HTTPException) as private_experiment_publish:
        await publish_qa_report_core(
            session,
            experiment_id=parent.id,
            org_id=org_id,
            published_by_user_id="user",
            expected_draft_version=version_after_sync,
            expected_public_token=None,
        )
    assert private_experiment_publish.value.status_code == 409
    parent.is_public = True
    await session.flush()

    with pytest.raises(HTTPException) as stale_publish:
        await publish_qa_report_core(
            session,
            experiment_id=parent.id,
            org_id=org_id,
            published_by_user_id="user",
            expected_draft_version=version_after_sync - 1,
            expected_public_token=None,
        )
    assert stale_publish.value.status_code == 409
    published = await publish_qa_report_core(
        session,
        experiment_id=parent.id,
        org_id=org_id,
        published_by_user_id="user",
        expected_draft_version=version_after_sync,
        expected_public_token=None,
    )
    assert published.public_token
    old_token = published.public_token
    assert (
        await get_public_qa_token_for_experiment(session, experiment_id=parent.id)
        == old_token
    )
    public = await get_public_qa_report_core(
        session,
        experiment_token=parent.public_token or "",
        qa_token=old_token,
    )
    assert public is not None
    assert public.tasks
    assert all(section.items for section in public.tasks)
    assert (
        await get_public_qa_report_core(
            session,
            experiment_token="wrong-experiment-token",
            qa_token=old_token,
        )
        is None
    )

    draft_after_publish = await patch_qa_report_core(
        session,
        experiment_id=parent.id,
        org_id=org_id,
        payload=QAReportPatchRequest(
            expected_draft_version=published.draft_version,
            conclusion="A newer private conclusion",
        ),
    )
    unchanged = await get_public_qa_report_core(
        session,
        experiment_token=parent.public_token or "",
        qa_token=old_token,
    )
    assert unchanged is not None
    assert unchanged.conclusion != "A newer private conclusion"

    with pytest.raises(HTTPException) as stale_unpublish:
        await unpublish_qa_report_core(
            session,
            experiment_id=parent.id,
            org_id=org_id,
            expected_draft_version=published.draft_version,
            expected_public_token=old_token,
        )
    assert stale_unpublish.value.status_code == 409
    with pytest.raises(HTTPException) as stale_public_link:
        await unpublish_qa_report_core(
            session,
            experiment_id=parent.id,
            org_id=org_id,
            expected_draft_version=draft_after_publish.draft_version,
            expected_public_token="x" * 43,
        )
    assert stale_public_link.value.status_code == 409
    await unpublish_qa_report_core(
        session,
        experiment_id=parent.id,
        org_id=org_id,
        expected_draft_version=draft_after_publish.draft_version,
        expected_public_token=old_token,
    )
    assert (
        await get_public_qa_report_core(
            session,
            experiment_token=parent.public_token or "",
            qa_token=old_token,
        )
        is None
    )
    with pytest.raises(HTTPException) as stale_republish:
        await publish_qa_report_core(
            session,
            experiment_id=parent.id,
            org_id=org_id,
            published_by_user_id="user",
            expected_draft_version=draft_after_publish.draft_version,
            expected_public_token=old_token,
        )
    assert stale_republish.value.status_code == 409
    republished = await publish_qa_report_core(
        session,
        experiment_id=parent.id,
        org_id=org_id,
        published_by_user_id="user",
        expected_draft_version=draft_after_publish.draft_version,
        expected_public_token=None,
    )
    assert republished.public_token
    assert republished.public_token != old_token

    publications = list(
        (
            await session.scalars(
                select(QAReportPublicationModel).where(
                    QAReportPublicationModel.report_id == republished.id
                )
            )
        ).all()
    )
    assert len(publications) == 2
    assert publications[-1].scope_task_ids == [task.id]

    parent.deleted_at = utcnow()
    await session.flush()
    assert (
        await get_public_qa_token_for_experiment(session, experiment_id=parent.id)
        is None
    )
    assert (
        await get_public_qa_report_core(
            session,
            experiment_token=parent.public_token or "",
            qa_token=republished.public_token,
        )
        is None
    )

    parent.deleted_at = None
    await session.flush()
    await unlink_task_from_experiment_core(
        session,
        task_id=task.id,
        experiment_id=parent.id,
        org_id=org_id,
    )
    await session.flush()
    stale_scope = await get_qa_report_core(
        session,
        experiment_id=parent.id,
        org_id=org_id,
    )
    assert stale_scope.scope_stale
    assert stale_scope.has_unpublished_changes
    assert (
        await get_public_qa_token_for_experiment(session, experiment_id=parent.id)
        is None
    )
    with pytest.raises(HTTPException) as stale_scope_publish:
        await publish_qa_report_core(
            session,
            experiment_id=parent.id,
            org_id=org_id,
            published_by_user_id="user",
            expected_draft_version=stale_scope.draft_version,
            expected_public_token=stale_scope.public_token,
        )
    assert stale_scope_publish.value.status_code == 409
    assert (
        await get_public_qa_report_core(
            session,
            experiment_token=parent.public_token or "",
            qa_token=republished.public_token,
        )
        is None
    )

    refreshed = await sync_qa_report_core(
        session,
        experiment_id=parent.id,
        org_id=org_id,
    )
    assert not refreshed.scope_stale
    assert all(not section.is_visible for section in refreshed.tasks)

    await session.execute(
        update(task_experiments)
        .where(
            task_experiments.c.task_id == task.id,
            task_experiments.c.experiment_id == parent.id,
        )
        .values(deleted_at=None)
    )
    await session.flush()
    assert (
        await get_public_qa_report_core(
            session,
            experiment_token=parent.public_token or "",
            qa_token=republished.public_token,
        )
        is None
    )
    assert (
        await get_public_qa_token_for_experiment(session, experiment_id=parent.id)
        is None
    )
