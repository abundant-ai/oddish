"""Direct backend enforcement of task findings and delivery exceptions."""

from copy import deepcopy

import pytest
from fastapi import HTTPException
from oddish.analyze.models import ActionItem, TaskVerdictModel
from oddish.core.deliveries import (
    create_delivery_core,
    finalize_delivery_core,
    get_delivery_board_core,
    get_task_qa_history_core,
    set_manual_check_core,
)
from oddish.core.task_findings import preserve_task_findings
from oddish.core.verdict_sync import apply_deterministic_verdict_rules
from oddish.db import DeliveryManualCheckModel, DeliverySnapshotModel, WorkerJobModel
from oddish.schemas import DeliveryCheckConfig, DeliveryCreate, ManualCheckSet
from oddish.worker.analysis_result_check import check_analysis_result
from oddish.workers.analysis_trials import analysis_check_payload
from sqlalchemy import func, select
from test_analysis_trials import _good_qa_entry
from test_deliveries import ORG, _checks, _green_task, _trial, _version
from test_qa_audit_rejection import FINDING


@pytest.mark.parametrize("kind", ["audit", "qa", "qa_eval"])
@pytest.mark.parametrize("tier", ["must_fix", "should_fix", "optional"])
def test_new_findings_require_must_fix_at_shared_validation_boundary(kind, tier):
    item = {**FINDING, "tier": tier}
    if kind == "audit":
        expected = analysis_check_payload(kind, None)
        artifact = {"items": [item]}
    else:
        expected = analysis_check_payload(
            kind,
            {
                "analysis_payload": {
                    "trial_ids": ["source"],
                    "with_verdict": False,
                }
            },
        )
        entry = _good_qa_entry("source")
        entry["analysis"].update(
            classification="GOOD_FAILURE",
            action_items=[
                {
                    **item,
                    "source": "post_trial",
                    "causal": False,
                }
            ],
        )
        artifact = {"trials": [entry], "verdict": None}
    errors = check_analysis_result(artifact, expected)
    assert bool(errors) == (tier != "must_fix"), errors
    if errors:
        assert any("tier must be one of ['must_fix']" in error for error in errors)
    # Parsing historical records does not rewrite their original severity.
    assert ActionItem.model_validate(item).tier.value == tier


def test_unrelated_defect_rejects_task_without_changing_fair_execution_failure():
    expected = analysis_check_payload(
        "qa",
        {
            "analysis_payload": {
                "trial_ids": ["source"],
                "with_verdict": False,
            }
        },
    )
    entry = _good_qa_entry("source")
    entry["analysis"].update(
        classification="GOOD_FAILURE",
        action_items=[
            {
                **FINDING,
                "source": "post_trial",
                "causal": False,
            }
        ],
    )
    artifact = {"trials": [entry], "verdict": None}
    assert check_analysis_result(artifact, expected) == []
    verdict = apply_deterministic_verdict_rules(
        TaskVerdictModel(verdict="accept", confidence="high"),
        must_fix_ids=[],
        baseline_evidence=[],
        task_defect_count=1,
    )
    assert not verdict.is_good
    assert entry["analysis"]["classification"] == "GOOD_FAILURE"
    entry["analysis"]["action_items"][0]["causal"] = True
    assert any("causal" in error for error in check_analysis_result(artifact, expected))


