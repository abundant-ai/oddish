"""DB-backed tests for the global admin cost breakdown over billable trials."""

from __future__ import annotations

import sys
import uuid
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import text

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from oddish.config import settings  # noqa: E402
from oddish.core.admin import (  # noqa: E402
    _UNATTRIBUTED_KEY,
    _spend_identity,
    get_cost_breakdown_core,
)
from oddish.core.dashboard import EXPERIMENTS_UNATTRIBUTED_OWNER  # noqa: E402
from oddish.config import normalize_model_id  # noqa: E402
from oddish.db import (  # noqa: E402
    AnalysisCostModel,
    ExperimentModel,
    TaskModel,
    TrialModel,
    TrialOrigin,
    get_session,
    task_experiments,
    utcnow,
)
from oddish.model_pricing import estimate_cost_usd  # noqa: E402

_RUN = uuid.uuid4().hex[:8]

_EST_MODEL = "gpt-5.5-pro"
_EST_IN, _EST_OUT, _EST_CACHE = 10_000, 4_000, 1_000
_EXPECTED_EST = estimate_cost_usd(_EST_MODEL, _EST_IN, _EST_OUT, _EST_CACHE)

USER_A = f"costuser-a-{_RUN}"
USER_B = f"costuser-b-{_RUN}"
USER_C = f"costuser-c-{_RUN}"
ORG_1 = f"costorg-1-{_RUN}"
ORG_2 = f"costorg-2-{_RUN}"
E1 = f"costexp-1-{_RUN}"
E2 = f"costexp-2-{_RUN}"
E3 = f"costexp-3-{_RUN}"
E4 = f"costexp-deleted-{_RUN}"
E5 = f"costexp-nonbillable-{_RUN}"
E6 = f"costexp-noauthor-{_RUN}"
E7 = f"costexp-twotask-{_RUN}"
E8 = f"costexp-stamped-unknown-{_RUN}"


def _approx(a: float | None, b: float | None, tol: float = 1e-6) -> bool:
    return a is not None and b is not None and abs(a - b) <= tol


