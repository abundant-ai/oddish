"""DB-backed tests for excluding flagged LLM-key spend from cost accounting.

Spend from trials stamped with an ``llm_key_hash`` on the
``cost_excluded_llm_keys`` list must vanish from every surface that shares
``first_party_spend_filter``: the admin cost breakdown and the quota sums. A
NULL-hash trial (pre-rollout / unresolved key) is always counted, and removing
a key from the list re-includes its spend.
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
from oddish.core.llm_key_fingerprint import hash_llm_key, key_hint  # noqa: E402
from oddish.core.quotas import sum_org_cost_usd  # noqa: E402
from oddish.db import (  # noqa: E402
    CostExcludedLlmKeyModel,
    ExperimentModel,
    TaskModel,
    TrialModel,
    get_session,
    task_experiments,
    utcnow,
)

_RUN = uuid.uuid4().hex[:8]

ORG = f"llmcost-org-{_RUN}"
USER = f"llmcost-user-{_RUN}"
EXP = f"llmcost-exp-{_RUN}"
EXCLUDED_TASK = f"{EXP}-task-excluded"
INCLUDED_TASK = f"{EXP}-task-included"
NULLHASH_TASK = f"{EXP}-task-nullhash"

EXCLUDED_COST = 7.0
INCLUDED_COST = 2.0
NULLHASH_COST = 1.0

EXCLUDED_KEY = f"xai-excluded-{_RUN}"
EXCLUDED_HASH = hash_llm_key(EXCLUDED_KEY)
INCLUDED_HASH = hash_llm_key(f"xai-included-{_RUN}")


def _trial(task_id, index, cost_usd, created_at, llm_key_hash) -> TrialModel:
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
        llm_key_hash=llm_key_hash,
        created_at=created_at,
        finished_at=created_at,
    )


@pytest_asyncio.fixture
async def seeded_data():
    recent = utcnow() - timedelta(hours=1)
    excluded = CostExcludedLlmKeyModel(
        key_hash=EXCLUDED_HASH, key_hint=key_hint(EXCLUDED_KEY), label="sponsored"
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
        session.add(
            ExperimentModel(
                id=EXP,
                name="llmcost-exp",
                org_id=ORG,
                owner_user_id=USER,
                created_at=recent,
                last_activity_at=recent,
            )
        )
        for task_id in (EXCLUDED_TASK, INCLUDED_TASK, NULLHASH_TASK):
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
                _trial(EXCLUDED_TASK, 0, EXCLUDED_COST, recent, EXCLUDED_HASH),
                _trial(INCLUDED_TASK, 0, INCLUDED_COST, recent, INCLUDED_HASH),
                _trial(NULLHASH_TASK, 0, NULLHASH_COST, recent, None),
            ]
        )
        await session.flush()
        await session.execute(
            task_experiments.insert(),
            [
                {"task_id": EXCLUDED_TASK, "experiment_id": EXP},
                {"task_id": INCLUDED_TASK, "experiment_id": EXP},
                {"task_id": NULLHASH_TASK, "experiment_id": EXP},
            ],
        )

    yield excluded.id

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
            CostExcludedLlmKeyModel.__table__.delete().where(
                CostExcludedLlmKeyModel.key_hash.in_([EXCLUDED_HASH, INCLUDED_HASH])
            )
        )
        await session.execute(
            text("DELETE FROM organizations WHERE id = :o"), {"o": ORG}
        )


@pytest.mark.asyncio
async def test_excluded_key_spend_dropped_then_reincluded_on_removal(seeded_data):
    excluded_id = seeded_data
    counted = INCLUDED_COST + NULLHASH_COST
    period_start = utcnow() - timedelta(days=1)

    async with get_session() as session:
        result = await get_cost_breakdown_core(
            session, window_days=7, experiment_limit=500, user_limit=500
        )
        exp = next(e for e in result.experiments if e.experiment_id == EXP)
        assert abs(exp.cost_usd - counted) <= 1e-6, exp.cost_usd
        assert exp.trial_count == 2

        org_total = await sum_org_cost_usd(session, ORG, period_start)
        assert abs(float(org_total) - counted) <= 1e-6, org_total

        # Removing the key from the list re-includes its spend: the exclusion
        # probe only matches live rows.
        await session.execute(
            CostExcludedLlmKeyModel.__table__.update()
            .where(CostExcludedLlmKeyModel.id == excluded_id)
            .values(deleted_at=utcnow())
        )
        await session.flush()

        org_total = await sum_org_cost_usd(session, ORG, period_start)
        assert abs(float(org_total) - (counted + EXCLUDED_COST)) <= 1e-6, org_total


@pytest.mark.asyncio
async def test_inflight_reservation_skips_excluded_key_spend(seeded_data):
    # A RETRYING attempt keeps its settlement stamp while finished_at is NULL.
    # Its partial cost must not reserve quota that the settled sums will never
    # charge -- and must reserve again once the key leaves the list.
    from oddish.core.quotas import inflight_reserved_usd
    from oddish.db import TrialStatus

    excluded_id = seeded_data
    inflight = _trial(EXCLUDED_TASK, 9, 5.0, utcnow(), EXCLUDED_HASH)
    inflight.finished_at = None
    inflight.status = TrialStatus.RETRYING

    async with get_session() as session:
        session.add(inflight)
        await session.flush()

        reserved = await inflight_reserved_usd(session, ORG, USER)
        assert float(reserved) == 0.0, reserved

        await session.execute(
            CostExcludedLlmKeyModel.__table__.update()
            .where(CostExcludedLlmKeyModel.id == excluded_id)
            .values(deleted_at=utcnow())
        )
        await session.flush()

        reserved = await inflight_reserved_usd(session, ORG, USER)
        assert float(reserved) >= 5.0, reserved
