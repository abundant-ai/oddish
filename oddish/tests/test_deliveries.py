"""Delivery checklist core: board computation, tick reset, finalize."""

import pytest
from fastapi import HTTPException

from oddish.core.deliveries import (
    add_delivery_tasks_core,
    create_delivery_core,
    finalize_delivery_core,
    get_delivery_board_core,
    get_task_qa_history_core,
    list_deliveries_core,
    patch_delivery_core,
    set_manual_check_core,
)
from oddish.db import (
    DeliverySnapshotModel,
    ExperimentModel,
    TaskModel,
    TaskVersionModel,
    TrialModel,
    TrialStatus,
    VerdictStatus,
    generate_id,
)
from oddish.schemas import (
    DeliveryCheckConfig,
    DeliveryCreate,
    DeliveryPatch,
    DeliveryTasksAdd,
    ManualCheckDefinition,
    ManualCheckSet,
)

ORG = "org-deliv"


def _task(name: str) -> TaskModel:
    return TaskModel(
        name=name, org_id=ORG, user="tester", task_path=f"s3://tasks/{name}"
    )


def _version(task: TaskModel, n: int, **kw) -> TaskVersionModel:
    return TaskVersionModel(
        id=f"{task.id}-v{n}",
        task_id=task.id,
        version=n,
        task_path=f"s3://t/{task.id}/v{n}",
        **kw,
    )


def _trial(
    task: TaskModel,
    experiment: ExperimentModel,
    version_id: str,
    *,
    agent: str = "codex",
    kind: str = "agent",
    status: TrialStatus = TrialStatus.SUCCESS,
    analysis: dict | None = None,
) -> TrialModel:
    trial_id = generate_id()
    return TrialModel(
        id=trial_id,
        name=trial_id,
        task_id=task.id,
        task_version_id=version_id,
        experiment_id=experiment.id,
        org_id=ORG,
        agent=agent,
        provider="openai",
        queue_key="openai/gpt-5.5",
        model="gpt-5.5",
        kind=kind,
        status=status,
        analysis=analysis,
        finished_at=None,
    )


async def _green_task(session, name: str):
    """A task whose current version passes every default automated check."""
    experiment = ExperimentModel(name=f"exp-{name}", org_id=ORG)
    task = _task(name)
    session.add_all([experiment, task])
    await session.flush()
    version = _version(
        task,
        1,
        pre_trial_status=VerdictStatus.SUCCESS,
        pre_trial={"items": []},
    )
    session.add(version)
    await session.flush()
    task.current_version_id = version.id
    for i in range(5):
        session.add(
            _trial(task, experiment, version.id, agent=f"agent-{i % 3}")
        )
    session.add(_trial(task, experiment, version.id, kind="qa"))
    task.verdict = {"is_good": True, "verdict": "accept"}
    task.verdict_status = VerdictStatus.SUCCESS
    await session.flush()
    return task, version, experiment


def _checks(board, task_id):
    row = next(r for r in board.tasks if r.task_id == task_id)
    return {c.key: c for c in row.checks}


async def _sign_off(session, delivery_id, task_id, user="signer"):
    """Acknowledge every open defect, then sign the task off."""
    board = await get_delivery_board_core(
        session, delivery_id=delivery_id, org_id=ORG
    )
    row = next(r for r in board.tasks if r.task_id == task_id)
    for defect in row.defects:
        if not defect.acknowledged:
            await set_manual_check_core(
                session,
                delivery_id=delivery_id,
                org_id=ORG,
                data=ManualCheckSet(
                    check_key=f"ack:{defect.id}",
                    delivery_task_id=row.delivery_task_id,
                    checked=True,
                ),
                user_id=user,
            )
    await set_manual_check_core(
        session,
        delivery_id=delivery_id,
        org_id=ORG,
        data=ManualCheckSet(
            check_key="signoff",
            delivery_task_id=row.delivery_task_id,
            checked=True,
        ),
        user_id=user,
    )


@pytest.mark.asyncio
async def test_green_task_board_is_ready(session):
    task, _, _ = await _green_task(session, "deliv-green")
    delivery = await create_delivery_core(
        session,
        data=DeliveryCreate(customer="acme", name="batch-1", task_ids=[task.id]),
        org_id=ORG,
        user_id="u1",
    )
    board = await get_delivery_board_core(
        session, delivery_id=delivery.id, org_id=ORG
    )
    checks = _checks(board, task.id)
    automated = {k: c for k, c in checks.items() if c.kind == "automated"}
    assert all(c.status == "pass" for c in automated.values()), {
        k: (c.status, c.detail) for k, c in automated.items()
    }
    # Every task needs a person's sign-off before the board is ready.
    assert checks["signoff"].status == "fail"
    assert not board.ready

    await _sign_off(session, delivery.id, task.id, user="u9")
    board = await get_delivery_board_core(
        session, delivery_id=delivery.id, org_id=ORG
    )
    assert board.ready and board.ready_task_count == 1
    assert _checks(board, task.id)["signoff"].checked_by_user_id == "u9"