@pytest_asyncio.fixture
async def seeded_cost_data():
    """Insert experiments, tasks, and trials, then hard-delete them after."""
    now = utcnow()
    recent = now - timedelta(hours=1)
    old = now - timedelta(days=40)

    async with get_session() as session:
        session.add_all(
            [
                ExperimentModel(
                    id=E1,
                    name="cost-exp-one",
                    org_id=ORG_1,
                    owner_user_id=USER_A,
                    created_at=recent,
                    last_activity_at=recent,
                ),
                ExperimentModel(
                    id=E2,
                    name="cost-exp-two",
                    org_id=ORG_1,
                    owner_user_id=USER_B,
                    owner="gh-octocat",
                    created_at=recent,
                    last_activity_at=recent,
                ),
                ExperimentModel(
                    id=E3,
                    name="cost-exp-three",
                    org_id=ORG_2,
                    owner_user_id=EXPERIMENTS_UNATTRIBUTED_OWNER,
                    created_at=recent,
                    last_activity_at=recent,
                ),
                ExperimentModel(
                    id=E4,
                    name="cost-exp-deleted",
                    org_id=ORG_1,
                    owner_user_id=USER_A,
                    created_at=recent,
                    last_activity_at=recent,
                    deleted_at=now,
                ),
                ExperimentModel(
                    id=E5,
                    name="cost-exp-nonbillable",
                    org_id=ORG_2,
                    owner_user_id=USER_A,
                    created_at=recent,
                    last_activity_at=recent,
                ),
                ExperimentModel(
                    id=E6,
                    name="cost-exp-noauthor",
                    org_id=ORG_2,
                    owner_user_id=None,
                    created_at=recent,
                    last_activity_at=recent,
                ),
                ExperimentModel(
                    id=E7,
                    name="cost-exp-twotask",
                    org_id=ORG_2,
                    owner_user_id=None,
                    created_at=recent,
                    last_activity_at=recent,
                ),
                ExperimentModel(
                    id=E8,
                    name="cost-exp-stamped-unknown",
                    org_id=ORG_2,
                    owner_user_id=None,
                    owner="unknown",
                    created_at=recent,
                    last_activity_at=recent,
                ),
            ]
        )
        task_tags = {
            E2: {"github_username": "e2-tag"},
            E3: {"github_username": "e3-gh"},
        }
        task_users = {E6: "unknown", E8: "e8-runner"}
        for exp_id, org_id in (
            (E1, ORG_1),
            (E2, ORG_1),
            (E3, ORG_2),
            (E4, ORG_1),
            (E5, ORG_2),
            (E6, ORG_2),
            (E8, ORG_2),
        ):
            session.add(
                TaskModel(
                    id=f"{exp_id}-task",
                    name=f"{exp_id}-task",
                    user=task_users.get(exp_id, "test"),
                    org_id=org_id,
                    task_path="some/path",
                    tags=task_tags.get(exp_id),
                )
            )
        session.add_all(
            [
                TaskModel(
                    id=f"{E7}-task-old",
                    name=f"{E7}-task-old",
                    user="alice",
                    org_id=ORG_2,
                    task_path="some/path",
                    created_at=old,
                ),
                TaskModel(
                    id=f"{E7}-task-new",
                    name=f"{E7}-task-new",
                    user="bob",
                    org_id=ORG_2,
                    task_path="some/path",
                    created_at=recent,
                ),
            ]
        )
        session.add_all(
            [
                _trial(
                    E1,
                    0,
                    model="claude-opus-4-8",
                    cost_usd=2.0,
                    created_at=recent,
                    billed_user_id=USER_A,
                ),
                _trial(
                    E1,
                    1,
                    model="claude-opus-4-8",
                    cost_usd=3.0,
                    created_at=recent,
                    billed_user_id=USER_B,
                ),
                _trial(
                    E2,
                    0,
                    model=_EST_MODEL,
                    provider="openai",
                    agent="codex",
                    cost_usd=None,
                    input_tokens=_EST_IN,
                    output_tokens=_EST_OUT,
                    cache_tokens=_EST_CACHE,
                    created_at=recent,
                    billed_user_id=USER_B,
                ),
                _trial(
                    E3,
                    0,
                    model="Claude-Opus-4-8",
                    cost_usd=1.5,
                    created_at=recent,
                    billed_user_id=USER_A,
                ),
                _trial(
                    E4,
                    0,
                    model="claude-opus-4-8",
                    cost_usd=99.0,
                    created_at=recent,
                    billed_user_id=USER_A,
                ),
                _trial(
                    E1,
                    2,
                    model="claude-opus-4-8",
                    cost_usd=50.0,
                    created_at=recent,
                    deleted_at=now,
                ),
                _trial(
                    E1,
                    3,
                    model="claude-opus-4-8",
                    cost_usd=7.0,
                    created_at=old,
                    billed_user_id=USER_A,
                ),
                _trial(E5, 0, model="claude-opus-4-8", cost_usd=4.0, created_at=recent),
                _trial(
                    E6,
                    0,
                    model="claude-opus-4-8",
                    cost_usd=1.0,
                    created_at=recent,
                    billed_user_id=USER_C,
                ),
                _trial(
                    E7,
                    1,
                    model="claude-opus-4-8",
                    cost_usd=1.0,
                    created_at=recent,
                    billed_user_id=USER_C,
                    task_id=f"{E7}-task-new",
                ),
                _trial(
                    E8,
                    0,
                    model="claude-opus-4-8",
                    cost_usd=1.0,
                    created_at=recent,
                    billed_user_id=USER_C,
                ),
            ]
        )
        await session.flush()
        await session.execute(
            task_experiments.insert(),
            [
                {"task_id": f"{E1}-task", "experiment_id": E1},
                {"task_id": f"{E2}-task", "experiment_id": E2},
                {"task_id": f"{E3}-task", "experiment_id": E3},
                {"task_id": f"{E4}-task", "experiment_id": E4},
                {"task_id": f"{E5}-task", "experiment_id": E5},
                {"task_id": f"{E6}-task", "experiment_id": E6},
                {"task_id": f"{E7}-task-old", "experiment_id": E7},
                {"task_id": f"{E7}-task-new", "experiment_id": E7},
                {"task_id": f"{E8}-task", "experiment_id": E8},
            ],
        )

    yield

    async with get_session() as session:
        for exp_id in (E1, E2, E3, E4, E5, E6, E7, E8):
            await session.execute(
                TrialModel.__table__.delete().where(TrialModel.experiment_id == exp_id)
            )
            await session.execute(
                TaskModel.__table__.delete().where(TaskModel.id.like(f"{exp_id}-task%"))
            )
            await session.execute(
                ExperimentModel.__table__.delete().where(ExperimentModel.id == exp_id)
            )