@pytest.mark.asyncio
@pytest.mark.parametrize("tier", ["must_fix", "should_fix", "optional"])
@pytest.mark.parametrize("source", ["pre_trial", "trial"])
async def test_v7_exception_retains_evidence_person_and_version(session, tier, source):
    task, version, experiment = await _green_task(
        session, "policy-Task-A", version_number=7
    )
    finding = {**FINDING, "id": "verifier-defect", "tier": tier}
    if source == "pre_trial":
        version.pre_trial = {"items": [finding], "block_id": "historical-audit"}
    else:
        finding["source"] = "post_trial"
        session.add(
            _trial(
                task,
                experiment,
                version.id,
                analysis={
                    "classification": "GOOD_FAILURE",
                    "action_items": [finding],
                    "_graded_by": "historical-execution-review",
                },
            )
        )
    await session.flush()
    before_jobs = await session.scalar(select(func.count()).select_from(WorkerJobModel))
    original_audit = deepcopy(version.pre_trial)
    original_verdict = deepcopy(task.verdict)
    delivery = await create_delivery_core(
        session,
        org_id=ORG,
        user_id="maya",
        data=DeliveryCreate(
            name="Acceptance demo",
            customer="acme",
            task_ids=[task.id],
            check_config=DeliveryCheckConfig(
                automated={"no_must_fix": {"enabled": False}}
            ),
        ),
    )
    board = await get_delivery_board_core(session, delivery_id=delivery.id, org_id=ORG)
    row = board.tasks[0]
    assert not board.ready
    assert _checks(board, task.id)["no_must_fix"].status == "fail"
    assert row.defects[0].recorded_tier == tier
    assert row.defects[0].finding == finding

    async def check(key, *, version_id=version.id):
        await set_manual_check_core(
            session,
            delivery_id=delivery.id,
            org_id=ORG,
            user_id="maya",
            data=ManualCheckSet(
                check_key=key,
                delivery_task_id=row.delivery_task_id,
                task_version_id=version_id,
                checked=True,
                note="Reviewed verifier evidence; accept this exception.",
            ),
        )

    for actor, reviewed_version in [("maya", None), (None, version.id)]:
        with pytest.raises(HTTPException) as missing_identity:
            await set_manual_check_core(
                session,
                delivery_id=delivery.id,
                org_id=ORG,
                user_id=actor,
                data=ManualCheckSet(
                    check_key="signoff",
                    delivery_task_id=row.delivery_task_id,
                    checked=True,
                    task_version_id=reviewed_version,
                ),
            )
        assert missing_identity.value.status_code == 422
    for key in ["signoff", "waive:no_must_fix"]:
        with pytest.raises(HTTPException):
            await check(key)
    with pytest.raises(HTTPException):
        await finalize_delivery_core(
            session, delivery_id=delivery.id, org_id=ORG, user_id="maya"
        )
    await check("ack:verifier-defect")
    await check("signoff")
    board = await get_delivery_board_core(session, delivery_id=delivery.id, org_id=ORG)
    assert board.ready
    assert board.tasks[0].defects[0].acknowledged_by_user_id == "maya"
    assert version.pre_trial == original_audit
    assert task.verdict == original_verdict
    history = await get_task_qa_history_core(session, task_id=task.id, org_id=ORG)
    assert history.versions[0].findings[0].tier == tier
    assert history.versions[0].must_fix == 1

    v8 = _version(
        task, 8, pre_trial=original_audit, pre_trial_status=version.pre_trial_status
    )
    session.add(v8)
    await session.flush()
    task.current_version_id = v8.id
    await session.flush()
    board = await get_delivery_board_core(session, delivery_id=delivery.id, org_id=ORG)
    assert not board.ready
    assert _checks(board, task.id)["signoff"].status == "fail"
    assert _checks(board, task.id)["verdict_ok"].status == "fail"
    assert all(not defect.acknowledged for defect in board.tasks[0].defects)
    with pytest.raises(HTTPException) as stale:
        await check("signoff")
    assert stale.value.status_code == 409
    if source == "pre_trial":
        await check("ack:verifier-defect", version_id=v8.id)
    decisions = list(
        (
            await session.scalars(
                select(DeliveryManualCheckModel).where(
                    DeliveryManualCheckModel.delivery_id == delivery.id,
                    DeliveryManualCheckModel.task_version_id == version.id,
                )
            )
        ).all()
    )
    assert {decision.check_key for decision in decisions} == {
        "ack:verifier-defect",
        "signoff",
    }
    assert all(decision.checked_by_user_id == "maya" for decision in decisions)
    history = await get_task_qa_history_core(session, task_id=task.id, org_id=ORG)
    v7_history = next(item for item in history.versions if item.version == 7)
    assert {item.check_key for item in v7_history.decisions} == {
        "ack:verifier-defect",
        "signoff",
    }
    assert all(item.checked_by_user_id == "maya" for item in v7_history.decisions)
    assert (
        await session.scalar(select(func.count()).select_from(WorkerJobModel))
        == before_jobs
    )


@pytest.mark.asyncio
async def test_review_replacement_and_superseding_preserve_reported_defects(session):
    task, version, experiment = await _green_task(session, "retained-findings")
    finding = {
        **FINDING,
        "id": "from-execution",
        "tier": "optional",
        "source": "post_trial",
    }
    trial = _trial(task, experiment, version.id, analysis={"action_items": [finding]})
    replacement = _trial(task, experiment, version.id)
    session.add_all([trial, replacement])
    await session.flush()
    trial.superseded_by_trial_id = replacement.id
    await preserve_task_findings(session, version.id)
    trial.analysis = None
    version.pre_trial = {"items": []}
    await session.flush()
    delivery = await create_delivery_core(
        session,
        org_id=ORG,
        user_id="maya",
        data=DeliveryCreate(
            name="Retained execution finding",
            customer="acme",
            task_ids=[task.id],
        ),
    )
    board = await get_delivery_board_core(session, delivery_id=delivery.id, org_id=ORG)
    assert not board.ready
    assert board.tasks[0].defects[0].finding == finding
    assert board.tasks[0].defects[0].reporting_trial_id == trial.id


