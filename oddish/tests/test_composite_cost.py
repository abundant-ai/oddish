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
from sqlalchemy import insert

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from oddish.core.cost_basis import (  # noqa: E402
    COMBINE_IDEMPOTENCY_PREFIX,
    CompositeCost,
    composite_cost_by_trial,
    is_combine_copy,
)
from oddish.core.endpoints.experiment_cost import (  # noqa: E402
    get_experiment_cost_totals,
)
from oddish.core.endpoints.task_detail import (  # noqa: E402
    _aggregate_task_detail_rollups,
    list_task_versions_core,
)
from oddish.core.endpoints.tasks_query import browse_tasks_core  # noqa: E402
from oddish.core.sharing.public import (  # noqa: E402
    get_public_task_status_core,
    list_public_experiment_tasks_core,
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
    TaskVersionModel,
    TrialModel,
    TrialOrigin,
    TrialStatus,
    utcnow,
)
from oddish.db.models import experiment_trials, task_experiments  # noqa: E402

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


def _trial(
    trial_id: str,
    *,
    cost_usd: float | None,
    model: str = "gpt-5",
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    task_version_id: str | None = None,
    superseded_by_trial_id: str | None = None,
    idempotency_key: str | None = None,
    billed_user_id: str | None = None,
    origin: TrialOrigin = TrialOrigin.ODDISH,
    is_probe: bool = False,
    experiment_id: str = _EXP,
    deleted_at=None,
) -> TrialModel:
    return TrialModel(
        id=trial_id,
        name=trial_id,
        task_id=_TASK,
        experiment_id=experiment_id,
        org_id=_ORG,
        agent="codex",
        provider="openai",
        queue_key=f"openai/{model}",
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        is_probe=is_probe,
        origin=origin,
        attempts=1,
        max_attempts=6,
        status=TrialStatus.SUCCESS,
        cost_usd=cost_usd,
        has_trajectory=False,
        task_version_id=task_version_id,
        superseded_by_trial_id=superseded_by_trial_id,
        idempotency_key=idempotency_key,
        billed_user_id=billed_user_id,
        deleted_at=deleted_at,
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


# --- Task-detail rollup (Slice 2): per-task composite fold + exclusion ---------

_V1 = f"{_TASK}-v1"
_BILLED_USER = f"user-{_RUN}"
# Two trials the detail view folds in (one billed), plus a superseded retry and a
# combine copy that get_task_detail_core filters out *before* the composite is
# batched -- so their ledger rows must contribute $0.
_KEEP_BILLED = f"{_TASK}-keep-billed"
_KEEP_PLAIN = f"{_TASK}-keep-plain"
_SUPERSEDED = f"{_TASK}-superseded"
_COMBINE = f"{_TASK}-combine"


@pytest.mark.asyncio
async def test_task_detail_rollup_folds_composite_and_excludes_filtered(session):
    """The task-detail rollup sums QA + compute over the same population as
    inference, and a filtered-out trial contributes $0 QA/compute.

    Exercises the real flow `get_task_detail_core` runs: filter combine
    copies / superseded retries, batch `composite_cost_by_trial` over only the
    survivors, build their `TrialResponse` rows, then fold via
    `_aggregate_task_detail_rollups`. The excluded trials carry fat ledgers that
    must never reach the totals.
    """
    task = TaskModel(
        id=_TASK,
        name=_TASK,
        org_id=_ORG,
        user="tester",
        task_path="/tmp/composite-cost-test",
        status=TaskStatus.COMPLETED,
    )
    session.add(ExperimentModel(id=_EXP, name=_EXP, org_id=_ORG))
    session.add(task)
    # Flush the task before the version: tasks.current_version_id <-> task
    # _versions.task_id is a cyclic FK, so let the task land first.
    await session.flush()
    session.add(
        TaskVersionModel(
            id=_V1, task_id=_TASK, version=1, task_path="/tmp/composite-cost-test"
        )
    )
    keep_billed = _trial(
        _KEEP_BILLED, cost_usd=0.50, task_version_id=_V1, billed_user_id=_BILLED_USER
    )
    keep_plain = _trial(_KEEP_PLAIN, cost_usd=0.25, task_version_id=_V1)
    session.add_all([keep_billed, keep_plain])
    # Flush the survivors first so the superseded FK (-> trials.id) resolves.
    await session.flush()

    superseded = _trial(
        _SUPERSEDED,
        cost_usd=1.00,
        task_version_id=_V1,
        superseded_by_trial_id=_KEEP_BILLED,
    )
    combine = _trial(
        _COMBINE,
        cost_usd=2.00,
        task_version_id=_V1,
        idempotency_key=f"{COMBINE_IDEMPOTENCY_PREFIX}{_KEEP_PLAIN}",
    )
    session.add_all([superseded, combine])
    session.add_all(
        [
            # keep_billed: QA 0.10 + 0.05 = 0.15; compute 0.20 + 0.30 = 0.50.
            _qa(_KEEP_BILLED, 0.10),
            _qa(_KEEP_BILLED, 0.05),
            _span(_KEEP_BILLED, Decimal("0.20"), 0),
            _span(_KEEP_BILLED, Decimal("0.30"), 1),
            # keep_plain: QA 0.07; no compute.
            _qa(_KEEP_PLAIN, 0.07),
            # Excluded ledgers -- must NOT reach the rollup.
            _qa(_SUPERSEDED, 0.99),
            _span(_SUPERSEDED, Decimal("0.88"), 0),
            _qa(_COMBINE, 0.77),
            _span(_COMBINE, Decimal("0.66"), 0),
        ]
    )
    await session.flush()

    # Mirror get_task_detail_core: drop superseded + combine copies, then batch
    # the composite over only the survivors and build their responses.
    all_trials = [keep_billed, keep_plain, superseded, combine]
    folded = [
        t
        for t in all_trials
        if t.superseded_by_trial_id is None and not is_combine_copy(t)
    ]
    assert {t.id for t in folded} == {_KEEP_BILLED, _KEEP_PLAIN}
    composite = await composite_cost_by_trial(session, [t.id for t in folded])
    responses = [
        build_trial_response(t, _TASK, composite=composite.get(t.id)) for t in folded
    ]
    billed_ids = {t.id for t in folded if t.billed_user_id is not None}
    version_rows = await list_task_versions_core(session, task_id=_TASK, task=task)

    totals, versions = _aggregate_task_detail_rollups(
        trials=responses,
        version_rows=version_rows,
        current_version_id=_V1,
        billed_trial_ids=billed_ids,
    )

    # Inference fold is unchanged (0.50 + 0.25); QA and compute sum over the same
    # two survivors -- NOT the excluded 0.99/0.77 QA or 0.88/0.66 compute.
    assert totals.cost_usd == pytest.approx(0.75)
    assert totals.qa_cost_usd == pytest.approx(0.22)
    assert totals.compute_cost_usd == pytest.approx(0.50)
    assert totals.total_cost_usd == pytest.approx(0.75 + 0.22 + 0.50)  # 1.47

    # Had the filtered trials leaked, QA would be 0.22 + 1.76 and compute
    # 0.50 + 1.54. Pin the exclusion explicitly.
    assert totals.qa_cost_usd != pytest.approx(0.22 + 0.99 + 0.77)
    assert totals.compute_cost_usd != pytest.approx(0.50 + 0.88 + 0.66)

    # Billed split: only the billed survivor contributes, mirroring billed_cost_usd.
    assert totals.billed_cost_usd == pytest.approx(0.50)
    assert totals.billed_qa_cost_usd == pytest.approx(0.15)
    assert totals.billed_compute_cost_usd == pytest.approx(0.50)
    assert totals.billed_total_cost_usd == pytest.approx(0.50 + 0.15 + 0.50)  # 1.15

    # Per-version bucket: all survivors are v1, so it mirrors the task totals.
    assert [v.id for v in versions] == [_V1]
    v1 = versions[0]
    assert v1.cost_usd == pytest.approx(0.75)
    assert v1.qa_cost_usd == pytest.approx(0.22)
    assert v1.compute_cost_usd == pytest.approx(0.50)
    assert v1.total_cost_usd == pytest.approx(1.47)
    assert v1.billed_qa_cost_usd == pytest.approx(0.15)
    assert v1.billed_compute_cost_usd == pytest.approx(0.50)
    assert v1.billed_total_cost_usd == pytest.approx(1.15)


# --- Task-browser list (Slice 3): per-task composite on the /tasks grid --------

# Browser eligibility differs from task-detail: it does NOT apply the first_party
# filter, so IMPORTED trials are counted. It excludes only superseded retries,
# probes, and combine copies. The composite (QA + compute) must mirror exactly
# that inference population: import counted, the excluded three at $0.
_BR_PLAIN = f"{_TASK}-br-plain"
_BR_BILLED = f"{_TASK}-br-billed"
_BR_IMPORT = f"{_TASK}-br-import"
_BR_SUPERSEDED = f"{_TASK}-br-superseded"
_BR_COMBINE = f"{_TASK}-br-combine"
_BR_PROBE = f"{_TASK}-br-probe"


@pytest.mark.asyncio
async def test_browse_tasks_core_folds_composite_and_counts_imports(session):
    """``browse_tasks_core`` reports per-task QA + compute over exactly the
    trials whose inference it folds: an IMPORTED trial is counted, while a
    superseded retry, a probe, and a combine copy contribute $0.

    Runs the real endpoint so the composite fold rides the browser's own trial
    query (its distinct eligibility rule), not a re-derived filter.
    """
    task = TaskModel(
        id=_TASK,
        name=_TASK,
        org_id=_ORG,
        user="tester",
        task_path="/tmp/composite-cost-test",
        status=TaskStatus.COMPLETED,
    )
    session.add(ExperimentModel(id=_EXP, name=_EXP, org_id=_ORG))
    session.add(task)
    # tasks.current_version_id <-> task_versions.task_id is a cyclic FK: land the
    # task, then the version, then point the task at it (the browser folds cost
    # for current-version trials, so current_version_id must be set).
    await session.flush()
    session.add(
        TaskVersionModel(
            id=_V1, task_id=_TASK, version=1, task_path="/tmp/composite-cost-test"
        )
    )
    await session.flush()
    task.current_version_id = _V1

    plain = _trial(_BR_PLAIN, cost_usd=0.25, task_version_id=_V1)
    billed = _trial(
        _BR_BILLED, cost_usd=0.50, task_version_id=_V1, billed_user_id=_BILLED_USER
    )
    imported = _trial(
        _BR_IMPORT,
        cost_usd=0.40,
        task_version_id=_V1,
        origin=TrialOrigin.IMPORTED,
    )
    session.add_all([plain, billed, imported])
    # Flush the survivors first so the superseded FK (-> trials.id) resolves.
    await session.flush()

    superseded = _trial(
        _BR_SUPERSEDED,
        cost_usd=1.00,
        task_version_id=_V1,
        superseded_by_trial_id=_BR_BILLED,
    )
    combine = _trial(
        _BR_COMBINE,
        cost_usd=2.00,
        task_version_id=_V1,
        idempotency_key=f"{COMBINE_IDEMPOTENCY_PREFIX}{_BR_PLAIN}",
    )
    probe = _trial(
        _BR_PROBE,
        cost_usd=3.00,
        task_version_id=_V1,
        is_probe=True,
    )
    session.add_all([superseded, combine, probe])
    session.add_all(
        [
            # plain: QA 0.07; no compute.
            _qa(_BR_PLAIN, 0.07),
            # billed: QA 0.10 + 0.05 = 0.15; compute 0.20 + 0.30 = 0.50.
            _qa(_BR_BILLED, 0.10),
            _qa(_BR_BILLED, 0.05),
            _span(_BR_BILLED, Decimal("0.20"), 0),
            _span(_BR_BILLED, Decimal("0.30"), 1),
            # imported: QA 0.03; compute 0.11 -- MUST be counted (imports count).
            _qa(_BR_IMPORT, 0.03),
            _span(_BR_IMPORT, Decimal("0.11"), 0),
            # Excluded ledgers -- must NOT reach the card.
            _qa(_BR_SUPERSEDED, 0.99),
            _span(_BR_SUPERSEDED, Decimal("0.88"), 0),
            _qa(_BR_COMBINE, 0.77),
            _span(_BR_COMBINE, Decimal("0.66"), 0),
            _qa(_BR_PROBE, 0.55),
            _span(_BR_PROBE, Decimal("0.44"), 0),
        ]
    )
    await session.flush()

    response = await browse_tasks_core(session, org_id=_ORG)
    items = [item for item in response.items if item.id == _TASK]
    assert len(items) == 1
    item = items[0]

    # Inference: plain + billed + import (0.25 + 0.50 + 0.40); excluded three out.
    assert item.cost_usd == pytest.approx(1.15)
    assert item.cost_trial_count == 3

    # QA and compute mirror that same population, IMPORT included.
    assert item.qa_cost_usd == pytest.approx(0.07 + 0.15 + 0.03)  # 0.25
    assert item.compute_cost_usd == pytest.approx(0.50 + 0.11)  # 0.61
    assert item.total_cost_usd == pytest.approx(1.15 + 0.25 + 0.61)  # 2.01

    # Had any excluded trial leaked, these would jump by its 0.99/0.77/0.55 QA or
    # 0.88/0.66/0.44 compute. Pin the exclusion explicitly.
    assert item.qa_cost_usd != pytest.approx(0.25 + 0.99)
    assert item.qa_cost_usd != pytest.approx(0.25 + 0.77)
    assert item.qa_cost_usd != pytest.approx(0.25 + 0.55)
    assert item.compute_cost_usd != pytest.approx(0.61 + 0.88)

    # Billed split: only the billed survivor contributes, mirroring billed_cost_usd.
    assert item.billed_cost_usd == pytest.approx(0.50)
    assert item.billed_qa_cost_usd == pytest.approx(0.15)
    assert item.billed_compute_cost_usd == pytest.approx(0.50)
    assert item.billed_total_cost_usd == pytest.approx(0.50 + 0.15 + 0.50)  # 1.15


# --- Whole-experiment rollup (Slice 4): composite fold over membership ----------

# Experiment eligibility differs from task-detail AND the browser: the population
# is trial_in_experiment MEMBERSHIP under include_deleted (no probe/superseded
# filter), split owned (homed here) vs gathered, with billed a subset of owned.
# QA + compute must fold over exactly that inference population: a deleted-but-
# member trial still contributes (its inference is counted under include_deleted),
# a gathered trial counts toward cost but not owned, and only owned+billed feeds
# the billed subset.
_EXP_OTHER = f"composite-exp-other-{_RUN}"
_XP_OWNED_BILLED = f"{_TASK}-xp-owned-billed"
_XP_OWNED_PLAIN = f"{_TASK}-xp-owned-plain"
_XP_OWNED_DELETED = f"{_TASK}-xp-owned-deleted"
_XP_GATHERED = f"{_TASK}-xp-gathered"


@pytest.mark.asyncio
async def test_experiment_totals_fold_composite_over_membership(session):
    """``get_experiment_cost_totals`` folds QA + compute over the same member
    population as inference, with the owned/billed split and include_deleted
    membership mirrored exactly.

    Members: two owned trials (one billed), an owned trial that was soft-deleted
    (still counted, like its inference under include_deleted), and a trial homed
    in another experiment but gathered in (counts toward cost, not owned). Each
    carries analysis_costs + modal_costs; a soft-deleted QA row and an open
    (NULL-cost) compute span on the billed trial must be excluded.
    """
    session.add(ExperimentModel(id=_EXP, name=_EXP, org_id=_ORG))
    session.add(ExperimentModel(id=_EXP_OTHER, name=_EXP_OTHER, org_id=_ORG))
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

    owned_billed = _trial(_XP_OWNED_BILLED, cost_usd=0.50, billed_user_id=_BILLED_USER)
    owned_plain = _trial(_XP_OWNED_PLAIN, cost_usd=0.25)
    owned_deleted = _trial(_XP_OWNED_DELETED, cost_usd=0.40, deleted_at=utcnow())
    gathered = _trial(_XP_GATHERED, cost_usd=0.60, experiment_id=_EXP_OTHER)
    session.add_all([owned_billed, owned_plain, owned_deleted, gathered])
    await session.flush()

    # Gather the other experiment's trial into _EXP (membership, not re-home).
    await session.execute(
        insert(experiment_trials).values(
            [{"experiment_id": _EXP, "trial_id": gathered.id}]
        )
    )

    session.add_all(
        [
            # owned_billed: QA 0.10 + 0.05 = 0.15 (one deleted row excluded);
            # compute 0.20 + 0.30 = 0.50 (one open/unpriced span excluded).
            _qa(_XP_OWNED_BILLED, 0.10),
            _qa(_XP_OWNED_BILLED, 0.05),
            _qa(_XP_OWNED_BILLED, 0.99, deleted=True),
            _span(_XP_OWNED_BILLED, Decimal("0.20"), 0),
            _span(_XP_OWNED_BILLED, Decimal("0.30"), 1),
            _span(_XP_OWNED_BILLED, None, 2),
            # owned_plain: QA 0.07; compute 0.11.
            _qa(_XP_OWNED_PLAIN, 0.07),
            _span(_XP_OWNED_PLAIN, Decimal("0.11"), 0),
            # owned_deleted: QA 0.03; compute 0.09 -- counted (member under
            # include_deleted), owned, but not billed.
            _qa(_XP_OWNED_DELETED, 0.03),
            _span(_XP_OWNED_DELETED, Decimal("0.09"), 0),
            # gathered: QA 0.02; compute 0.08 -- counted toward cost, NOT owned.
            _qa(_XP_GATHERED, 0.02),
            _span(_XP_GATHERED, Decimal("0.08"), 0),
        ]
    )
    await session.flush()

    totals = await get_experiment_cost_totals(session, experiment_id=_EXP, org_id=_ORG)

    # Inference (unchanged): every member trial, deleted + gathered included.
    assert totals.cost_usd == pytest.approx(0.50 + 0.25 + 0.40 + 0.60)  # 1.75
    assert totals.total_trials == 4

    # Member-wide composite folds QA/compute over that same population.
    assert totals.qa_cost_usd == pytest.approx(0.15 + 0.07 + 0.03 + 0.02)  # 0.27
    assert totals.compute_cost_usd == pytest.approx(0.50 + 0.11 + 0.09 + 0.08)  # 0.78
    assert totals.total_cost_usd == pytest.approx(1.75 + 0.27 + 0.78)  # 2.80

    # Owned (homed here) excludes the gathered trial's QA/compute.
    assert totals.owned_cost_usd == pytest.approx(0.50 + 0.25 + 0.40)  # 1.15
    assert totals.owned_qa_cost_usd == pytest.approx(0.15 + 0.07 + 0.03)  # 0.25
    assert totals.owned_compute_cost_usd == pytest.approx(0.50 + 0.11 + 0.09)  # 0.70
    assert totals.owned_total_cost_usd == pytest.approx(1.15 + 0.25 + 0.70)  # 2.10

    # Billed is the subset of owned carrying a billed_user_id (only owned_billed).
    assert totals.billed_cost_usd == pytest.approx(0.50)
    assert totals.billed_qa_cost_usd == pytest.approx(0.15)
    assert totals.billed_compute_cost_usd == pytest.approx(0.50)
    assert totals.billed_total_cost_usd == pytest.approx(0.50 + 0.15 + 0.50)  # 1.15

    # Had the excluded ledger rows leaked, QA would carry the deleted 0.99 and
    # compute the open span. Pin the exclusion.
    assert totals.qa_cost_usd != pytest.approx(0.27 + 0.99)
    assert totals.owned_compute_cost_usd == pytest.approx(0.70)


# --- Public share list (Slice 5): composite per trial on the share grid --------

# The public experiment grid (share view) and its trial drawer render the
# composite total via the shared ``sumTaskTrialCost`` client aggregator. That
# only works if the backing ``list_public_experiment_tasks`` endpoint stamps
# qa/compute per trial. Pin that the extracted core threads the batched composite
# into every emitted trial (this path was unwired before Slice 5).
_PUBLIC_TOKEN = f"composite-share-{_RUN}"
_PUB_FULL = f"{_TASK}-pub-full"
_PUB_QA = f"{_TASK}-pub-qa"
_PUB_BARE = f"{_TASK}-pub-bare"


@pytest.mark.asyncio
async def test_public_experiment_tasks_emit_composite_per_trial(session):
    """``list_public_experiment_tasks_core`` stamps qa/compute/total on every
    emitted trial, so the share grid's per-task cost badge and the trial drawer's
    task rollup render the composite total rather than inference alone.
    """
    session.add(
        ExperimentModel(
            id=_EXP,
            name=_EXP,
            org_id=_ORG,
            is_public=True,
            public_token=_PUBLIC_TOKEN,
        )
    )
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
    session.add_all(
        [
            _trial(_PUB_FULL, cost_usd=0.50),  # inference + QA + compute
            _trial(_PUB_QA, cost_usd=0.25),  # inference + QA only
            _trial(_PUB_BARE, cost_usd=0.10),  # inference only (no ledger rows)
        ]
    )
    await session.flush()

    # The share grid joins tasks through task_experiments, so link the task into
    # the public experiment (membership, mirroring a real shared experiment).
    await session.execute(
        insert(task_experiments).values([{"task_id": _TASK, "experiment_id": _EXP}])
    )
    session.add_all(
        [
            _qa(_PUB_FULL, 0.20),
            _span(_PUB_FULL, Decimal("0.30"), 0),
            _qa(_PUB_QA, 0.05),
        ]
    )
    await session.flush()

    responses = await list_public_experiment_tasks_core(session, _PUBLIC_TOKEN)

    assert len(responses) == 1
    trials = {t.id: t for t in responses[0].trials}
    assert set(trials) == {_PUB_FULL, _PUB_QA, _PUB_BARE}

    # Full ledger: inference 0.50 + QA 0.20 + compute 0.30 = 1.00.
    full = trials[_PUB_FULL]
    assert full.cost_usd == pytest.approx(0.50)
    assert full.qa_cost_usd == pytest.approx(0.20)
    assert full.compute_cost_usd == pytest.approx(0.30)
    assert full.total_cost_usd == pytest.approx(1.00)

    # QA only: compute defaults to 0.0; total folds inference + QA (0.25 + 0.05).
    qa_only = trials[_PUB_QA]
    assert qa_only.qa_cost_usd == pytest.approx(0.05)
    assert qa_only.compute_cost_usd == pytest.approx(0.0)
    assert qa_only.total_cost_usd == pytest.approx(0.30)

    # No ledger rows: qa/compute are 0.0 and total == inference. Crucially the
    # fields are populated (not None) — the endpoint supplies a composite for
    # every trial — so the client aggregator sums a real total, not a fallback.
    bare = trials[_PUB_BARE]
    assert bare.qa_cost_usd == pytest.approx(0.0)
    assert bare.compute_cost_usd == pytest.approx(0.0)
    assert bare.total_cost_usd == pytest.approx(0.10)


# --- Public single-task refetch (Bugbot follow-up): composite on refetch -------

# ``get_public_task_status`` is the single-task refetch a share view hits after
# navigating to one task. It must stamp qa/compute/total per trial exactly as the
# list path does, or the share view silently drops to inference-only (the fields
# come back null) after navigation. Pin that the extracted core threads the
# batched composite into every emitted trial.
_PUBLIC_TOKEN_ONE = f"composite-share-one-{_RUN}"
_PT_FULL = f"{_TASK}-pt-full"
_PT_QA = f"{_TASK}-pt-qa"
_PT_BARE = f"{_TASK}-pt-bare"


@pytest.mark.asyncio
async def test_get_public_task_status_emits_composite_per_trial(session):
    """``get_public_task_status_core`` (single public task refetch) stamps
    qa/compute/total on every emitted trial, mirroring the list path so a share
    view keeps the composite total after navigating to one task rather than
    dropping back to inference-only.
    """
    session.add(
        ExperimentModel(
            id=_EXP,
            name=_EXP,
            org_id=_ORG,
            is_public=True,
            public_token=_PUBLIC_TOKEN_ONE,
        )
    )
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
    session.add_all(
        [
            _trial(_PT_FULL, cost_usd=0.50),  # inference + QA + compute
            _trial(_PT_QA, cost_usd=0.25),  # inference + QA only
            _trial(_PT_BARE, cost_usd=0.10),  # inference only (no ledger rows)
        ]
    )
    await session.flush()

    # Expose the task through the share token via task_experiments membership,
    # mirroring a real shared experiment (the single-task fetch resolves the task
    # only through that membership).
    await session.execute(
        insert(task_experiments).values([{"task_id": _TASK, "experiment_id": _EXP}])
    )
    session.add_all(
        [
            _qa(_PT_FULL, 0.20),
            _span(_PT_FULL, Decimal("0.30"), 0),
            _qa(_PT_QA, 0.05),
        ]
    )
    await session.flush()

    response = await get_public_task_status_core(session, _PUBLIC_TOKEN_ONE, _TASK)

    trials = {t.id: t for t in (response.trials or [])}
    assert set(trials) == {_PT_FULL, _PT_QA, _PT_BARE}

    # Full ledger: inference 0.50 + QA 0.20 + compute 0.30 = 1.00.
    full = trials[_PT_FULL]
    assert full.cost_usd == pytest.approx(0.50)
    assert full.qa_cost_usd == pytest.approx(0.20)
    assert full.compute_cost_usd == pytest.approx(0.30)
    assert full.total_cost_usd == pytest.approx(1.00)

    # QA only: compute defaults to 0.0; total folds inference + QA (0.25 + 0.05).
    qa_only = trials[_PT_QA]
    assert qa_only.qa_cost_usd == pytest.approx(0.05)
    assert qa_only.compute_cost_usd == pytest.approx(0.0)
    assert qa_only.total_cost_usd == pytest.approx(0.30)

    # No ledger rows: qa/compute are 0.0 and total == inference. Crucially the
    # fields are populated (not None) — the refetch supplies a composite for every
    # trial — so the client aggregator sums a real total, not a fallback.
    bare = trials[_PT_BARE]
    assert bare.qa_cost_usd == pytest.approx(0.0)
    assert bare.compute_cost_usd == pytest.approx(0.0)
    assert bare.total_cost_usd == pytest.approx(0.10)


# --- Count consistency (Bugbot follow-up): composite-only trials count too ------

# A billed trial with $0 priced inference but real QA/compute must still count
# toward cost_trial_count AND billed_trial_count in every rollup, so "N (billed)
# trials" never contradicts a shown composite total. A member trial with no cost
# at all (no inference, no QA, no compute) must NOT be counted. These pin the fix
# in all three rollups: task-detail, the browser card, and the experiment tile.
_CC_BILLED_COMPOSITE = f"{_TASK}-cc-billed-composite"
_CC_ZERO = f"{_TASK}-cc-zero"


def _cc_ledger_rows() -> list:
    """QA (0.10 + 0.05 = 0.15) + compute (0.20 + 0.30 = 0.50) for the billed,
    $0-inference trial shared by all three count-consistency tests."""
    return [
        _qa(_CC_BILLED_COMPOSITE, 0.10),
        _qa(_CC_BILLED_COMPOSITE, 0.05),
        _span(_CC_BILLED_COMPOSITE, Decimal("0.20"), 0),
        _span(_CC_BILLED_COMPOSITE, Decimal("0.30"), 1),
    ]


@pytest.mark.asyncio
async def test_task_detail_rollup_counts_composite_only_billed_trial(session):
    """Task-detail: a $0-inference billed trial with QA + compute increments
    cost_trial_count and billed_trial_count (and feeds total/billed_total), while
    a no-cost trial is not counted -- so the count never contradicts the total.
    """
    task = TaskModel(
        id=_TASK,
        name=_TASK,
        org_id=_ORG,
        user="tester",
        task_path="/tmp/composite-cost-test",
        status=TaskStatus.COMPLETED,
    )
    session.add(ExperimentModel(id=_EXP, name=_EXP, org_id=_ORG))
    session.add(task)
    await session.flush()
    session.add(
        TaskVersionModel(
            id=_V1, task_id=_TASK, version=1, task_path="/tmp/composite-cost-test"
        )
    )
    billed_composite = _trial(
        _CC_BILLED_COMPOSITE,
        cost_usd=None,
        task_version_id=_V1,
        billed_user_id=_BILLED_USER,
    )
    zero = _trial(_CC_ZERO, cost_usd=None, task_version_id=_V1)
    session.add_all([billed_composite, zero])
    session.add_all(_cc_ledger_rows())
    await session.flush()

    folded = [billed_composite, zero]
    composite = await composite_cost_by_trial(session, [t.id for t in folded])
    responses = [
        build_trial_response(t, _TASK, composite=composite.get(t.id)) for t in folded
    ]
    version_rows = await list_task_versions_core(session, task_id=_TASK, task=task)
    totals, versions = _aggregate_task_detail_rollups(
        trials=responses,
        version_rows=version_rows,
        current_version_id=_V1,
        billed_trial_ids={_CC_BILLED_COMPOSITE},
    )

    # No priced inference at all, but real composite spend.
    assert totals.cost_usd == pytest.approx(0.0)
    assert totals.qa_cost_usd == pytest.approx(0.15)
    assert totals.compute_cost_usd == pytest.approx(0.50)
    assert totals.total_cost_usd == pytest.approx(0.65)
    # The composite-only trial is counted; the no-cost trial is not.
    assert totals.total_trials == 2
    assert totals.cost_trial_count == 1
    # Billed count + total include the composite-only billed trial. The bug this
    # closes: the count stayed 0 while billed_total_cost_usd was 0.65.
    assert totals.billed_trial_count == 1
    assert totals.billed_qa_cost_usd == pytest.approx(0.15)
    assert totals.billed_compute_cost_usd == pytest.approx(0.50)
    assert totals.billed_total_cost_usd == pytest.approx(0.65)
    # Per-version bucket mirrors the task totals.
    v1 = versions[0]
    assert v1.cost_trial_count == 1
    assert v1.billed_trial_count == 1
    assert v1.total_cost_usd == pytest.approx(0.65)
    assert v1.billed_total_cost_usd == pytest.approx(0.65)


@pytest.mark.asyncio
async def test_browse_tasks_core_counts_composite_only_billed_trial(session):
    """Browser card: a $0-inference billed trial with QA + compute counts toward
    cost_trial_count and billed_trial_count (and folds its spend into the card
    total), while a no-cost trial is not counted.
    """
    task = TaskModel(
        id=_TASK,
        name=_TASK,
        org_id=_ORG,
        user="tester",
        task_path="/tmp/composite-cost-test",
        status=TaskStatus.COMPLETED,
    )
    session.add(ExperimentModel(id=_EXP, name=_EXP, org_id=_ORG))
    session.add(task)
    await session.flush()
    session.add(
        TaskVersionModel(
            id=_V1, task_id=_TASK, version=1, task_path="/tmp/composite-cost-test"
        )
    )
    await session.flush()
    task.current_version_id = _V1

    billed_composite = _trial(
        _CC_BILLED_COMPOSITE,
        cost_usd=None,
        task_version_id=_V1,
        billed_user_id=_BILLED_USER,
    )
    zero = _trial(_CC_ZERO, cost_usd=None, task_version_id=_V1)
    session.add_all([billed_composite, zero])
    session.add_all(_cc_ledger_rows())
    await session.flush()

    response = await browse_tasks_core(session, org_id=_ORG)
    items = [item for item in response.items if item.id == _TASK]
    assert len(items) == 1
    item = items[0]

    assert item.cost_usd == pytest.approx(0.0)
    assert item.qa_cost_usd == pytest.approx(0.15)
    assert item.compute_cost_usd == pytest.approx(0.50)
    assert item.total_cost_usd == pytest.approx(0.65)
    # Composite-only trial counted; no-cost trial excluded.
    assert item.cost_trial_count == 1
    assert item.billed_trial_count == 1
    assert item.billed_qa_cost_usd == pytest.approx(0.15)
    assert item.billed_compute_cost_usd == pytest.approx(0.50)
    assert item.billed_total_cost_usd == pytest.approx(0.65)


@pytest.mark.asyncio
async def test_experiment_totals_count_composite_only_billed_trial(session):
    """Experiment tile: a $0-inference owned+billed member trial with QA + compute
    increments cost_trial_count, owned_trial_count, and billed_trial_count, so
    "N trials" never contradicts the composite total; a no-cost member is not
    counted.
    """
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
    billed_composite = _trial(
        _CC_BILLED_COMPOSITE, cost_usd=None, billed_user_id=_BILLED_USER
    )
    zero = _trial(_CC_ZERO, cost_usd=None)
    session.add_all([billed_composite, zero])
    session.add_all(_cc_ledger_rows())
    await session.flush()

    totals = await get_experiment_cost_totals(session, experiment_id=_EXP, org_id=_ORG)

    # Two members, neither with priced inference; one contributes composite spend.
    assert totals.total_trials == 2
    assert totals.cost_usd == pytest.approx(0.0)
    assert totals.qa_cost_usd == pytest.approx(0.15)
    assert totals.compute_cost_usd == pytest.approx(0.50)
    assert totals.total_cost_usd == pytest.approx(0.65)
    # The composite-only member is counted in all three scopes; the no-cost
    # member is not. The bug this closes: the counts stayed 0 (priced-only) while
    # total_cost_usd / billed_total_cost_usd were 0.65.
    assert totals.cost_trial_count == 1
    assert totals.owned_trial_count == 1
    assert totals.billed_trial_count == 1
    assert totals.owned_total_cost_usd == pytest.approx(0.65)
    assert totals.billed_total_cost_usd == pytest.approx(0.65)


# --- Count consistency (Bugbot follow-up): unpriceable-model inference + QA/compute

# The experiment rollup prices inference in pure SQL, which can't do the LiteLLM
# pricing lookup. A member trial that HAS inference tokens (so ``_ESTIMATED_ROW``)
# but on a model absent from the pricing table prices to $0: the grouped fold skips
# it (no priced inference, no priced count), and the composite-only count used to
# skip it too because it is not ``~_ESTIMATED_ROW``. So when such a trial also
# carried QA/compute it was counted nowhere while its QA/compute still landed in
# ``total_*`` -- the count contradicted the total. Pin that it now counts exactly
# once, that a token-but-unpriceable trial with NO QA/compute still counts as
# nothing, and that a priced trial with QA/compute is not double-counted.
_CC_UNPRICEABLE_MODEL = f"totally-unknown-model-{uuid.uuid4().hex[:6]}"
_CC_UNPRICEABLE_COMPOSITE = f"{_TASK}-cc-unpriceable-composite"
_CC_UNPRICEABLE_BARE = f"{_TASK}-cc-unpriceable-bare"
_CC_PRICED_PLUS_LEDGERS = f"{_TASK}-cc-priced-plus-ledgers"


@pytest.mark.asyncio
async def test_experiment_totals_count_unpriceable_estimated_trial_with_qa_compute(
    session,
):
    """A member trial with inference tokens on an UNPRICEABLE model ($0 inference)
    plus QA + compute counts exactly once, in every scope its billing puts it in,
    and its QA/compute lands in ``total_*``.

    This is the gap the grouped rollup left: the fold skips the trial (its model
    doesn't price, so it yields no inference and no priced count), and the
    composite-only count excluded it too because it *is* an ``_ESTIMATED_ROW``. Its
    QA/compute still summed into ``total_*``, so ``cost_trial_count`` contradicted
    the total. Also pinned: a token-but-unpriceable trial with NO QA/compute still
    counts as nothing, and a priced trial that also has QA/compute counts once.
    """
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

    # Unpriceable model + tokens + QA/compute, billed -> must count in all scopes.
    unpriceable_composite = _trial(
        _CC_UNPRICEABLE_COMPOSITE,
        cost_usd=None,
        model=_CC_UNPRICEABLE_MODEL,
        input_tokens=500_000,
        output_tokens=50_000,
        billed_user_id=_BILLED_USER,
    )
    # Same unpriceable model + tokens, but NO QA/compute -> counts as nothing.
    unpriceable_bare = _trial(
        _CC_UNPRICEABLE_BARE,
        cost_usd=None,
        model=_CC_UNPRICEABLE_MODEL,
        input_tokens=500_000,
        output_tokens=50_000,
    )
    # Priced native inference AND QA/compute -> counted once (no double-count).
    priced_plus_ledgers = _trial(_CC_PRICED_PLUS_LEDGERS, cost_usd=0.40)
    session.add_all([unpriceable_composite, unpriceable_bare, priced_plus_ledgers])
    session.add_all(
        [
            # unpriceable_composite: QA 0.10 + 0.05 = 0.15; compute 0.20 + 0.30 = 0.50.
            _qa(_CC_UNPRICEABLE_COMPOSITE, 0.10),
            _qa(_CC_UNPRICEABLE_COMPOSITE, 0.05),
            _span(_CC_UNPRICEABLE_COMPOSITE, Decimal("0.20"), 0),
            _span(_CC_UNPRICEABLE_COMPOSITE, Decimal("0.30"), 1),
            # priced_plus_ledgers: QA 0.07; compute 0.11.
            _qa(_CC_PRICED_PLUS_LEDGERS, 0.07),
            _span(_CC_PRICED_PLUS_LEDGERS, Decimal("0.11"), 0),
        ]
    )
    await session.flush()

    totals = await get_experiment_cost_totals(session, experiment_id=_EXP, org_id=_ORG)

    # Three members; only the priced trial has native inference. The two unpriceable
    # trials price to $0 -- no inference dollars, no estimated flag.
    assert totals.total_trials == 3
    assert totals.cost_usd == pytest.approx(0.40)
    assert totals.cost_has_native is True
    assert totals.cost_has_estimated is False

    # QA/compute sum over the whole member population, unpriceable trial included.
    assert totals.qa_cost_usd == pytest.approx(0.15 + 0.07)  # 0.22
    assert totals.compute_cost_usd == pytest.approx(0.50 + 0.11)  # 0.61
    assert totals.total_cost_usd == pytest.approx(0.40 + 0.22 + 0.61)  # 1.23

    # The count now mirrors the population feeding total_*: the priced trial AND the
    # unpriceable-but-QA/compute trial each count once; the bare unpriceable trial
    # (no QA/compute) counts as nothing. == 2 (not 3) also pins that the priced
    # trial with QA/compute is not double-counted.
    assert totals.cost_trial_count == 2
    assert totals.owned_trial_count == 2
    # Only unpriceable_composite is billed; it counts despite $0 priced inference.
    assert totals.billed_trial_count == 1

    # Billed scope: $0 inference (unpriceable) but its QA/compute feed billed_total.
    assert totals.billed_cost_usd == pytest.approx(0.0)
    assert totals.billed_qa_cost_usd == pytest.approx(0.15)
    assert totals.billed_compute_cost_usd == pytest.approx(0.50)
    assert totals.billed_total_cost_usd == pytest.approx(0.65)
    # Owned == member here (all three homed in _EXP).
    assert totals.owned_total_cost_usd == pytest.approx(1.23)
