"""DB-backed test for the per-trial composite (inference + QA + compute) cost.

``composite_cost_by_trial`` batches two grouped ledger queries -- QA over
``analysis_costs`` and compute over ``modal_costs`` -- and must:

* return a ``CompositeCost`` for *every* requested trial id (0.0 when a ledger
  has no rows),
* exclude soft-deleted ledger rows and open/unpriced compute spans
  (``modal_costs.cost_usd IS NULL``),
* keep each trial's sums isolated (no cross-trial leakage).

The emit-site combination (``total = cost_usd + qa + compute``) is checked
through ``build_trial_response`` so the wired-up ``TrialResponse`` contract is
pinned, not just the helper.

Uses the rollback-per-test ``session`` fixture (see ``conftest.py``) against the
local Postgres, with run-scoped ids so each test sees only its own rows.
"""

from __future__ import annotations

import sys
import uuid
from decimal import Decimal
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from oddish.core.cost_basis import (  # noqa: E402
    CompositeCost,
    composite_cost_by_trial,
)
from oddish.core.helpers import (  # noqa: E402
    _composite_cost_fields,
    build_trial_response,
)
from oddish.db import (  # noqa: E402
    AnalysisCostModel,
    ExperimentModel,
    ModalCostSpanModel,
    TaskModel,
    TaskStatus,
    TrialModel,
    TrialStatus,
    utcnow,
)

_RUN = uuid.uuid4().hex[:8]
_ORG = f"composite-org-{_RUN}"
_EXP = f"composite-exp-{_RUN}"
_TASK = f"composite-task-{_RUN}"
# T1: full ledger (inference + QA + compute); T2: QA only; T3: inference only.
_T1 = f"{_TASK}-0"
_T2 = f"{_TASK}-1"
_T3 = f"{_TASK}-2"

_T1_INFERENCE = 0.50
_T2_INFERENCE = 0.25
_T3_INFERENCE = 0.10


def _trial(trial_id: str, *, cost_usd: float) -> TrialModel:
    return TrialModel(
        id=trial_id,
        name=trial_id,
        task_id=_TASK,
        experiment_id=_EXP,
        org_id=_ORG,
        agent="codex",
        provider="openai",
        queue_key="openai/gpt-5",
        model="gpt-5",
        is_probe=False,
        attempts=1,
        max_attempts=6,
        status=TrialStatus.SUCCESS,
        cost_usd=cost_usd,
        has_trajectory=False,
    )


def _qa(trial_id: str, cost: float, *, deleted: bool = False) -> AnalysisCostModel:
    return AnalysisCostModel(
        job_kind="classify_trial",
        trial_id=trial_id,
        experiment_id=_EXP,
        org_id=_ORG,
        model="gpt-5",
        cost_usd=cost,
        cost_source="native",
        deleted_at=utcnow() if deleted else None,
    )


def _span(
    trial_id: str,
    cost: Decimal | None,
    ordinal: int,
    *,
    deleted: bool = False,
) -> ModalCostSpanModel:
    started = utcnow()
    return ModalCostSpanModel(
        trial_id=trial_id,
        experiment_id=_EXP,
        org_id=_ORG,
        component_role="agent_sandbox",
        span_ordinal=ordinal,
        provider="modal",
        started_at=started,
        # NULL cost => open/unpriced span; leave it open (finished_at NULL).
        finished_at=started if cost is not None else None,
        basis="hooks",
        spec_source="pinned",
        cost_usd=cost,
        cost_source="estimated",
        deleted_at=utcnow() if deleted else None,
    )


@pytest.mark.asyncio
async def test_composite_cost_by_trial_sums_qa_and_compute(session):
    session.add(ExperimentModel(id=_EXP, name=_EXP, org_id=_ORG))
    session.add(
        TaskModel(
            id=_TASK,
            name=_TASK,
            org_id=_ORG,
            user="tester",
            task_path="/tmp/composite-cost-test",
            status=TaskStatus.COMPLETED,
        )
    )
    t1 = _trial(_T1, cost_usd=_T1_INFERENCE)
    session.add_all(
        [t1, _trial(_T2, cost_usd=_T2_INFERENCE), _trial(_T3, cost_usd=_T3_INFERENCE)]
    )

    session.add_all(
        [
            # T1 QA: two counted (0.10 + 0.05 = 0.15), one soft-deleted (excluded).
            _qa(_T1, 0.10),
            _qa(_T1, 0.05),
            _qa(_T1, 0.99, deleted=True),
            # T2 QA: one counted (0.07), no compute.
            _qa(_T2, 0.07),
        ]
    )
    session.add_all(
        [
            # T1 compute: two priced+closed (0.20 + 0.30 = 0.50); one open/unpriced
            # span (NULL cost, excluded) and one soft-deleted priced span (excluded).
            _span(_T1, Decimal("0.20"), 0),
            _span(_T1, Decimal("0.30"), 1),
            _span(_T1, None, 2),
            _span(_T1, Decimal("0.77"), 3, deleted=True),
        ]
    )
    await session.flush()

    result = await composite_cost_by_trial(session, [_T1, _T2, _T3])

    # Every requested id is present, even the one with no ledger rows.
    assert set(result) == {_T1, _T2, _T3}
    assert all(isinstance(v, CompositeCost) for v in result.values())

    # T1: QA excludes the deleted row; compute excludes the open + deleted spans.
    assert result[_T1].qa == pytest.approx(0.15)
    assert result[_T1].compute == pytest.approx(0.50)
    # inference is not recomputed by the helper.
    assert result[_T1].inference == 0.0

    # T2: QA only; compute absent -> 0.0. T3: both absent -> 0.0.
    assert result[_T2].qa == pytest.approx(0.07)
    assert result[_T2].compute == 0.0
    assert result[_T3].qa == 0.0
    assert result[_T3].compute == 0.0

    # Emit-site combination folds the trial's own inference cost_usd back in.
    qa, compute, total = _composite_cost_fields(_T1_INFERENCE, result[_T1])
    assert qa == pytest.approx(0.15)
    assert compute == pytest.approx(0.50)
    assert total == pytest.approx(_T1_INFERENCE + 0.15 + 0.50)  # 1.15

    # End-to-end: the fields land on the TrialResponse via the builder.
    resp = build_trial_response(t1, _TASK, composite=result[_T1])
    assert resp.cost_usd == pytest.approx(_T1_INFERENCE)
    assert resp.qa_cost_usd == pytest.approx(0.15)
    assert resp.compute_cost_usd == pytest.approx(0.50)
    assert resp.total_cost_usd == pytest.approx(1.15)

    # Without a composite the new fields stay None (purely additive).
    bare = build_trial_response(t1, _TASK)
    assert bare.qa_cost_usd is None
    assert bare.compute_cost_usd is None
    assert bare.total_cost_usd is None
    assert bare.cost_usd == pytest.approx(_T1_INFERENCE)


@pytest.mark.asyncio
async def test_composite_cost_by_trial_empty_input(session):
    assert await composite_cost_by_trial(session, []) == {}