@pytest.mark.asyncio
async def test_version_bump_resets_board(session):
    task, _, _ = await _green_task(session, "deliv-bump")
    delivery = await create_delivery_core(
        session,
        data=DeliveryCreate(customer="acme", name="batch-2", task_ids=[task.id]),
        org_id=ORG,
        user_id="u1",
    )
    await _sign_off(session, delivery.id, task.id)
    v2 = _version(task, 2)
    session.add(v2)
    await session.flush()
    task.current_version_id = v2.id
    await session.flush()

    board = await get_delivery_board_core(
        session, delivery_id=delivery.id, org_id=ORG
    )
    checks = _checks(board, task.id)
    assert checks["pre_trial_passed"].status == "fail"
    assert checks["min_rollouts"].status == "fail"
    assert checks["verdict_ok"].status == "fail"
    assert "does not cover" in checks["verdict_ok"].detail
    assert checks["signoff"].status == "fail"
    assert not board.ready


@pytest.mark.asyncio
async def test_must_fix_defects_block(session):
    task, version, experiment = await _green_task(session, "deliv-mustfix")
    version.pre_trial = {
        "items": [
            {"tier": "must_fix", "title": "leak"},
            {"tier": "should_fix", "title": "nit"},
        ]
    }
    cheat_trial = _trial(
        task,
        experiment,
        version.id,
        analysis={"action_items": [{"tier": "must_fix", "title": "cheat"}]},
    )
    session.add(cheat_trial)
    # Malformed analyses (object / scalar action_items) must not crash the
    # board query or count as defects.
    session.add(
        _trial(task, experiment, version.id, analysis={"action_items": {"o": 1}})
    )
    session.add(
        _trial(task, experiment, version.id, analysis={"action_items": "nope"})
    )
    await session.flush()
    delivery = await create_delivery_core(
        session,
        data=DeliveryCreate(customer="acme", name="batch-3", task_ids=[task.id]),
        org_id=ORG,
        user_id="u1",
    )
    board = await get_delivery_board_core(
        session, delivery_id=delivery.id, org_id=ORG
    )
    check = _checks(board, task.id)["no_must_fix"]
    assert check.status == "fail"
    assert "2 of 2 must-fix unacknowledged" in check.detail
    row = next(r for r in board.tasks if r.task_id == task.id)
    assert len(row.defects) == 2 and not any(d.acknowledged for d in row.defects)

    # Deleting the trial that reported a defect must not clear it: only an
    # acknowledgement or a new version does.
    from oddish.db import utcnow

    cheat_trial.deleted_at = utcnow()
    await session.flush()
    board = await get_delivery_board_core(
        session, delivery_id=delivery.id, org_id=ORG
    )
    row = next(r for r in board.tasks if r.task_id == task.id)
    assert len(row.defects) == 2


@pytest.mark.asyncio
async def test_same_title_defects_stay_distinct(session):
    # Numeric ids and the source are part of a defect's identity: three
    # distinct must-fix items that share a title must yield three ack ids,
    # not collapse into one acknowledgement.
    task, version, experiment = await _green_task(session, "deliv-collide")
    version.pre_trial = {
        "items": [
            {"id": 1, "tier": "must_fix", "title": "flaky test"},
            {"id": 2, "tier": "must_fix", "title": "flaky test"},
        ]
    }
    session.add(
        _trial(
            task,
            experiment,
            version.id,
            analysis={
                "action_items": [{"tier": "must_fix", "title": "flaky test"}]
            },
        )
    )
    await session.flush()
    delivery = await create_delivery_core(
        session,
        data=DeliveryCreate(customer="acme", name="batch-c", task_ids=[task.id]),
        org_id=ORG,
        user_id="u1",
    )
    board = await get_delivery_board_core(
        session, delivery_id=delivery.id, org_id=ORG
    )
    row = next(r for r in board.tasks if r.task_id == task.id)
    assert len(row.defects) == 3
    assert len({d.id for d in row.defects}) == 3
    assert {d.source for d in row.defects} == {"pre_trial", "trial"}
    assert "3 of 3 must-fix unacknowledged" in (
        _checks(board, task.id)["no_must_fix"].detail
    )