def _trial(
    experiment_id: str,
    index: int,
    *,
    model: str | None,
    cost_usd: float | None,
    created_at,
    provider: str = "bedrock",
    agent: str = "claude-code",
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    cache_tokens: int | None = None,
    billed_user_id: str | None = None,
    deleted_at=None,
    task_id: str | None = None,
    origin: TrialOrigin = TrialOrigin.ODDISH,
    is_probe: bool = False,
    idempotency_key: str | None = None,
    finished_at=None,
    harbor_stage: str | None = None,
) -> TrialModel:
    return TrialModel(
        id=f"{experiment_id}-{index}",
        name=f"{experiment_id}-{index}",
        task_id=task_id or f"{experiment_id}-task",
        experiment_id=experiment_id,
        org_id=None,
        agent=agent,
        provider=provider,
        queue_key=f"{provider}/{model or 'default'}",
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_tokens=cache_tokens,
        billed_user_id=billed_user_id,
        cost_usd=cost_usd,
        created_at=created_at,
        # The cost dashboards key off settlement time; default a seeded trial to
        # finished at its created_at so it lands in the same window as before.
        finished_at=created_at if finished_at is None else finished_at,
        harbor_stage=harbor_stage,
        deleted_at=deleted_at,
        origin=origin,
        is_probe=is_probe,
        idempotency_key=idempotency_key,
    )


@pytest.mark.asyncio
async def test_cost_breakdown_window_attribution_and_soft_delete(seeded_cost_data):
    assert _EXPECTED_EST is not None and _EXPECTED_EST > 0

    async with get_session() as session:
        result = await get_cost_breakdown_core(
            session, window_days=7, experiment_limit=500, user_limit=500
        )

    exps = {e.experiment_id: e for e in result.experiments}

    # Historical spend remains visible after deletion and is explicitly marked.
    assert _approx(exps[E4].cost_usd, 99.0)
    assert exps[E4].is_deleted is True
    assert exps[E4].has_deleted_spend is True
    # E5's lone trial is unbilled (no billed_user_id) but real oddish spend, so
    # it is now counted instead of dropped by the old billed-only gate.
    assert _approx(exps[E5].cost_usd, 4.0), exps[E5].cost_usd

    assert _approx(exps[E1].cost_usd, 55.0), exps[E1].cost_usd
    assert _approx(exps[E1].cost_estimated_usd, 0.0)
    assert exps[E1].trial_count == 3
    assert exps[E1].is_deleted is False
    assert exps[E1].has_deleted_spend is True
    assert exps[E1].models[0].model == "claude-opus-4-8"
    assert _approx(exps[E1].models[0].cost_usd, 55.0)

    assert _approx(exps[E2].cost_usd, _EXPECTED_EST), exps[E2].cost_usd
    assert _approx(exps[E2].cost_estimated_usd, _EXPECTED_EST)
    assert exps[E2].models[0].model == _EST_MODEL
    assert exps[E2].input_tokens == _EST_IN

    assert _approx(exps[E3].cost_usd, 1.5)
    assert exps[E3].models[0].model == "claude-opus-4-8"
    assert exps[E3].owner_user_id is None

    assert exps[E2].owner_label == "gh-octocat", exps[E2].owner_label
    assert exps[E3].owner_label == "e3-gh", exps[E3].owner_label
    assert exps[E1].owner_label == "test", exps[E1].owner_label
    assert exps[E6].owner_label is None, exps[E6].owner_label
    assert exps[E7].owner_label == "alice", exps[E7].owner_label
    assert exps[E8].owner_label == "e8-runner", exps[E8].owner_label

    by_user = {u.key: u for u in result.by_user}
    assert _approx(by_user[USER_A].cost_usd, 102.5)
    assert by_user[USER_A].owner_user_id == USER_A
    # All of USER_A's in-window spend is billed, so no unbilled flag.
    assert by_user[USER_A].has_unbilled_spend is False
    assert by_user[USER_A].label is None
    assert by_user[USER_A].experiment_count == 3
    assert by_user[USER_A].trial_count == 3
    assert _approx(by_user[USER_B].cost_usd, 3.0 + _EXPECTED_EST)
    assert by_user[USER_B].experiment_count == 2
    # Unbilled spend surfaces as its own bucket instead of vanishing; it is not
    # a registered user, so it stays non-clickable and flags the unbilled spend.
    assert _approx(by_user["__unattributed__"].cost_usd, 54.0)
    assert by_user["__unattributed__"].owner_user_id is None
    assert by_user["__unattributed__"].has_unbilled_spend is True
    assert by_user["__unattributed__"].label == "Unattributed"

    costs = [e.cost_usd for e in result.experiments]
    assert costs == sorted(costs, reverse=True)

    assert result.bucket == "day"
    for series in (
        result.series_by_agent,
        result.series_by_model,
        result.series_by_user,
    ):
        assert series.buckets, "expected at least one series bucket"
        assert _approx(
            sum(b.cost_usd for b in series.buckets), result.totals.cost_usd, tol=0.01
        )
        assert sum(b.trial_count for b in series.buckets) == result.totals.trial_count
        starts = [b.bucket_start for b in series.buckets]
        assert starts == sorted(starts)
        for b in series.buckets:
            assert _approx(sum(b.costs.values()), b.cost_usd, tol=0.01)

    agent_keys = {k.key for k in result.series_by_agent.keys}
    assert "claude-code" in agent_keys and "codex" in agent_keys

    model_keys = {k.key for k in result.series_by_model.keys}
    assert "claude-opus-4-8" in model_keys
    assert "Claude-Opus-4-8" not in model_keys

    user_keys = {k.key for k in result.series_by_user.keys}
    assert USER_A in user_keys and USER_B in user_keys
    assert "__unattributed__" in user_keys
    unattributed_key = next(
        k for k in result.series_by_user.keys if k.key == "__unattributed__"
    )
    assert unattributed_key.label == "Unattributed"


