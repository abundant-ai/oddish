"""DB-backed tests for spend admins declared isn't real.

Spend on a model in ``cost_excluded_models``, or homed in an experiment in
``cost_excluded_experiments``, must vanish from every surface that shares
``first_party_spend_filter``: the admin cost breakdown and the quota sums.
Removing the entry re-includes it -- the exclusion only ever hid the money.
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
    CostExcludedModelModel,
    ExperimentModel,
    TaskModel,
    TrialModel,
    get_session,
    utcnow,
)

_RUN = uuid.uuid4().hex[:8]

ORG = f"excl-org-{_RUN}"
USER = f"excl-user-{_RUN}"

# Three experiments so the two axes can be separated: one excluded outright,
# one that merely runs an excluded model, and a control that counts.
EXCLUDED_EXP = f"excl-exp-{_RUN}"
FREE_MODEL_EXP = f"excl-freemodel-{_RUN}"
INCLUDED_EXP = f"excl-included-{_RUN}"
EXPERIMENTS = (EXCLUDED_EXP, FREE_MODEL_EXP, INCLUDED_EXP)

FREE_MODEL = "xai/grok-free-preview"
PAID_MODEL = "xai/grok-4"

EXCLUDED_EXP_COST = 7.0
FREE_MODEL_COST = 3.0
INCLUDED_COST = 2.0


def _trial(experiment_id, index, cost_usd, created_at, model=PAID_MODEL) -> TrialModel:
    task_id = f"{experiment_id}-task"
    return TrialModel(
        id=f"{task_id}-{index}",
        name=f"{task_id}-{index}",
        task_id=task_id,
        experiment_id=experiment_id,
        org_id=ORG,
        agent="claude-code",
        provider="xai",
        queue_key=model,
        model=model,
        billed_user_id=USER,
        cost_usd=cost_usd,
        created_at=created_at,
        finished_at=created_at,
    )


@pytest_asyncio.fixture
async def seeded_data():
    recent = utcnow() - timedelta(hours=1)
    excluded_exp = CostExcludedExperimentModel(
        experiment_id=EXCLUDED_EXP, experiment_name="excl-exp", label="comped"
    )
    excluded_model = CostExcludedModelModel(model_name=FREE_MODEL, label="free tier")

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
        session.add_all([excluded_exp, excluded_model])
        for exp_id in EXPERIMENTS:
            session.add(
                ExperimentModel(
                    id=exp_id,
                    name=exp_id,
                    org_id=ORG,
                    owner_user_id=USER,
                    created_at=recent,
                    last_activity_at=recent,
                )
            )
            session.add(
                TaskModel(
                    id=f"{exp_id}-task",
                    name=f"{exp_id}-task",
                    user="test",
                    org_id=ORG,
                    task_path="some/path",
                )
            )
        session.add_all(
            [
                _trial(EXCLUDED_EXP, 0, EXCLUDED_EXP_COST, recent),
                _trial(FREE_MODEL_EXP, 0, FREE_MODEL_COST, recent, model=FREE_MODEL),
                _trial(INCLUDED_EXP, 0, INCLUDED_COST, recent),
            ]
        )
        await session.flush()

    yield excluded_exp.id, excluded_model.id

    async with get_session() as session:
        await session.execute(
            TrialModel.__table__.delete().where(
                TrialModel.experiment_id.in_(EXPERIMENTS)
            )
        )
        await session.execute(
            TaskModel.__table__.delete().where(
                TaskModel.id.in_([f"{e}-task" for e in EXPERIMENTS])
            )
        )
        await session.execute(
            ExperimentModel.__table__.delete().where(
                ExperimentModel.id.in_(EXPERIMENTS)
            )
        )
        await session.execute(
            CostExcludedExperimentModel.__table__.delete().where(
                CostExcludedExperimentModel.experiment_id == EXCLUDED_EXP
            )
        )
        await session.execute(
            CostExcludedModelModel.__table__.delete().where(
                CostExcludedModelModel.model_name == FREE_MODEL
            )
        )
        await session.execute(
            text("DELETE FROM organizations WHERE id = :o"), {"o": ORG}
        )


@pytest.mark.asyncio
async def test_both_axes_drop_from_cost_breakdown(seeded_data):
    async with get_session() as session:
        result = await get_cost_breakdown_core(
            session, window_days=7, experiment_limit=500, user_limit=500
        )
        by_id = {e.experiment_id: e for e in result.experiments}
        assert EXCLUDED_EXP not in by_id, "excluded experiment still on the dashboard"
        assert FREE_MODEL_EXP not in by_id, "free-model spend still on the dashboard"

        included = by_id[INCLUDED_EXP]
        assert abs(included.cost_usd - INCLUDED_COST) <= 1e-6, included.cost_usd


@pytest.mark.asyncio
async def test_excluded_experiment_spend_reincluded_on_removal(seeded_data):
    excluded_exp_id, _ = seeded_data
    period_start = utcnow() - timedelta(days=1)

    async with get_session() as session:
        org_total = await sum_org_cost_usd(session, ORG, period_start)
        assert abs(float(org_total) - INCLUDED_COST) <= 1e-6, org_total

        # The exclusion probe only matches live rows, so soft-deleting the
        # entry must restore the spend rather than leave it hidden forever.
        await session.execute(
            CostExcludedExperimentModel.__table__.update()
            .where(CostExcludedExperimentModel.id == excluded_exp_id)
            .values(deleted_at=utcnow())
        )
        await session.flush()

        org_total = await sum_org_cost_usd(session, ORG, period_start)
        expected = INCLUDED_COST + EXCLUDED_EXP_COST
        assert abs(float(org_total) - expected) <= 1e-6, org_total


@pytest.mark.asyncio
async def test_excluded_model_spend_reincluded_on_removal(seeded_data):
    _, excluded_model_id = seeded_data
    period_start = utcnow() - timedelta(days=1)

    async with get_session() as session:
        org_total = await sum_org_cost_usd(session, ORG, period_start)
        assert abs(float(org_total) - INCLUDED_COST) <= 1e-6, org_total

        await session.execute(
            CostExcludedModelModel.__table__.update()
            .where(CostExcludedModelModel.id == excluded_model_id)
            .values(deleted_at=utcnow())
        )
        await session.flush()

        org_total = await sum_org_cost_usd(session, ORG, period_start)
        expected = INCLUDED_COST + FREE_MODEL_COST
        assert abs(float(org_total) - expected) <= 1e-6, org_total


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "experiment_id,model",
    [(EXCLUDED_EXP, PAID_MODEL), (FREE_MODEL_EXP, FREE_MODEL)],
)
async def test_inflight_reservation_skips_excluded_spend(
    seeded_data, monkeypatch, experiment_id, model
):
    # An in-flight trial whose spend the settled sums will never charge must
    # not reserve quota against the cap either.
    from oddish.config import settings
    from oddish.core.quotas import (
        inflight_reserved_usd,
        inflight_trial_count_by_org_user_all_orgs,
    )
    from oddish.db import TrialStatus

    monkeypatch.setattr(settings, "pending_trial_reservation_usd", 2.5)

    inflight = _trial(experiment_id, 9, 5.0, utcnow(), model=model)
    inflight.finished_at = None
    inflight.status = TrialStatus.RUNNING

    async with get_session() as session:
        session.add(inflight)
        await session.flush()

        reserved = await inflight_reserved_usd(session, ORG, USER)
        assert float(reserved) == 0.0, reserved
        counts = await inflight_trial_count_by_org_user_all_orgs(session)
        assert counts.get((ORG, USER), 0) == 0


@pytest.mark.asyncio
async def test_model_exclusion_matches_case_and_whitespace_variants(seeded_data):
    # Stored canonically, and trials.model is normalized by the same function
    # on write -- so a trial recorded with odd spelling still matches.
    from oddish.core.cost_exclusions import canonical_excluded_model

    assert canonical_excluded_model("  XAI/Grok-Free-Preview ") == FREE_MODEL
    assert canonical_excluded_model("") == ""
    assert canonical_excluded_model(None) == ""


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "experiment_id,model",
    [(EXCLUDED_EXP, PAID_MODEL), (FREE_MODEL_EXP, FREE_MODEL)],
)
async def test_quota_sweep_does_not_cancel_excluded_trials(
    seeded_data, experiment_id, model
):
    # The cancellation sweep must share the exclusion filters with the sums
    # that trip the cap. An excluded trial contributed nothing to the number
    # that tripped it and reserves nothing, so cancelling it frees no headroom
    # and is pure collateral.
    from sqlalchemy import select

    from oddish.core.quota_enforcement import _active_trial_predicates
    from oddish.db import TrialStatus

    active = _trial(experiment_id, 8, 5.0, utcnow(), model=model)
    active.finished_at = None
    active.status = TrialStatus.RUNNING

    included = _trial(INCLUDED_EXP, 8, 5.0, utcnow())
    included.finished_at = None
    included.status = TrialStatus.RUNNING

    async with get_session() as session:
        session.add_all([active, included])
        await session.flush()

        rows = await session.scalars(
            select(TrialModel.id).where(
                *_active_trial_predicates(ORG, USER, scope="org")
            )
        )
        swept = set(rows)
        assert active.id not in swept, "excluded trial would be cancelled"
        assert included.id in swept, "real spend must still be sweepable"