@pytest.mark.asyncio
async def test_manual_tick_and_version_reset(session):
    task, _, _ = await _green_task(session, "deliv-manual")
    config = DeliveryCheckConfig(
        manual=[
            ManualCheckDefinition(key="proofread", label="Proofread", scope="task"),
            ManualCheckDefinition(
                key="scope_ok", label="Scope confirmed", scope="delivery"
            ),
        ]
    )
    delivery = await create_delivery_core(
        session,
        data=DeliveryCreate(customer="acme", name="batch-4", check_config=config, task_ids=[task.id]),
        org_id=ORG,
        user_id="u1",
    )
    board = await get_delivery_board_core(
        session, delivery_id=delivery.id, org_id=ORG
    )
    assert not board.ready
    member_id = board.tasks[0].delivery_task_id

    await set_manual_check_core(
        session,
        delivery_id=delivery.id,
        org_id=ORG,
        data=ManualCheckSet(
            check_key="proofread", delivery_task_id=member_id, checked=True
        ),
        user_id="u2",
    )
    await set_manual_check_core(
        session,
        delivery_id=delivery.id,
        org_id=ORG,
        data=ManualCheckSet(check_key="scope_ok", checked=True),
        user_id="u2",
    )
    await _sign_off(session, delivery.id, task.id)
    board = await get_delivery_board_core(
        session, delivery_id=delivery.id, org_id=ORG
    )
    assert board.ready
    assert _checks(board, task.id)["proofread"].checked_by_user_id == "u2"

    # A new default version stales the task-scoped tick, not the
    # delivery-scoped one.
    v2 = _version(task, 2)
    session.add(v2)
    await session.flush()
    task.current_version_id = v2.id
    await session.flush()
    board = await get_delivery_board_core(
        session, delivery_id=delivery.id, org_id=ORG
    )
    assert _checks(board, task.id)["proofread"].status == "fail"
    assert "older version" in _checks(board, task.id)["proofread"].detail
    assert board.delivery_checks[0].status == "pass"


@pytest.mark.asyncio
async def test_finalize_gates_pins_and_freezes(session):
    task, version, _ = await _green_task(session, "deliv-final")
    delivery = await create_delivery_core(
        session,
        data=DeliveryCreate(customer="acme", name="batch-5"),
        org_id=ORG,
        user_id="u1",
    )
    with pytest.raises(HTTPException) as err:
        await finalize_delivery_core(
            session, delivery_id=delivery.id, org_id=ORG, user_id="u1"
        )
    assert err.value.status_code == 409  # empty delivery is never ready

    await add_delivery_tasks_core(
        session,
        delivery_id=delivery.id,
        org_id=ORG,
        data=DeliveryTasksAdd(task_ids=[task.id]),
    )
    await _sign_off(session, delivery.id, task.id)
    board = await finalize_delivery_core(
        session, delivery_id=delivery.id, org_id=ORG, user_id="u1"
    )
    assert board.frozen and delivery.status == "finalized"

    # Versions are pinned and a snapshot row exists with the scope.
    from sqlalchemy import select

    snapshot = await session.scalar(
        select(DeliverySnapshotModel).where(
            DeliverySnapshotModel.delivery_id == delivery.id
        )
    )
    assert snapshot is not None
    assert snapshot.scope == [
        {"task_id": task.id, "task_version_id": version.id}
    ]
    assert snapshot.snapshot["public"]["tasks"][0]["internal_note"] is None

    # Finalized deliveries are read-only, and the board serves the snapshot.
    with pytest.raises(HTTPException) as err:
        await add_delivery_tasks_core(
            session,
            delivery_id=delivery.id,
            org_id=ORG,
            data=DeliveryTasksAdd(task_ids=[task.id]),
        )
    assert err.value.status_code == 409

    v2 = _version(task, 2)
    session.add(v2)
    await session.flush()
    task.current_version_id = v2.id
    await session.flush()
    board = await get_delivery_board_core(
        session, delivery_id=delivery.id, org_id=ORG
    )
    assert board.frozen and board.ready  # v2 does not disturb the record
    # The frozen board carries the pinned version, not null.
    assert board.tasks[0].pinned_version_id == version.id