@pytest.mark.asyncio
async def test_monthly_quota_cost_uses_budgeted_orgs_only(
    seeded_cost_data, monkeypatch
):
    assert _EXPECTED_EST is not None and _EXPECTED_EST > 0
    monkeypatch.setattr(settings, "default_org_monthly_quota_usd", None)

    async with get_session() as session:
        await session.execute(
            text(
                "INSERT INTO organizations "
                "(id, name, slug, plan, settings, is_active, created_at, updated_at) "
                "VALUES (:id, :id, :id, 'free', '{}'::jsonb, true, NOW(), NOW())"
            ),
            {"id": ORG_1},
        )
        await session.execute(
            text(
                "INSERT INTO org_quotas "
                "(id, org_id, limit_usd, period_kind, created_at, updated_at) "
                "VALUES (:qid, :org_id, :limit, 'monthly', NOW(), NOW())"
            ),
            {"qid": uuid.uuid4().hex[:8], "org_id": ORG_1, "limit": Decimal("10.00")},
        )
        await session.execute(
            text(
                "UPDATE trials SET org_id = :org_id "
                "WHERE experiment_id IN (:exp_1, :exp_2)"
            ),
            {"org_id": ORG_1, "exp_1": E1, "exp_2": E2},
        )
        await session.execute(
            text(
                "UPDATE trials SET org_id = :org_id "
                "WHERE experiment_id IN (:exp_3, :exp_5, :exp_6, :exp_7, :exp_8)"
            ),
            {
                "org_id": ORG_2,
                "exp_3": E3,
                "exp_5": E5,
                "exp_6": E6,
                "exp_7": E7,
                "exp_8": E8,
            },
        )
        await session.flush()

    try:
        async with get_session() as session:
            result = await get_cost_breakdown_core(
                session, window_days=7, experiment_limit=500, user_limit=500
            )
        assert result.totals.month_budget_usd == 10.0
        assert _approx(result.totals.month_cost_usd, 55.0 + _EXPECTED_EST)
    finally:
        async with get_session() as session:
            await session.execute(
                text("DELETE FROM org_quotas WHERE org_id = :org_id"),
                {"org_id": ORG_1},
            )
            await session.execute(
                text("DELETE FROM organizations WHERE id = :org_id"),
                {"org_id": ORG_1},
            )


