"""DB-backed tests for excluding flagged API keys from cost accounting.

Spend from trials whose task was submitted with an ``exclude_from_costs`` key
must vanish from every surface that shares ``first_party_spend_filter``: the
admin cost breakdown and the quota sums. Exclusion resolves at read time
through ``tasks.api_key_id``, so it also survives revoking (soft-deleting)
the key.
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
from oddish.core.api_keys import create_api_key  # noqa: E402
from oddish.core.quotas import sum_org_cost_usd  # noqa: E402
from oddish.db import (  # noqa: E402
    ExperimentModel,
    TaskModel,
    TrialModel,
    get_session,
    task_experiments,
    utcnow,
)
from oddish.db.models import APIKeyModel  # noqa: E402

_RUN = uuid.uuid4().hex[:8]

ORG = f"keycost-org-{_RUN}"
USER = f"keycost-user-{_RUN}"
EXP = f"keycost-exp-{_RUN}"
EXCLUDED_TASK = f"{EXP}-task-excluded"
INCLUDED_TASK = f"{EXP}-task-included"
KEYLESS_TASK = f"{EXP}-task-keyless"

EXCLUDED_COST = 7.0
INCLUDED_COST = 2.0
KEYLESS_COST = 1.0


def _trial(task_id: str, index: int, cost_usd: float, created_at) -> TrialModel:
    return TrialModel(
        id=f"{task_id}-{index}",
        name=f"{task_id}-{index}",
        task_id=task_id,
        experiment_id=EXP,
        org_id=ORG,
        agent="claude-code",
        provider="bedrock",
        queue_key="bedrock/claude-opus-4-8",
        model="claude-opus-4-8",
        billed_user_id=USER,
        cost_usd=cost_usd,
        created_at=created_at,
        finished_at=created_at,
    )


@pytest_asyncio.fixture
async def seeded_excluded_key_data():
    recent = utcnow() - timedelta(hours=1)

    excluded_key, _ = create_api_key(
        org_id=ORG, name=f"excluded-{_RUN}", exclude_from_costs=True
    )
    included_key, _ = create_api_key(org_id=ORG, name=f"included-{_RUN}")

    async with get_session() as session:
        # The combined oddish+backend dev DB FKs tasks.org_id / api_keys.org_id
        # to organizations, so seed the org row first.
        await session.execute(
            text(
                "INSERT INTO organizations "
                "(id, name, slug, plan, settings, is_active, created_at, updated_at) "
                "VALUES (:id, :id, :id, 'free', '{}'::jsonb, true, NOW(), NOW()) "
                "ON CONFLICT (id) DO NOTHING"
            ),
            {"id": ORG},
        )
        session.add_all([excluded_key, included_key])
        await session.flush()
        session.add(
            ExperimentModel(
                id=EXP,
                name="keycost-exp",
                org_id=ORG,
                owner_user_id=USER,
                created_at=recent,
                last_activity_at=recent,
            )
        )
        for task_id, api_key_id in (
            (EXCLUDED_TASK, excluded_key.id),
            (INCLUDED_TASK, included_key.id),
            (KEYLESS_TASK, None),
        ):
            session.add(
                TaskModel(
                    id=task_id,
                    name=task_id,
                    user="test",
                    org_id=ORG,
                    task_path="some/path",
                    api_key_id=api_key_id,
                )
            )
        session.add_all(
            [
                _trial(EXCLUDED_TASK, 0, EXCLUDED_COST, recent),
                _trial(INCLUDED_TASK, 0, INCLUDED_COST, recent),
                _trial(KEYLESS_TASK, 0, KEYLESS_COST, recent),
            ]
        )
        await session.flush()
        await session.execute(
            task_experiments.insert(),
            [
                {"task_id": EXCLUDED_TASK, "experiment_id": EXP},
                {"task_id": INCLUDED_TASK, "experiment_id": EXP},
                {"task_id": KEYLESS_TASK, "experiment_id": EXP},
            ],
        )

    yield excluded_key.id, included_key.id

    async with get_session() as session:
        await session.execute(
            TrialModel.__table__.delete().where(TrialModel.experiment_id == EXP)
        )
        await session.execute(
            TaskModel.__table__.delete().where(TaskModel.id.like(f"{EXP}-task%"))
        )
        await session.execute(
            ExperimentModel.__table__.delete().where(ExperimentModel.id == EXP)
        )
        await session.execute(
            APIKeyModel.__table__.delete().where(
                APIKeyModel.id.in_([excluded_key.id, included_key.id])
            )
        )
        await session.execute(
            text("DELETE FROM organizations WHERE id = :o"), {"o": ORG}
        )


@pytest.mark.asyncio
async def test_excluded_key_spend_dropped_from_breakdown_and_quota(
    seeded_excluded_key_data,
):
    excluded_key_id, _ = seeded_excluded_key_data
    counted = INCLUDED_COST + KEYLESS_COST

    async with get_session() as session:
        result = await get_cost_breakdown_core(
            session, window_days=7, experiment_limit=500, user_limit=500
        )
        exp = next(e for e in result.experiments if e.experiment_id == EXP)
        assert abs(exp.cost_usd - counted) <= 1e-6, exp.cost_usd
        assert exp.trial_count == 2

        period_start = utcnow() - timedelta(days=1)
        org_total = await sum_org_cost_usd(session, ORG, period_start)
        assert abs(float(org_total) - counted) <= 1e-6, org_total

        # Revoking the excluded key must not resurrect its historical spend:
        # cost surfaces query with include_deleted=True.
        await session.execute(
            APIKeyModel.__table__.update()
            .where(APIKeyModel.id == excluded_key_id)
            .values(deleted_at=utcnow())
        )
        await session.flush()

        org_total = await sum_org_cost_usd(session, ORG, period_start)
        assert abs(float(org_total) - counted) <= 1e-6, org_total