@pytest.mark.asyncio
async def test_org_scoping_and_unknown_check_key(session):
    task, _, _ = await _green_task(session, "deliv-scope")
    delivery = await create_delivery_core(
        session,
        data=DeliveryCreate(customer="acme", name="batch-6", task_ids=[task.id]),
        org_id=ORG,
        user_id="u1",
    )
    with pytest.raises(HTTPException) as err:
        await get_delivery_board_core(
            session, delivery_id=delivery.id, org_id="other-org"
        )
    assert err.value.status_code == 404

    with pytest.raises(HTTPException) as err:
        await patch_delivery_core(
            session,
            delivery_id=delivery.id,
            org_id=ORG,
            data=DeliveryPatch(
                check_config=DeliveryCheckConfig(automated={"nope": {}})
            ),
        )
    assert err.value.status_code == 422

    listed = await list_deliveries_core(session, org_id=ORG)
    ours = next(d for d in listed if d.id == delivery.id)
    assert ours.task_count == 1
    assert delivery.id not in {
        d.id for d in await list_deliveries_core(session, org_id="other-org")
    }


@pytest.mark.asyncio
async def test_qa_history(session):
    task, v1, experiment = await _green_task(session, "deliv-history")
    v2 = _version(
        task,
        2,
        pre_trial_status=VerdictStatus.SUCCESS,
        pre_trial={"items": [{"tier": "must_fix", "title": "x"}]},
        message="fix the verifier",
    )
    session.add(v2)
    await session.flush()
    task.current_version_id = v2.id
    session.add(_trial(task, experiment, v2.id, kind="audit"))
    # History must count must-fix from trial analyses too — the same
    # source the board blocks on.
    session.add(
        _trial(
            task,
            experiment,
            v2.id,
            analysis={"action_items": [{"tier": "must_fix", "title": "cheat"}]},
        )
    )
    # A malformed pre-trial items shape must not crash history, and a
    # failed audit must expose why it failed.
    v3 = _version(
        task,
        3,
        pre_trial={"items": "garbage"},
        pre_trial_status=VerdictStatus.FAILED,
        pre_trial_error="docker died",
    )
    session.add(v3)
    await session.flush()
    failed_audit = _trial(
        task, experiment, v3.id, kind="audit", status=TrialStatus.FAILED
    )
    failed_audit.error_message = "container OOM"
    session.add(failed_audit)
    # A legacy QA run with no version id must still appear in the history,
    # apart from the versions, not vanish into a bucket nobody reads.
    session.add(_trial(task, experiment, None, kind="qa"))
    await session.flush()

    history = await get_task_qa_history_core(session, task_id=task.id, org_id=ORG)
    assert [run.kind for run in history.unversioned_runs] == ["qa"]
    assert [v.version for v in history.versions] == [3, 2, 1]
    broken, latest, first = history.versions
    assert broken.must_fix == 0 and broken.pre_trial_should_fix == 0
    assert broken.findings == []
    assert broken.pre_trial_error == "docker died"
    assert [run.error for run in broken.qa_runs] == ["container OOM"]
    assert latest.is_current and latest.must_fix == 2
    # The findings behind the counts, for inline display: the pre-trial
    # item and the trial-analysis item, with their sources.
    assert {(f.tier, f.title, f.source) for f in latest.findings} == {
        ("must_fix", "x", "pre_trial"),
        ("must_fix", "cheat", "trial"),
    }
    # The verdict covers the version the newest verdict-producing QA run
    # graded: v1.
    assert history.verdict_version_id == v1.id
    assert latest.message == "fix the verifier"
    assert [run.kind for run in latest.qa_runs] == ["audit"]
    assert first.rollout_count == 5 and first.rollout_agents == 3
    assert [run.kind for run in first.qa_runs] == ["qa"]
    assert history.verdict == {"is_good": True, "verdict": "accept"}

    # The task name works too, like every other delivery entry point.
    by_name = await get_task_qa_history_core(
        session, task_id=task.name, org_id=ORG
    )
    assert by_name.task_id == task.id

    with pytest.raises(HTTPException):
        await get_task_qa_history_core(
            session, task_id=task.id, org_id="other-org"
        )