@pytest.mark.asyncio
async def test_cost_breakdown_includes_unattributed_and_normalizes_sentinel(
    seeded_cost_data,
):
    """Unbilled spend surfaces as an Unattributed row; the owner sentinel never
    reaches the UI (as a link target) even though its spend is now counted."""
    async with get_session() as session:
        result = await get_cost_breakdown_core(
            session, window_days=7, experiment_limit=500, user_limit=500
        )

    exps = {e.experiment_id: e for e in result.experiments}

    assert E5 in exps  # unbilled but real oddish spend is now counted
    assert exps[E3].owner_user_id is None, exps[E3].owner_user_id
    assert exps[E3].owner_label == "e3-gh", exps[E3].owner_label

    # The internal unattributed-owner sentinel is never a link target.
    assert all(e.owner_user_id != EXPERIMENTS_UNATTRIBUTED_OWNER for e in exps.values())
    link_ids = {u.owner_user_id for u in result.by_user}
    assert EXPERIMENTS_UNATTRIBUTED_OWNER not in link_ids
    # Fallback rows carry no link id; the Unattributed row is one such row.
    assert None in link_ids
    unattributed = next(u for u in result.by_user if u.key == "__unattributed__")
    assert unattributed.owner_user_id is None
    assert unattributed.label == "Unattributed"

    assert any(k.key == "__unattributed__" for k in result.series_by_user.keys)


@pytest.mark.asyncio
async def test_cost_breakdown_all_time_includes_old_trials(seeded_cost_data):
    async with get_session() as session:
        windowed = await get_cost_breakdown_core(
            session, window_days=7, experiment_limit=500, user_limit=500
        )
        all_time = await get_cost_breakdown_core(
            session, window_days=None, experiment_limit=500, user_limit=500
        )

    e1_windowed = {e.experiment_id: e for e in windowed.experiments}[E1]
    e1_all = {e.experiment_id: e for e in all_time.experiments}[E1]

    assert _approx(e1_windowed.cost_usd, 55.0)
    assert _approx(e1_all.cost_usd, 62.0)
    assert e1_all.trial_count == 4

    user_a_all = {u.key: u for u in all_time.by_user}[USER_A]
    assert _approx(user_a_all.cost_usd, 109.5)


# --- attribution fallbacks + billability predicate ---------------------------

FA = f"costfall-ghuser-{_RUN}"
FB = f"costfall-ghid-{_RUN}"
FC = f"costfall-sub-{_RUN}"
FD = f"costfall-imported-{_RUN}"
FE = f"costfall-combine-{_RUN}"
FF = f"costfall-probe-{_RUN}"
FG = f"costfall-merge-{_RUN}"
_FALL_EXPS = (FA, FB, FC, FD, FE, FF, FG)
SUBMITTER = f"costfall-submitter-{_RUN}"
PAYER = f"costfall-payer-{_RUN}"
MERGED = f"costfall-merged-{_RUN}"


@pytest_asyncio.fixture
async def seeded_fallback_data():
    recent = utcnow() - timedelta(hours=1)
    async with get_session() as session:
        session.add_all(
            [
                ExperimentModel(
                    id=e,
                    name=e,
                    org_id=ORG_1,
                    created_at=recent,
                    last_activity_at=recent,
                )
                for e in _FALL_EXPS
            ]
        )
        task_tags = {
            FA: {"github_username": "octo-ext"},
            FB: {"github_id": "gh-9001", "github_username": "with-id"},
        }
        for e in _FALL_EXPS:
            session.add(
                TaskModel(
                    id=f"{e}-task",
                    name=f"{e}-task",
                    user="runner",
                    org_id=ORG_1,
                    task_path="some/path",
                    tags=task_tags.get(e),
                    created_by_user_id=SUBMITTER if e == FC else None,
                )
            )
        # Second FG task, submitted by MERGED, carries the unbilled half.
        session.add(
            TaskModel(
                id=f"{FG}-task-sub",
                name=f"{FG}-task-sub",
                user="runner",
                org_id=ORG_1,
                task_path="some/path",
                created_by_user_id=MERGED,
            )
        )
        session.add_all(
            [
                # Unbilled but real oddish spend -> GitHub-handle fallback.
                _trial(
                    FA, 0, model="claude-opus-4-8", cost_usd=10.0, created_at=recent
                ),
                # Unbilled -> GitHub-id fallback (handle shown when present).
                _trial(
                    FB, 0, model="claude-opus-4-8", cost_usd=20.0, created_at=recent
                ),
                # Unbilled, no GitHub -> submitting-credential fallback.
                _trial(FC, 0, model="claude-opus-4-8", cost_usd=5.0, created_at=recent),
                # Imported (external Harbor run) -> excluded, no double count.
                _trial(
                    FD,
                    0,
                    model="claude-opus-4-8",
                    cost_usd=999.0,
                    created_at=recent,
                    origin=TrialOrigin.IMPORTED,
                    billed_user_id=PAYER,
                ),
                # Experiment-combine copy -> excluded (originals carry the spend).
                _trial(
                    FE,
                    0,
                    model="claude-opus-4-8",
                    cost_usd=888.0,
                    created_at=recent,
                    idempotency_key=f"combine:{FE}:src-{_RUN}",
                    billed_user_id=PAYER,
                ),
                # Billed probe -> counted (drew budget), attributed to its payer.
                _trial(
                    FF,
                    0,
                    model="claude-opus-4-8",
                    cost_usd=7.0,
                    created_at=recent,
                    billed_user_id=PAYER,
                    is_probe=True,
                ),
                # Same user MERGED billed on one trial and the (unbilled)
                # submitter fallback on another: they merge into one linkable row
                # whatever order the SQL groups arrive in.
                _trial(
                    FG,
                    0,
                    model="claude-opus-4-8",
                    cost_usd=3.0,
                    created_at=recent,
                    billed_user_id=MERGED,
                ),
                _trial(
                    FG,
                    1,
                    model="claude-opus-4-8",
                    cost_usd=2.0,
                    created_at=recent,
                    task_id=f"{FG}-task-sub",
                ),
            ]
        )
        await session.flush()

    yield

    async with get_session() as session:
        for e in _FALL_EXPS:
            await session.execute(
                TrialModel.__table__.delete().where(TrialModel.experiment_id == e)
            )
            await session.execute(
                TaskModel.__table__.delete().where(TaskModel.id.like(f"{e}-task%"))
            )
            await session.execute(
                ExperimentModel.__table__.delete().where(ExperimentModel.id == e)
            )


