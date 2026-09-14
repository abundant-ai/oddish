"""The actual bounded /open response must carry counts, not finding bodies."""

import pytest
from test_deliveries import ORG, _green_task, _trial, _version
from oddish.db import VerdictStatus
from oddish.core.endpoints.task_open import get_task_open_core
from oddish.core.deliveries import create_delivery_core, get_delivery_board_core
from oddish.schemas import DeliveryCreate


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tier_fields, is_must_fix",
    [
        ({"tier": "must_fix"}, True),
        ({"severity": "must_fix"}, True),
        ({"tier": None, "severity": "must_fix"}, True),
        ({"tier": "must_fix", "severity": "optional"}, True),
        ({"tier": "optional", "severity": "must_fix"}, False),
    ],
    ids=["tier", "severity", "null-tier", "required-tier", "optional-tier"],
)
async def test_open_counts_audit_retained_and_live_findings_without_returning_bodies(
    session, tier_fields, is_must_fix
):
    task, version, experiment = await _green_task(session, "open-findings")
    audit = {
        "id": "audit-fix",
        **tier_fields,
        "title": "Audit defect",
        "detail": "PRIVATE FINDING BODY",
    }
    optional = {
        "id": "historical",
        "tier": "optional",
        "title": "Historical suggestion",
    }
    retained = {
        "id": "retained-fix",
        "links_to": "historical",
        **tier_fields,
        "title": "Required retained fix",
    }
    version.pre_trial = {"items": [audit, optional]}
    version.reported_findings = [
        {"source": "pre_trial", "finding": audit},
        {"source": "trial", "finding": retained},
    ]
    live = {"id": "live-fix", **tier_fields, "title": "Run-review defect"}
    run = _trial(
        task,
        experiment,
        version.id,
        analysis={
            "classification": "BAD_FAILURE",
            "action_items": [
                live,
                {**live, "id": "linked-audit", "links_to": "audit-fix"},
            ],
        },
    )
    run.analysis_status = VerdictStatus.SUCCESS
    session.add(run)
    other_version = _version(task, 2)
    session.add(other_version)
    await session.flush()
    # Duplicate IDs and audit links represent one finding. Unreviewed,
    # baseline, probe, superseded, and other-version findings do not count.
    for index, (agent, probe, status, superseded, target) in enumerate(
        [
            ("codex", False, VerdictStatus.SUCCESS, False, version.id),
            ("oracle-special", False, VerdictStatus.SUCCESS, False, version.id),
            ("codex", True, VerdictStatus.SUCCESS, False, version.id),
            ("codex", False, VerdictStatus.RUNNING, False, version.id),
            ("codex", False, VerdictStatus.SUCCESS, True, version.id),
            ("codex", False, VerdictStatus.SUCCESS, False, other_version.id),
        ]
    ):
        trial = _trial(
            task,
            experiment,
            target,
            agent=agent,
            analysis={
                "classification": "BAD_FAILURE",
                "action_items": [
                    {**live, "id": "live-fix" if index == 0 else f"excluded-{index}"}
                ],
            },
        )
        trial.is_probe = probe
        trial.analysis_status = status
        if superseded:
            trial.superseded_by_trial_id = run.id
        session.add(trial)
    await session.flush()
    response = await get_task_open_core(session, task_id=task.id, org_id=ORG)
    assert response.selected_version.must_fix_count == (3 if is_must_fix else 0)
    assert response.selected_version.pre_trial_must_fix_count == (
        1 if is_must_fix else 0
    )
    payload = response.selected_version.model_dump()
    assert "pre_trial_findings" not in payload
    assert "retained_findings" not in payload
    assert "PRIVATE FINDING BODY" not in response.model_dump_json()
    historical = await get_task_open_core(
        session, task_id=task.id, version_id=other_version.id, org_id=ORG
    )
    assert historical.selected_version.must_fix_count == (1 if is_must_fix else 0)
    assert historical.selected_version.pre_trial_must_fix_count == 0
    delivery = await create_delivery_core(
        session,
        data=DeliveryCreate(customer="acme", name="linked finding", task_ids=[task.id]),
        org_id=ORG,
        user_id="u1",
    )
    board = await get_delivery_board_core(session, delivery_id=delivery.id, org_id=ORG)
    defect = next(d for d in board.tasks[0].defects if d.title == retained["title"])
    assert defect.finding_id == "retained-fix"


@pytest.mark.asyncio
async def test_first_run_review_is_counted_without_source_or_retained_findings(session):
    task, version, experiment = await _green_task(session, "first-run-finding")
    trial = _trial(
        task,
        experiment,
        version.id,
        analysis={
            "classification": "BAD_FAILURE",
            "action_items": [
                {"id": "new", "tier": "must_fix", "title": "First run defect"}
            ],
        },
    )
    trial.analysis_status = VerdictStatus.SUCCESS
    session.add(trial)
    await session.flush()
    response = await get_task_open_core(session, task_id=task.id, org_id=ORG)
    assert response.selected_version.must_fix_count == 1
    assert response.selected_version.pre_trial_must_fix_count == 0
