"""Compare paged reads with the complete approval board in real PostgreSQL."""

import pytest
import pytest_asyncio
from oddish.core.deliveries import (
    _compute_board,
    create_delivery_core,
    finalize_delivery_core,
    get_delivery_board_core,
    set_manual_check_core,
)
from oddish.core.delivery_view import (
    delivery_page,
    delivery_selection,
    delivery_task_state,
)
from oddish.core.task_findings import task_defect_items
from oddish.db import utcnow
from oddish.schemas import DeliveryCreate, DeliveryViewQuery, ManualCheckSet
from sqlalchemy.ext.asyncio import AsyncSession
from test_deliveries import ORG, _green_task, _sign_off, _trial
from test_statement_budgets import count_statements


@pytest_asyncio.fixture
async def inventory(session):
    tasks = []
    for i in range(32):
        task, version, experiment = await _green_task(session, f"paged-{i:02}")
        version.qa_work = {
            "owner_user_id": "owner-a" if i % 2 == 0 else "owner-b",
            "issue_categories": ["verifier"] if i % 3 == 0 else [],
        }
        if i % 3 == 0:
            session.add(
                _trial(
                    task,
                    experiment,
                    version.id,
                    analysis={
                        "action_items": [
                            {
                                "id": f"finding-{i}",
                                "title": f"Finding {i}",
                                "tier": "must_fix",
                                "file": "instruction.md",
                                "line_start": 2,
                                "description": "evidence " * 15000,
                            }
                        ]
                    },
                )
            )
        tasks.append(task)
    await session.flush()
    delivery = await create_delivery_core(
        session,
        data=DeliveryCreate(
            customer="Acme", name="Paged delivery", task_ids=[t.id for t in tasks]
        ),
        org_id=ORG,
        user_id="owner-a",
    )
    await session.flush()
    return delivery, tasks


@pytest.mark.asyncio
async def test_pages_preserve_checks_totals_and_bound_evidence(session, inventory):
    delivery, tasks = inventory
    async with AsyncSession(bind=await session.connection()) as reader:
        full = await get_delivery_board_core(
            reader, delivery_id=delivery.id, org_id=ORG
        )
        seen = []
        for number in range(1, 5):
            with count_statements() as statements:
                compact = await get_delivery_board_core(
                    reader, delivery_id=delivery.id, org_id=ORG, include_details=False
                )
                page = await delivery_page(
                    reader, compact, DeliveryViewQuery(page=number, per_page=10)
                )
            assert len(statements) <= 8
            assert all(sql.lstrip().upper().startswith("SELECT") for sql in statements)
            assert len(page.tasks) <= 10
            assert page.task_count == page.total == 32
            assert sum(page.owner_counts.values()) == 32
            assert set(page.member_task_ids) == {t.id for t in tasks}
            assert page.ready == full.ready
            for row in page.tasks:
                original = next(r for r in full.tasks if r.task_id == row.task_id)
                assert row.checks == original.checks
                assert row.qa == original.qa
                assert delivery_task_state(row) == delivery_task_state(original)
                assert [(d.id, d.acknowledged) for d in row.defects] == [
                    (d.id, d.acknowledged) for d in original.defects
                ]
            seen.extend(r.task_id for r in page.tasks)
            assert len(page.model_dump_json()) < len(full.model_dump_json()) / 10
        assert seen == [r.task_id for r in full.tasks]


@pytest.mark.asyncio
async def test_focus_outside_filters_hydrates_exact_finding_and_selection(
    session, inventory
):
    delivery, tasks = inventory
    compact = await get_delivery_board_core(
        session, delivery_id=delivery.id, org_id=ORG, include_details=False
    )
    compact.qa_viewer_user_id = "owner-a"
    view = DeliveryViewQuery(
        filter="awaiting_signoff", owner="mine", task=tasks[0].name, per_page=10
    )
    selected = delivery_selection(compact, view)
    page = await delivery_page(session, compact, view)
    assert page.focus_outside_filters
    assert page.focus_task_id == tasks[0].id
    assert tasks[0].id in {r.task_id for r in page.tasks}
    assert sum(page.owner_counts.values()) == 16
    focused = next(r for r in page.tasks if r.task_id == tasks[0].id)
    assert focused.defects[0].finding["description"] == "evidence " * 15000
    assert {r.delivery_task_id for r in selected} == set(page.matching_task_ids)
    assert len(selected) > 10  # select all spans multiple pages
    assert all(r.version_id for r in selected)
    assert not next(r for r in selected if r.task_id == tasks[0].id).can_sign_off