@pytest.mark.asyncio
async def test_cost_breakdown_attribution_fallbacks_and_billability(
    seeded_fallback_data,
):
    async with get_session() as session:
        result = await get_cost_breakdown_core(
            session, window_days=7, experiment_limit=500, user_limit=500
        )

    by_user = {u.key: u for u in result.by_user}

    # GitHub-handle fallback: label-only, not a registered oddish user, so it
    # stays non-clickable; the spend is unbilled.
    assert _approx(by_user["ghuser:octo-ext"].cost_usd, 10.0)
    assert by_user["ghuser:octo-ext"].label == "@octo-ext"
    assert by_user["ghuser:octo-ext"].owner_user_id is None
    assert by_user["ghuser:octo-ext"].has_unbilled_spend is True

    # GitHub-id fallback keys on the id but shows the handle when present; also
    # not a registered user, so non-clickable and unbilled.
    assert _approx(by_user["ghid:gh-9001"].cost_usd, 20.0)
    assert by_user["ghid:gh-9001"].label == "@with-id"
    assert by_user["ghid:gh-9001"].owner_user_id is None
    assert by_user["ghid:gh-9001"].has_unbilled_spend is True

    # Submitting-credential fallback keys on the real user id: the spend is
    # unbilled but it IS a real oddish user, so the row links to their drilldown
    # and flags the unbilled spend. It carries no label.
    assert _approx(by_user[SUBMITTER].cost_usd, 5.0)
    assert by_user[SUBMITTER].owner_user_id == SUBMITTER
    assert by_user[SUBMITTER].has_unbilled_spend is True
    assert by_user[SUBMITTER].label is None

    # Billed probe is kept and attributed to its payer with a drilldown link;
    # all of its spend is billed, so no unbilled flag.
    assert _approx(by_user[PAYER].cost_usd, 7.0)
    assert by_user[PAYER].owner_user_id == PAYER
    assert by_user[PAYER].has_unbilled_spend is False
    assert by_user[PAYER].label is None

    # Billed + submitter-fallback spend for the same real user merges into one
    # row (total 5.0). It links to the user's drilldown and flags the unbilled
    # half, since the billed-only drilldown (3.0) totals less than this row.
    assert _approx(by_user[MERGED].cost_usd, 5.0)
    assert by_user[MERGED].owner_user_id == MERGED
    assert by_user[MERGED].has_unbilled_spend is True
    assert by_user[MERGED].label is None

    # The "N users" total counts real users, not the GitHub-handle /
    # Unattributed fallback rows (which are exactly the labelled rows).
    assert result.totals.user_count == sum(1 for u in result.by_user if u.label is None)
    assert by_user["ghuser:octo-ext"].label is not None

    # Imported and combine-copy spend is excluded so nothing double-counts.
    exp_ids = {e.experiment_id for e in result.experiments}
    assert FD not in exp_ids
    assert FE not in exp_ids

    fall_total = sum(
        u.cost_usd
        for u in result.by_user
        if u.key in {"ghuser:octo-ext", "ghid:gh-9001", SUBMITTER, PAYER}
    )
    assert _approx(fall_total, 42.0)


