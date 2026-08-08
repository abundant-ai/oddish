"""DB-backed tests for excluding flagged experiments' spend from cost accounting.

Spend from trials homed in an experiment on the ``cost_excluded_experiments``
list must vanish from every surface that shares ``first_party_spend_filter``:
the admin cost breakdown and the quota sums. Removing an experiment from the
list re-includes its spend.
"""

from __future__ import annotations

import sys
import uuid
from datetime import timedelta
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import text

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from oddish.core.admin import get_cost_breakdown_core  # noqa: E402
from oddish.core.quotas import sum_org_cost_usd  # noqa: E402
from oddish.db import (  # noqa: E402
    CostExcludedExperimentModel,
    ExperimentModel,
    TaskModel,
    TrialModel,
    get_session,
    utcnow,
)

_RUN = uuid.uuid4().hex[:8]

ORG = f"expcost-org-{_RUN}"
USER = f"expcost-user-{_RUN}"
EXCLUDED_EXP = f"expcost-excluded-{_RUN}"
INCLUDED_EXP = f"expcost-included-{_RUN}"
EXCLUDED_TASK = f"{EXCLUDED_EXP}-task"
INCLUDED_TASK = f"{INCLUDED_EXP}-task"

EXCLUDED_COST = 7.0
INCLUDED_COST = 2.0


def _trial(task_id, experiment_id, index, cost_usd, created_at) -> TrialModel:
    return TrialModel(
        id=f"{task_id}-{index}",
        name=f"{task_id}-{index}",
        task_id=task_id,
        experiment_id=experiment_id,
        org_id=ORG,
        agent="claude-code",
        provider="xai",
        queue_key="xai/grok-4",
        model="xai/grok-4",
        billed_user_id=USER,
        cost_usd=cost_usd,
        created_at=created_at,
        finished_at=created_at,
    )


@pytest_asyncio.fixture
async def seeded_data():
    recent = utcnow() - timedelta(hours=1)
    excluded = CostExcludedExperimentModel(
        experiment_id=EXCLUDED_EXP, experiment_name="expcost-excluded", label="free"
    )

    async with get_session() as session:
        await session.execute(
            text(
                "INSERT INTO organizations "
                "(id, name, slug, plan, settings, is_active, created_at, updated_at) "
                "VALUES (:id, :id, :id, 'free', '{}'::jsonb, true, NOW(), NOW()) "
                "ON CONFLICT (id) DO NOTHING"
            ),
            {"id": ORG},
        )
        session.add(excluded)
        for exp_id, name in (
            (EXCLUDED_EXP, "expcost-excluded"),
            (INCLUDED_EXP, "expcost-included"),
        ):
            session.add(
                ExperimentModel(
                    id=exp_id,
                    name=name,
                    org_id=ORG,
                    owner_user_id=USER,
                    created_at=recent,
                    last_activity_at=recent,
                )
            )
        for task_id in (EXCLUDED_TASK, INCLUDED_TASK):
            session.add(
                TaskModel(
                    id=task_id,
                    name=task_id,
                    user="test",
                    org_id=ORG,
                    task_path="some/path",
                )
            )
        session.add_all(
            [
                _trial(EXCLUDED_TASK, EXCLUDED_EXP, 0, EXCLUDED_COST, recent),
                _trial(INCLUDED_TASK, INCLUDED_EXP, 0, INCLUDED_COST, recent),
            ]
        )
        await session.flush()

    yield excluded.id

    async with get_session() as session:
        await session.execute(
            TrialModel.__table__.delete().where(
                TrialModel.experiment_id.in_([EXCLUDED_EXP, INCLUDED_EXP])
            )
        )
        await session.execute(
            TaskModel.__table__.delete().where(
                TaskModel.id.in_([EXCLUDED_TASK, INCLUDED_TASK])
            )
        )
        await session.execute(
            ExperimentModel.__table__.delete().where(
                ExperimentModel.id.in_([EXCLUDED_EXP, INCLUDED_EXP])
            )
        )
        await session.execute(
            CostExcludedExperimentModel.__table__.delete().where(
                CostExcludedExperimentModel.experiment_id == EXCLUDED_EXP
            )
        )
        await session.execute(
            text("DELETE FROM organizations WHERE id = :o"), {"o": ORG}
        )


@pytest.mark.asyncio
async def test_excluded_experiment_spend_dropped_then_reincluded_on_removal(
    seeded_data,
):
    excluded_id = seeded_data
    period_start = utcnow() - timedelta(days=1)

    async with get_session() as session:
        result = await get_cost_breakdown_core(
            session, window_days=7, experiment_limit=500, user_limit=500
        )
        by_id = {e.experiment_id: e for e in result.experiments}
        assert EXCLUDED_EXP not in by_id
        included = by_id[INCLUDED_EXP]
        assert abs(included.cost_usd - INCLUDED_COST) <= 1e-6, included.cost_usd

        org_total = await sum_org_cost_usd(session, ORG, period_start)
        assert abs(float(org_total) - INCLUDED_COST) <= 1e-6, org_total

        # Removing the experiment from the list re-includes its spend: the
        # exclusion probe only matches live rows.
        await session.execute(
            CostExcludedExperimentModel.__table__.update()
            .where(CostExcludedExperimentModel.id == excluded_id)
            .values(deleted_at=utcnow())
        )
        await session.flush()

        org_total = await sum_org_cost_usd(session, ORG, period_start)
        expected = INCLUDED_COST + EXCLUDED_COST
        assert abs(float(org_total) - expected) <= 1e-6, org_total


@pytest.mark.asyncio
async def test_inflight_reservation_skips_excluded_experiment_spend(
    seeded_data, monkeypatch
):
    # An in-flight trial homed in an excluded experiment must not reserve
    # quota the settled sums will never charge -- and it reserves again once
    # the experiment leaves the list.
    from oddish.config import settings
    from oddish.core.quotas import (
        inflight_reserved_usd,
        inflight_trial_count_by_org_user_all_orgs,
    )
    from oddish.db import TrialStatus

    monkeypatch.setattr(settings, "pending_trial_reservation_usd", 2.5)

    excluded_id = seeded_data
    inflight = _trial(EXCLUDED_TASK, EXCLUDED_EXP, 9, 5.0, utcnow())
    inflight.finished_at = None
    inflight.status = TrialStatus.RUNNING

    async with get_session() as session:
        session.add(inflight)
        await session.flush()

        reserved = await inflight_reserved_usd(session, ORG, USER)
        assert float(reserved) == 0.0, reserved
        inflight_counts = await inflight_trial_count_by_org_user_all_orgs(session)
        assert inflight_counts.get((ORG, USER), 0) == 0

        await session.execute(
            CostExcludedExperimentModel.__table__.update()
            .where(CostExcludedExperimentModel.id == excluded_id)
            .values(deleted_at=utcnow())
        )
        await session.flush()

        reserved = await inflight_reserved_usd(session, ORG, USER)
        assert abs(float(reserved) - 5.0) <= 1e-6, reserved
        inflight_counts = await inflight_trial_count_by_org_user_all_orgs(session)
        assert inflight_counts[(ORG, USER)] == 1
