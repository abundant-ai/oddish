from __future__ import annotations

import sys
import uuid
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from oddish.core.admin import get_cost_breakdown_core  # noqa: E402
from oddish.db import ModalCostSpanModel, get_session, utcnow  # noqa: E402

_RUN = uuid.uuid4().hex[:8]


def _breakdown_map(result):
    return {
        row.provider: (row.cost_usd, row.span_count)
        for row in result.compute_by_provider
    }


def _series_totals(result):
    return {
        key.key: sum(
            bucket.costs.get(key.key, 0.0)
            for bucket in result.series_compute_by_provider.buckets
        )
        for key in result.series_compute_by_provider.keys
    }


def _span(
    index: int,
    *,
    provider: str,
    cost_usd: str | None,
    created_at,
    finished_at,
    deleted_at=None,
) -> ModalCostSpanModel:
    return ModalCostSpanModel(
        id=f"modal-dashboard-{_RUN}-{index}",
        worker_job_id=f"modal-dashboard-job-{_RUN}-{index}",
        worker_job_attempt=1,
        attempt=1,
        component_role="agent_sandbox",
        span_ordinal=0,
        provider=provider,
        external_id=f"modal-dashboard-external-{_RUN}-{index}",
        started_at=finished_at - timedelta(minutes=1),
        finished_at=finished_at,
        basis="hooks",
        spec_source="pinned",
        gpu_type=None,
        gpu_count=None,
        price_multiplier=Decimal("1"),
        cost_usd=Decimal(cost_usd) if cost_usd is not None else None,
        cost_source="estimated",
        estimator_version=1,
        created_at=created_at,
        deleted_at=deleted_at,
    )


@pytest.mark.asyncio
async def test_compute_cost_breakdown_empty_table_returns_zero_and_empty_lists():
    async with get_session() as session:
        savepoint = await session.begin_nested()
        await session.execute(ModalCostSpanModel.__table__.delete())

        result = await get_cost_breakdown_core(session, window_days=7)

        assert result.totals.compute_cost_usd == 0.0
        assert result.compute_by_provider == []
        assert result.series_compute_by_provider.dimension == "provider"
        assert result.series_compute_by_provider.keys == []
        assert result.series_compute_by_provider.buckets == []
        assert {key.key for key in result.series_by_type.keys} == {
            "inference",
            "qa",
            "compute",
        }
        await savepoint.rollback()


@pytest.mark.asyncio
async def test_compute_cost_breakdown_uses_finished_at_and_groups_by_provider():
    now = utcnow()
    recent = now - timedelta(hours=1)
    old = now - timedelta(days=40)

    async with get_session() as session:
        baseline = await get_cost_breakdown_core(session, window_days=7)
    baseline_breakdown = _breakdown_map(baseline)
    baseline_series = _series_totals(baseline)

    rows = [
        # Priced, in-window: modal 0.25, daytona 0.75 + 0.25, other (docker) 0.50.
        _span(0, provider="modal", cost_usd="0.25", created_at=old, finished_at=recent),
        _span(
            1,
            provider="daytona",
            cost_usd="0.75",
            created_at=recent,
            finished_at=recent,
        ),
        _span(
            2,
            provider="daytona",
            cost_usd="0.25",
            created_at=recent,
            finished_at=recent,
        ),
        _span(
            6, provider="docker", cost_usd="0.50", created_at=recent, finished_at=recent
        ),
        # Excluded: finished before the window.
        _span(
            3, provider="modal", cost_usd="10.00", created_at=recent, finished_at=old
        ),
        # Excluded: soft-deleted.
        _span(
            4,
            provider="modal",
            cost_usd="20.00",
            created_at=recent,
            finished_at=recent,
            deleted_at=now,
        ),
        # Excluded: unpriced (cost_usd IS NULL).
        _span(
            5, provider="daytona", cost_usd=None, created_at=recent, finished_at=recent
        ),
    ]

    async with get_session() as session:
        session.add_all(rows)

    try:
        async with get_session() as session:
            result = await get_cost_breakdown_core(session, window_days=7)

        # Total compute delta = 0.25 (modal) + 1.0 (daytona) + 0.50 (other).
        assert (
            round(result.totals.compute_cost_usd - baseline.totals.compute_cost_usd, 4)
            == 1.75
        )
        # Compute stays out of inference spend.
        assert result.totals.cost_usd == baseline.totals.cost_usd

        breakdown = _breakdown_map(result)
        modal_before = baseline_breakdown.get("modal", (0.0, 0))
        modal_after = breakdown["modal"]
        assert round(modal_after[0] - modal_before[0], 4) == 0.25
        assert modal_after[1] - modal_before[1] == 1

        daytona_before = baseline_breakdown.get("daytona", (0.0, 0))
        daytona_after = breakdown["daytona"]
        assert round(daytona_after[0] - daytona_before[0], 4) == 1.0
        assert daytona_after[1] - daytona_before[1] == 2

        # An unknown provider folds into the "other" bucket.
        other_before = baseline_breakdown.get("other", (0.0, 0))
        other_after = breakdown["other"]
        assert round(other_after[0] - other_before[0], 4) == 0.5
        assert other_after[1] - other_before[1] == 1

        series = _series_totals(result)
        assert round(series["modal"] - baseline_series.get("modal", 0.0), 4) == 0.25
        assert round(series["daytona"] - baseline_series.get("daytona", 0.0), 4) == 1.0
        assert round(series["other"] - baseline_series.get("other", 0.0), 4) == 0.5

        compute_total = sum(
            bucket.costs.get("compute", 0.0) for bucket in result.series_by_type.buckets
        )
        baseline_compute_total = sum(
            bucket.costs.get("compute", 0.0)
            for bucket in baseline.series_by_type.buckets
        )
        assert round(compute_total - baseline_compute_total, 4) == 1.75
    finally:
        async with get_session() as session:
            await session.execute(
                ModalCostSpanModel.__table__.delete().where(
                    ModalCostSpanModel.id.like(f"modal-dashboard-{_RUN}-%")
                )
            )