@pytest.mark.asyncio
async def test_deleted_task_blocks_readiness(session):
    task, _, _ = await _green_task(session, "deliv-deleted")
    delivery = await create_delivery_core(
        session,
        data=DeliveryCreate(customer="acme", name="batch-8", task_ids=[task.id]),
        org_id=ORG,
        user_id="u1",
    )
    await _sign_off(session, delivery.id, task.id)
    board = await get_delivery_board_core(
        session, delivery_id=delivery.id, org_id=ORG
    )
    assert board.ready

    # Soft-deleting a member task must surface as a failing row, not
    # silently shrink the board.
    from oddish.db import utcnow

    task.deleted_at = utcnow()
    await session.flush()
    board = await get_delivery_board_core(
        session, delivery_id=delivery.id, org_id=ORG
    )
    assert board.task_count == 1 and not board.ready
    row = board.tasks[0]
    assert row.checks[0].key == "task_exists"
    assert "deleted" in row.checks[0].detail

    with pytest.raises(HTTPException) as err:
        await finalize_delivery_core(
            session, delivery_id=delivery.id, org_id=ORG, user_id="u1"
        )
    assert err.value.status_code == 409

    # The remediation path still works: the dead member can be removed.
    from oddish.core.deliveries import remove_delivery_task_core

    await remove_delivery_task_core(
        session, delivery_id=delivery.id, org_id=ORG, task_id=task.id
    )
    board = await get_delivery_board_core(
        session, delivery_id=delivery.id, org_id=ORG
    )
    assert board.task_count == 0


@pytest.mark.asyncio
async def test_sort_order_advances_from_zero(session):
    a, _, _ = await _green_task(session, "deliv-order-a")
    b, _, _ = await _green_task(session, "deliv-order-b")
    delivery = await create_delivery_core(
        session,
        data=DeliveryCreate(customer="acme", name="batch-9", task_ids=[a.id]),
        org_id=ORG,
        user_id="u1",
    )
    await add_delivery_tasks_core(
        session,
        delivery_id=delivery.id,
        org_id=ORG,
        data=DeliveryTasksAdd(task_ids=[b.id]),
    )
    board = await get_delivery_board_core(
        session, delivery_id=delivery.id, org_id=ORG
    )
    assert [(r.task_id, r.sort_order) for r in board.tasks] == [
        (a.id, 0),
        (b.id, 1),
    ]


@pytest.mark.asyncio
async def test_finalized_delivery_cannot_be_deleted(session):
    task, _, _ = await _green_task(session, "deliv-nodelete")
    delivery = await create_delivery_core(
        session,
        data=DeliveryCreate(customer="acme", name="batch-10", task_ids=[task.id]),
        org_id=ORG,
        user_id="u1",
    )
    await _sign_off(session, delivery.id, task.id)
    await finalize_delivery_core(
        session, delivery_id=delivery.id, org_id=ORG, user_id="u1"
    )
    from oddish.core.deliveries import delete_delivery_core

    with pytest.raises(HTTPException) as err:
        await delete_delivery_core(session, delivery_id=delivery.id, org_id=ORG)
    assert err.value.status_code == 409


@pytest.mark.asyncio
async def test_verdict_freshness_follows_newest_qa_run(session):
    from datetime import datetime, timezone

    task, v1, experiment = await _green_task(session, "deliv-qa-order")
    # Timestamp the fixture's QA run too: recency must come from the
    # trials' own timestamps, not insertion order.
    from sqlalchemy import select

    fixture_qa = await session.scalar(
        select(TrialModel).where(
            TrialModel.task_id == task.id, TrialModel.kind == "qa"
        )
    )
    fixture_qa.finished_at = datetime(2026, 7, 1, tzinfo=timezone.utc)
    # An OLDER successful QA on another version does not disturb a verdict
    # produced by the newest run, which graded the current version.
    qa_current = _trial(task, experiment, v1.id, kind="qa")
    qa_current.finished_at = datetime(2026, 8, 15, tzinfo=timezone.utc)
    v2 = _version(task, 2)  # a non-default sibling version
    session.add_all([qa_current, v2])
    await session.flush()
    qa_old = _trial(task, experiment, v2.id, kind="qa")
    qa_old.finished_at = datetime(2026, 8, 1, tzinfo=timezone.utc)
    session.add(qa_old)
    await session.flush()

    delivery = await create_delivery_core(
        session,
        data=DeliveryCreate(customer="acme", name="batch-11", task_ids=[task.id]),
        org_id=ORG,
        user_id="u1",
    )
    board = await get_delivery_board_core(
        session, delivery_id=delivery.id, org_id=ORG
    )
    assert _checks(board, task.id)["verdict_ok"].status == "pass"

    # A NEWER successful QA on another version overwrote tasks.verdict, so
    # the stored verdict no longer covers the current default.
    qa_newer = _trial(task, experiment, v2.id, kind="qa")
    qa_newer.finished_at = datetime(2026, 8, 20, tzinfo=timezone.utc)
    session.add(qa_newer)
    await session.flush()
    board = await get_delivery_board_core(
        session, delivery_id=delivery.id, org_id=ORG
    )
    check = _checks(board, task.id)["verdict_ok"]
    assert check.status == "fail"
    assert "does not cover" in check.detail