@pytest.mark.asyncio
async def test_pre_policy_finalized_snapshot_is_not_recomputed(session):
    task, version, _ = await _green_task(session, "historical-snapshot")
    delivery = await create_delivery_core(
        session,
        org_id=ORG,
        user_id="maya",
        data=DeliveryCreate(
            name="Already finalized",
            customer="acme",
            task_ids=[task.id],
        ),
    )
    board = await get_delivery_board_core(session, delivery_id=delivery.id, org_id=ORG)
    legacy = board.model_dump(mode="json")
    legacy["ready"] = True
    legacy["tasks"][0]["ready"] = True
    legacy["delivery"]["status"] = "finalized"
    snapshot = DeliverySnapshotModel(
        delivery_id=delivery.id, snapshot={"board": legacy}, scope=[]
    )
    session.add(snapshot)
    from oddish.db import utcnow

    delivery.status = "finalized"
    delivery.finalized_at = utcnow()
    version.pre_trial = {"items": [{**FINDING, "tier": "should_fix"}]}
    await session.flush()
    stored = deepcopy(snapshot.snapshot)
    result = await get_delivery_board_core(session, delivery_id=delivery.id, org_id=ORG)
    assert result.frozen and result.ready
    assert result.tasks[0].defects == []
    await session.refresh(snapshot)
    assert snapshot.snapshot == stored


@pytest.mark.asyncio
async def test_migration_preserves_artifacts_snapshots_and_queue(session):
    """Execute the actual migration against an isolated pre-policy schema."""
    import importlib.util
    from pathlib import Path

    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import text

    await session.execute(text("CREATE SCHEMA defect_policy_migration_test"))
    await session.execute(text("SET LOCAL search_path TO defect_policy_migration_test"))
    await session.execute(
        text("CREATE TABLE task_versions (id text PRIMARY KEY, pre_trial jsonb)")
    )
    await session.execute(
        text(
            "CREATE TABLE delivery_manual_checks (delivery_id text, delivery_task_id text, check_key text, task_version_id text)"
        )
    )
    await session.execute(
        text(
            "CREATE UNIQUE INDEX uq_delivery_manual_checks_task ON delivery_manual_checks (delivery_id, delivery_task_id, check_key) WHERE delivery_task_id IS NOT NULL"
        )
    )
    await session.execute(text("CREATE TABLE delivery_snapshots (snapshot jsonb)"))
    await session.execute(text("CREATE TABLE worker_jobs (id text)"))
    await session.execute(
        text(
            'INSERT INTO task_versions VALUES (\'v7\', \'{"items":[{"tier":"should_fix","title":"Verifier defect"}]}\')'
        )
    )
    await session.execute(
        text(
            'INSERT INTO delivery_snapshots VALUES (\'{"ready": true,"original":"frozen"}\')'
        )
    )
    await session.execute(
        text(
            "INSERT INTO delivery_manual_checks VALUES ('delivery','task','ack:finding','v7')"
        )
    )
    original = await session.scalar(text("SELECT pre_trial::text FROM task_versions"))
    snapshot = await session.scalar(
        text("SELECT snapshot::text FROM delivery_snapshots")
    )
    spec = importlib.util.spec_from_file_location(
        "task_defects_migration",
        Path(__file__).parents[1] / "alembic/versions/task_defects_001.py",
    )
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)

    def upgrade(connection):
        with Operations.context(MigrationContext.configure(connection)):
            migration.upgrade()

    await (await session.connection()).run_sync(upgrade)
    assert (
        await session.scalar(text("SELECT pre_trial::text FROM task_versions"))
        == original
    )
    assert (
        await session.scalar(text("SELECT snapshot::text FROM delivery_snapshots"))
        == snapshot
    )
    assert await session.scalar(text("SELECT count(*) FROM worker_jobs")) == 0
    await session.execute(
        text(
            "INSERT INTO delivery_manual_checks VALUES ('delivery','task','ack:finding','v8')"
        )
    )
    assert (
        await session.scalar(text("SELECT count(*) FROM delivery_manual_checks")) == 2
    )