@pytest.mark.asyncio
async def test_expanding_a_task_preserves_sibling_findings_and_compact_board(
    session, inventory
):
    delivery, tasks = inventory
    compact = await get_delivery_board_core(
        session, delivery_id=delivery.id, org_id=ORG, include_details=False
    )
    original = compact.model_dump_json()
    for task in (tasks[0], tasks[3]):
        with count_statements() as statements:
            page = await delivery_page(
                session, compact, DeliveryViewQuery(task=task.id, per_page=10)
            )
        assert len(statements) == 2  # One version read and one focused findings read.
        assert compact.model_dump_json() == original
        siblings_with_findings = 0
        for row in page.tasks:
            before = next(r for r in compact.tasks if r.task_id == row.task_id)
            if row.task_id == task.id:
                assert row.defects[0].finding["description"] == "evidence " * 15000
            else:
                assert row.defects == before.defects
                siblings_with_findings += bool(row.defects)
                assert all("description" not in d.finding for d in row.defects)
        assert siblings_with_findings == 3


@pytest.mark.asyncio
async def test_filters_counts_and_grouping_agree_with_full_board(session, inventory):
    delivery, _ = inventory
    board = await get_delivery_board_core(
        session, delivery_id=delivery.id, org_id=ORG, include_details=False
    )
    board.qa_viewer_user_id = "owner-a"
    for state in (
        "all",
        "blocked",
        "outstanding",
        "needs_work",
        "qa_incomplete",
        "awaiting_signoff",
        "ready",
    ):
        page = await delivery_page(
            session,
            board,
            DeliveryViewQuery(
                filter=state, owner="mine", issue="verifier", group="owner"
            ),
        )
        expected = [
            r
            for r in board.tasks
            if r.qa_work.owner_user_id == "owner-a"
            and "verifier" in r.qa_work.issue_categories
            and (
                state == "all"
                or state == delivery_task_state(r)
                or state == "blocked"
                and delivery_task_state(r) in ("needs_work", "qa_incomplete")
                or state == "outstanding"
                and not r.ready
            )
        ]
        assert page.total == len(expected)
        assert page.owner_counts["needs_work"] == 6
        assert [r.task_id for r in page.tasks] == [r.task_id for r in expected]


@pytest.mark.asyncio
async def test_single_signoff_scopes_evidence_but_finalize_still_checks_every_member(
    session, inventory, monkeypatch
):
    import oddish.core.deliveries as core
    from fastapi import HTTPException

    delivery, tasks = inventory
    captured = []
    original = core.task_defect_items

    async def track(reader, versions, **kwargs):
        captured.append(set(versions))
        return await original(reader, versions, **kwargs)

    monkeypatch.setattr(core, "task_defect_items", track)
    scoped = await _compute_board(session, delivery, task_ids=[tasks[1].id])
    row = scoped.tasks[0]
    captured.clear()
    await set_manual_check_core(
        session,
        delivery_id=delivery.id,
        org_id=ORG,
        user_id="owner-a",
        data=ManualCheckSet(
            check_key="signoff",
            delivery_task_id=row.delivery_task_id,
            expected_version_id=row.version_id,
            checked=True,
        ),
    )
    assert captured == [{row.version_id}]
    with pytest.raises(HTTPException) as exc:
        await finalize_delivery_core(
            session, delivery_id=delivery.id, org_id=ORG, user_id="owner-a"
        )
    assert exc.value.status_code == 409
    assert len(captured[-1]) == 32