@pytest.mark.asyncio
async def test_add_tasks_by_name(session):
    task, _, _ = await _green_task(session, "deliv-by-name")
    delivery = await create_delivery_core(
        session,
        data=DeliveryCreate(customer="acme", name="batch-12", task_ids=["deliv-by-name"]),
        org_id=ORG,
        user_id="u1",
    )
    board = await get_delivery_board_core(
        session, delivery_id=delivery.id, org_id=ORG
    )
    assert board.tasks[0].task_id == task.id

    # Names in another org must not resolve, and unknown refs still 404.
    with pytest.raises(HTTPException) as err:
        await add_delivery_tasks_core(
            session,
            delivery_id=delivery.id,
            org_id=ORG,
            data=DeliveryTasksAdd(task_ids=["no-such-task"]),
        )
    assert err.value.status_code == 404

    # Removal accepts a name too.
    from oddish.core.deliveries import remove_delivery_task_core

    await remove_delivery_task_core(
        session, delivery_id=delivery.id, org_id=ORG, task_id="deliv-by-name"
    )
    board = await get_delivery_board_core(
        session, delivery_id=delivery.id, org_id=ORG
    )
    assert board.task_count == 0


@pytest.mark.asyncio
async def test_retrying_a_trial_keeps_its_must_fix_findings(session):
    task, version, experiment = await _green_task(session, "deliv-retry")
    flagged = _trial(
        task,
        experiment,
        version.id,
        analysis={"action_items": [{"tier": "must_fix", "title": "leak"}]},
    )
    replacement = _trial(task, experiment, version.id)
    session.add_all([flagged, replacement])
    await session.flush()
    flagged.superseded_by_trial_id = replacement.id
    await session.flush()

    delivery = await create_delivery_core(
        session,
        data=DeliveryCreate(customer="acme", name="batch-13", task_ids=[task.id]),
        org_id=ORG,
        user_id="u1",
    )
    board = await get_delivery_board_core(
        session, delivery_id=delivery.id, org_id=ORG
    )
    check = _checks(board, task.id)["no_must_fix"]
    assert check.status == "fail"
    assert "1 of 1 must-fix unacknowledged" in check.detail


@pytest.mark.asyncio
async def test_signoff_requires_defect_acknowledgement(session):
    task, version, _ = await _green_task(session, "deliv-ack")
    version.pre_trial = {
        "items": [{"id": "def-1", "tier": "must_fix", "title": "leaky check"}]
    }
    await session.flush()
    delivery = await create_delivery_core(
        session,
        data=DeliveryCreate(customer="acme", name="batch-14", task_ids=[task.id]),
        org_id=ORG,
        user_id="u1",
    )
    board = await get_delivery_board_core(
        session, delivery_id=delivery.id, org_id=ORG
    )
    row = board.tasks[0]
    assert [d.id for d in row.defects] == ["def-1"]

    # Sign-off is refused while the defect has no acknowledgement.
    with pytest.raises(HTTPException) as err:
        await set_manual_check_core(
            session,
            delivery_id=delivery.id,
            org_id=ORG,
            data=ManualCheckSet(
                check_key="signoff",
                delivery_task_id=row.delivery_task_id,
                checked=True,
            ),
            user_id="u5",
        )
    assert err.value.status_code == 409
    assert "def-1" in err.value.detail

    # An acknowledgement must name a real defect.
    with pytest.raises(HTTPException) as err:
        await set_manual_check_core(
            session,
            delivery_id=delivery.id,
            org_id=ORG,
            data=ManualCheckSet(
                check_key="ack:not-a-defect",
                delivery_task_id=row.delivery_task_id,
                checked=True,
            ),
            user_id="u5",
        )
    assert err.value.status_code == 404

    # Acknowledge, then sign off. Both record the person.
    await set_manual_check_core(
        session,
        delivery_id=delivery.id,
        org_id=ORG,
        data=ManualCheckSet(
            check_key="ack:def-1",
            delivery_task_id=row.delivery_task_id,
            checked=True,
        ),
        user_id="u5",
    )
    await set_manual_check_core(
        session,
        delivery_id=delivery.id,
        org_id=ORG,
        data=ManualCheckSet(
            check_key="signoff",
            delivery_task_id=row.delivery_task_id,
            checked=True,
        ),
        user_id="u6",
    )
    board = await get_delivery_board_core(
        session, delivery_id=delivery.id, org_id=ORG
    )
    row = board.tasks[0]
    assert board.ready
    assert row.defects[0].acknowledged
    assert row.defects[0].acknowledged_by_user_id == "u5"
    assert _checks(board, task.id)["signoff"].checked_by_user_id == "u6"
    assert _checks(board, task.id)["no_must_fix"].status == "pass"

    # A reserved key cannot be redefined in check_config.
    from oddish.schemas import DeliveryCheckConfig as _Config

    with pytest.raises(HTTPException) as err:
        await patch_delivery_core(
            session,
            delivery_id=delivery.id,
            org_id=ORG,
            data=DeliveryPatch(
                check_config=_Config(
                    manual=[
                        ManualCheckDefinition(
                            key="signoff", label="x", scope="task"
                        )
                    ]
                )
            ),
        )
    assert err.value.status_code == 422


