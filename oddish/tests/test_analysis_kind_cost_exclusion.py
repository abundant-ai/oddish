"""DB-backed tests for excluding analysis-kind trial spend from agent spend.

A trial with ``kind != 'agent'`` is a platform analysis run. Its cost must
vanish from every surface that shares ``first_party_spend_filter`` (the admin
cost breakdown and the quota sums) and be selected by
``analysis_spend_filter`` instead. The agent trial's cost must be counted at
exactly its recorded value -- existing all-agent data produces the same sums
as before the kind clause existed.
"""

from __future__ import annotations

import sys
import uuid
from datetime import timedelta
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import select, text

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from oddish.core.admin import get_cost_breakdown_core  # noqa: E402
from oddish.core.cost_basis import analysis_spend_filter  # noqa: E402
from oddish.core.quotas import sum_org_cost_usd  # noqa: E402
from oddish.db import (  # noqa: E402
    AGENT_TRIAL_KIND,
    ExperimentModel,
    TaskModel,
    TrialModel,
    get_session,
    task_experiments,
    utcnow,
)

_RUN = uuid.uuid4().hex[:8]

ORG = f"kindcost-org-{_RUN}"
USER = f"kindcost-user-{_RUN}"
EXP = f"kindcost-exp-{_RUN}"
AGENT_TASK = f"{EXP}-task-agent"
QA_TASK = f"{EXP}-task-qa"

AGENT_COST = 2.0
QA_COST = 7.0


def _trial(task_id, index, cost_usd, created_at, kind) -> TrialModel:
    return TrialModel(
        id=f"{task_id}-{index}",
        name=f"{task_id}-{index}",
        task_id=task_id,
        experiment_id=EXP,
        org_id=ORG,
        agent="claude-code",
        provider="xai",
        queue_key="xai/grok-4",
        model="xai/grok-4",
        billed_user_id=USER,
        cost_usd=cost_usd,
        kind=kind,
        created_at=created_at,
        finished_at=created_at,
    )


@pytest_asyncio.fixture
async def seeded_data():
    recent = utcnow() - timedelta(hours=1)

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
        session.add(
            ExperimentModel(
                id=EXP,
                name="kindcost-exp",
                org_id=ORG,
                owner_user_id=USER,
                created_at=recent,
                last_activity_at=recent,
            )
        )
        for task_id in (AGENT_TASK, QA_TASK):
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
                _trial(AGENT_TASK, 0, AGENT_COST, recent, AGENT_TRIAL_KIND),
                _trial(QA_TASK, 0, QA_COST, recent, "qa"),
            ]
        )
        await session.flush()
        await session.execute(
            task_experiments.insert(),
            [
                {"task_id": AGENT_TASK, "experiment_id": EXP},
                {"task_id": QA_TASK, "experiment_id": EXP},
            ],
        )

    yield

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
            text("DELETE FROM organizations WHERE id = :o"), {"o": ORG}
        )


@pytest.mark.asyncio
async def test_analysis_kind_spend_excluded_from_agent_spend(seeded_data):
    period_start = utcnow() - timedelta(days=1)

    async with get_session() as session:
        result = await get_cost_breakdown_core(
            session, window_days=7, experiment_limit=500, user_limit=500
        )
        exp = next(e for e in result.experiments if e.experiment_id == EXP)
        assert abs(exp.cost_usd - AGENT_COST) <= 1e-6, exp.cost_usd
        assert exp.trial_count == 1

        org_total = await sum_org_cost_usd(session, ORG, period_start)
        assert abs(float(org_total) - AGENT_COST) <= 1e-6, org_total


@pytest.mark.asyncio
async def test_analysis_spend_filter_selects_only_non_agent_kinds(seeded_data):
    async with get_session() as session:
        rows = (
            await session.execute(
                select(TrialModel.id, TrialModel.cost_usd).where(
                    TrialModel.experiment_id == EXP, analysis_spend_filter()
                )
            )
        ).all()
    assert [(r.id, float(r.cost_usd)) for r in rows] == [(f"{QA_TASK}-0", QA_COST)]