@pytest.mark.asyncio
async def test_finalized_page_uses_saved_evidence_without_current_task_reads(session):
    task, version, _ = await _green_task(session, "frozen-page")
    delivery = await create_delivery_core(
        session,
        data=DeliveryCreate(customer="Acme", name="Frozen", task_ids=[task.id]),
        org_id=ORG,
        user_id="owner-a",
    )
    await _sign_off(session, delivery.id, task.id)
    snapshot = await finalize_delivery_core(
        session, delivery_id=delivery.id, org_id=ORG, user_id="owner-a"
    )
    task.verdict = {"is_good": False}
    version.pre_trial = {"items": [{"id": "new", "title": "new", "tier": "must_fix"}]}
    await session.flush()
    with count_statements() as statements:
        board = await get_delivery_board_core(
            session, delivery_id=delivery.id, org_id=ORG, include_details=False
        )
        page = await delivery_page(session, board, DeliveryViewQuery(task=task.id))
    assert page.frozen and page.ready
    assert [r.model_dump(exclude={"state"}) for r in page.tasks] == [
        r.model_dump() for r in snapshot.tasks
    ]
    assert not any(
        "FROM trials" in sql or "FROM task_versions" in sql for sql in statements
    )


@pytest.mark.asyncio
async def test_compact_read_cannot_find_another_organizations_delivery(
    session, inventory
):
    from fastapi import HTTPException

    delivery, _ = inventory
    with pytest.raises(HTTPException) as exc:
        await get_delivery_board_core(
            session,
            delivery_id=delivery.id,
            org_id="another-org",
            include_details=False,
        )
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_compact_findings_preserve_identity_and_retained_evidence(session):
    task, version, experiment = await _green_task(session, "compact-identity")
    version.pre_trial = {
        "items": [{"id": "shared", "title": "Audit wins", "severity": "must_fix"}]
    }
    version.reported_findings = [
        {
            "finding": {"id": "retained", "title": "Retained", "tier": "should_fix"},
            "source": "analysis",
            "reporting_trial_id": "old-trial",
        }
    ]
    trial = _trial(
        task,
        experiment,
        version.id,
        analysis={
            "action_items": [
                {"id": "shared", "title": "Duplicate audit", "tier": "must_fix"},
                {
                    "id": 7,
                    "title": "Numeric ID",
                    "file": "a.py",
                    "line_start": 3,
                    "severity": "should_fix",
                    "description": "Original body",
                },
                {"title": "No ID", "file": "b.py", "line_start": 1, "tier": "must_fix"},
                {"id": "twice", "title": "First in array", "tier": "must_fix"},
                {"id": "twice", "title": "Second in array", "tier": "must_fix"},
            ]
        },
    )
    trial.deleted_at = utcnow()
    session.add(trial)
    await session.flush()
    full = (await task_defect_items(session, {version.id: version}))[version.id]
    compact = (
        await task_defect_items(session, {version.id: version}, include_details=False)
    )[version.id]
    assert [{k: v for k, v in d.items() if k != "finding"} for d in compact] == [
        {k: v for k, v in d.items() if k != "finding"} for d in full
    ]
    assert [d["title"] for d in full] == [
        "Retained",
        "Audit wins",
        "Numeric ID",
        "No ID",
        "First in array",
    ]
    assert full[2]["finding"]["description"] == "Original body"
    assert "description" not in compact[2]["finding"]


@pytest.mark.asyncio
async def test_page_handles_no_live_highest_version(session):
    task, version, _ = await _green_task(session, "deleted-version-page")
    delivery = await create_delivery_core(
        session,
        data=DeliveryCreate(
            customer="Acme", name="Deleted version", task_ids=[task.id]
        ),
        org_id=ORG,
        user_id="owner-a",
    )
    version.deleted_at = utcnow()
    await session.flush()
    async with AsyncSession(bind=await session.connection()) as reader:
        board = await get_delivery_board_core(
            reader, delivery_id=delivery.id, org_id=ORG, include_details=False
        )
        page = await delivery_page(reader, board, DeliveryViewQuery())
    assert len(page.tasks) == 1 and not page.ready
    assert page.tasks[0].task_id == task.id