@pytest.mark.asyncio
async def test_no_verdict_qa_run_cannot_vouch(session):
    from datetime import datetime, timezone

    task, v1, experiment = await _green_task(session, "deliv-noverdict")
    v2 = _version(task, 2)
    session.add(v2)
    await session.flush()
    task.current_version_id = v2.id
    # The newest successful QA run graded v2, but it was staged below the
    # evidence bar (with_verdict=false): it restored the v1 verdict instead
    # of authoring one, so it must not make the stored verdict look fresh.
    no_verdict_run = _trial(task, experiment, v2.id, kind="qa")
    no_verdict_run.harbor_config = {"analysis_payload": {"with_verdict": False}}
    no_verdict_run.finished_at = datetime(2026, 8, 20, tzinfo=timezone.utc)
    session.add(no_verdict_run)
    await session.flush()

    delivery = await create_delivery_core(
        session,
        data=DeliveryCreate(customer="acme", name="batch-15", task_ids=[task.id]),
        org_id=ORG,
        user_id="u1",
    )
    board = await get_delivery_board_core(
        session, delivery_id=delivery.id, org_id=ORG
    )
    check = _checks(board, task.id)["verdict_ok"]
    assert check.status == "fail"
    assert "does not cover" in check.detail


@pytest.mark.asyncio
async def test_failing_checks_need_acknowledgement_before_signoff(session):
    """A person can ship a red check, but only with a recorded waive."""
    task, _, _ = await _green_task(session, "deliv-waive")
    v2 = _version(task, 2)
    session.add(v2)
    await session.flush()
    task.current_version_id = v2.id
    await session.flush()

    delivery = await create_delivery_core(
        session,
        data=DeliveryCreate(customer="acme", name="batch-16", task_ids=[task.id]),
        org_id=ORG,
        user_id="u1",
    )
    board = await get_delivery_board_core(
        session, delivery_id=delivery.id, org_id=ORG
    )
    member_id = board.tasks[0].delivery_task_id
    failing = [
        c.key
        for c in board.tasks[0].checks
        if c.kind == "automated" and c.status == "fail"
    ]
    assert set(failing) == {"pre_trial_passed", "min_rollouts", "verdict_ok"}

    # Sign-off is refused while a failing check has no waive.
    with pytest.raises(HTTPException) as err:
        await set_manual_check_core(
            session,
            delivery_id=delivery.id,
            org_id=ORG,
            data=ManualCheckSet(
                check_key="signoff", delivery_task_id=member_id, checked=True
            ),
            user_id="u5",
        )
    assert err.value.status_code == 409
    assert "min_rollouts" in err.value.detail

    # A waive must name a real automated check, and never 'no_must_fix'.
    for bad_key, code in (("waive:nope", 404), ("waive:no_must_fix", 422)):
        with pytest.raises(HTTPException) as err:
            await set_manual_check_core(
                session,
                delivery_id=delivery.id,
                org_id=ORG,
                data=ManualCheckSet(
                    check_key=bad_key, delivery_task_id=member_id, checked=True
                ),
                user_id="u5",
            )
        assert err.value.status_code == code

    for key in failing:
        await set_manual_check_core(
            session,
            delivery_id=delivery.id,
            org_id=ORG,
            data=ManualCheckSet(
                check_key=f"waive:{key}", delivery_task_id=member_id, checked=True
            ),
            user_id="u5",
        )
    await set_manual_check_core(
        session,
        delivery_id=delivery.id,
        org_id=ORG,
        data=ManualCheckSet(
            check_key="signoff", delivery_task_id=member_id, checked=True
        ),
        user_id="u6",
    )
    board = await get_delivery_board_core(
        session, delivery_id=delivery.id, org_id=ORG
    )
    checks = _checks(board, task.id)
    assert board.ready
    for key in failing:
        assert checks[key].status == "waived"
        assert checks[key].checked_by_user_id == "u5"
    assert checks["signoff"].checked_by_user_id == "u6"

    # A new default version voids the waives with everything else.
    v3 = _version(task, 3)
    session.add(v3)
    await session.flush()
    task.current_version_id = v3.id
    await session.flush()
    board = await get_delivery_board_core(
        session, delivery_id=delivery.id, org_id=ORG
    )
    checks = _checks(board, task.id)
    assert checks["min_rollouts"].status == "fail"
    assert checks["signoff"].status == "fail"
    assert not board.ready