def test_spend_identity_github_rung_precedence():
    # A resolved GitHub identity keys on that user, so their handle spend lands
    # on the same row their billed spend does instead of a ghost @handle row.
    assert _spend_identity(None, "gh-1", "octo", None, "user-1") == (
        "user-1",
        "user-1",
        None,
    )
    # The billed payer still outranks it.
    assert _spend_identity("payer", "gh-1", "octo", None, "user-1") == (
        "payer",
        "payer",
        None,
    )
    # Resolving to nobody leaves every existing fallback exactly as it was.
    assert _spend_identity(None, "gh-1", "octo", None, None) == (
        "ghid:gh-1",
        None,
        "@octo",
    )
    assert _spend_identity(None, None, "octo", None, None) == (
        "ghuser:octo",
        None,
        "@octo",
    )
    assert _spend_identity(None, None, None, "sub", None) == ("sub", "sub", None)
    assert _spend_identity(None, None, None, None, None) == (
        _UNATTRIBUTED_KEY,
        None,
        "Unattributed",
    )


@pytest.mark.asyncio
async def test_cost_breakdown_merges_resolved_github_identity(seeded_fallback_data):
    """A handle owned by a registered user folds into that user's row."""

    async def resolver(_session, identities):
        # Only "octo-ext" belongs to someone registered; gh-9001 is a stranger.
        return {i: MERGED for i in identities if i[2] == "octo-ext"}

    async with get_session() as session:
        result = await get_cost_breakdown_core(
            session,
            window_days=7,
            experiment_limit=500,
            user_limit=500,
            resolve_github_users=resolver,
        )

    by_user = {u.key: u for u in result.by_user}

    # MERGED's own billed (3.0) + submitter-fallback (2.0) + the handle's 10.0.
    assert "ghuser:octo-ext" not in by_user
    assert _approx(by_user[MERGED].cost_usd, 15.0)
    assert by_user[MERGED].owner_user_id == MERGED
    assert by_user[MERGED].label is None
    # The handle's spend was never billed, so the merged row still says so.
    assert by_user[MERGED].has_unbilled_spend is True

    # An identity that resolves to nobody is untouched by the resolver.
    assert _approx(by_user["ghid:gh-9001"].cost_usd, 20.0)
    assert by_user["ghid:gh-9001"].label == "@with-id"
    assert by_user["ghid:gh-9001"].owner_user_id is None

    # One person, one row: merging must not double-count them in "N users".
    assert result.totals.user_count == sum(1 for u in result.by_user if u.label is None)
    assert sum(1 for u in result.by_user if u.key == MERGED) == 1


@pytest.mark.asyncio
async def test_cost_breakdown_includes_qa_analysis_cost():
    """Analysis-job spend surfaces as qa_cost_usd plus a qa_by_model row."""
    recent = utcnow() - timedelta(hours=1)
    qa_model = f"gpt-5.5-qa-{_RUN}"
    label = normalize_model_id(qa_model)

    async with get_session() as session:
        result = await get_cost_breakdown_core(session, window_days=7)
        baseline = result.totals.qa_cost_usd

    async with get_session() as session:
        session.add_all(
            [
                AnalysisCostModel(
                    id=f"qacost-{_RUN}-{i}",
                    job_kind="trial_classifier",
                    model=qa_model,
                    cost_usd=cost,
                    cost_source="native",
                    created_at=recent,
                )
                for i, cost in enumerate((0.25, 0.75))
            ]
        )

    try:
        async with get_session() as session:
            result = await get_cost_breakdown_core(session, window_days=7)
        # Robust to other analysis rows already in the shared DB: check the delta.
        assert _approx(result.totals.qa_cost_usd - baseline, 1.0)
        by_model = {m.model: m.cost_usd for m in result.qa_by_model}
        assert _approx(by_model.get(label), 1.0)
    finally:
        async with get_session() as session:
            await session.execute(
                AnalysisCostModel.__table__.delete().where(
                    AnalysisCostModel.id.like(f"qacost-{_RUN}-%")
                )
            )