@pytest.mark.asyncio
async def test_customers_are_rows_and_reused(session):
    """Every delivery ships to a customer row; equal names share one row."""
    from oddish.core.deliveries import list_customers_core

    d1 = await create_delivery_core(
        session,
        data=DeliveryCreate(customer="Initech", name="batch-c1"),
        org_id=ORG,
        user_id="u1",
    )
    d2 = await create_delivery_core(
        session,
        data=DeliveryCreate(customer="Initech", name="batch-c2"),
        org_id=ORG,
        user_id="u1",
    )
    assert d1.customer_id == d2.customer_id
    assert d1.customer_name == "Initech"

    # A customer id works as the reference too.
    d3 = await create_delivery_core(
        session,
        data=DeliveryCreate(customer=d1.customer_id, name="batch-c3"),
        org_id=ORG,
        user_id="u1",
    )
    assert d3.customer_id == d1.customer_id

    customers = await list_customers_core(session, org_id=ORG)
    assert "Initech" in [c.name for c in customers]

    # Explicit creation makes a row; a duplicate name is a conflict.
    from oddish.core.deliveries import create_customer_core

    hooli = await create_customer_core(session, org_id=ORG, name=" Hooli ")
    assert hooli.name == "Hooli"
    with pytest.raises(HTTPException) as err:
        await create_customer_core(session, org_id=ORG, name="Hooli")
    assert err.value.status_code == 409

    # Patch moves the delivery to another customer, creating it on demand.
    await patch_delivery_core(
        session,
        delivery_id=d2.id,
        org_id=ORG,
        data=DeliveryPatch(customer="Globex"),
    )
    board = await get_delivery_board_core(
        session, delivery_id=d2.id, org_id=ORG
    )
    assert board.delivery.customer_name == "Globex"


@pytest.mark.asyncio
async def test_customer_create_race_recovers(session):
    """When two requests create the same customer name at once, the
    loser's insert hits the unique index. The resolver recovers with the
    winner's row and the explicit create answers 409 — never a 500."""
    from unittest import mock

    import oddish.core.deliveries as deliveries_mod

    existing = await deliveries_mod.create_customer_core(
        session, org_id=ORG, name="racer"
    )
    # Simulate the race window: the pre-insert lookup misses, the insert
    # then collides with the winner's committed row.
    real_find = deliveries_mod._find_customer
    calls = {"n": 0}

    async def racy_find(session_, org_id, ref):
        calls["n"] += 1
        if calls["n"] == 1:
            return None
        return await real_find(session_, org_id, ref)

    with mock.patch.object(deliveries_mod, "_find_customer", racy_find):
        customer = await deliveries_mod._resolve_customer(session, ORG, "racer")
    assert customer.id == existing.id
    assert calls["n"] == 2

    # The session survived the failed insert: further work still commits.
    others = await deliveries_mod.list_customers_core(session, org_id=ORG)
    assert sum(1 for c in others if c.name == "racer") == 1


@pytest.mark.asyncio
async def test_agent_count_matches_verdict_evidence_bar(session):
    """Agent variants that differ only in case or spacing count once."""
    experiment = ExperimentModel(name="exp-deliv-agents", org_id=ORG)
    task = _task("deliv-agents")
    session.add_all([experiment, task])
    await session.flush()
    version = _version(task, 1)
    session.add(version)
    await session.flush()
    task.current_version_id = version.id
    for agent in [" Codex", "codex", "CODEX", "gpt", "gemini"]:
        session.add(_trial(task, experiment, version.id, agent=agent))
    await session.flush()

    delivery = await create_delivery_core(
        session,
        data=DeliveryCreate(customer="acme", name="batch-agents", task_ids=[task.id]),
        org_id=ORG,
        user_id="u1",
    )
    board = await get_delivery_board_core(
        session, delivery_id=delivery.id, org_id=ORG
    )
    check = _checks(board, task.id)["min_rollouts"]
    # 5 trials, but only 3 distinct agents after normalization.
    assert "5/5 trials, 3/3 agents" in check.detail
    assert check.status == "pass"
