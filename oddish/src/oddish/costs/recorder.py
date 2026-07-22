"""Best-effort persistence for compute-cost spans."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.exc import ProgrammingError

from oddish.config import settings
from oddish.costs.modal_cost import (
    DEFAULT_RATES,
    RateRow,
    SpanResources,
    build_span_row,
    close_span_values,
    estimate_span_cost,
    select_rates,
)
from oddish.db import (
    ModalCostSpanModel,
    ModalRateModel,
    TrialModel,
    WorkerJobModel,
    WorkerJobStatus,
    get_session,
)

log = logging.getLogger(__name__)
_missing_table_logged = False


@dataclass(frozen=True)
class WorkerBillingSpec:
    cpu_cores: float
    memory_mb: int
    nonpreemptible: bool
    provider: str = "modal"

    def resources(self) -> SpanResources:
        return SpanResources(
            cpu_request=self.cpu_cores,
            cpu_limit=None,
            mem_request_mb=self.memory_mb,
            mem_limit_mb=None,
            gpu_type=None,
            gpu_count=0,
            price_multiplier=Decimal(3 if self.nonpreemptible else 1),
            container_class="function",
            spec_source="pinned",
            cpu_enforcement_mode="request",
            mem_enforcement_mode="request",
        )


def _is_missing_table(exc: BaseException) -> bool:
    if not isinstance(exc, ProgrammingError):
        return False
    orig = getattr(exc, "orig", None)
    return (
        getattr(orig, "sqlstate", None) == "42P01"
        or "UndefinedTable" in type(orig).__name__
    )


def _record_failure(operation: str, exc: BaseException) -> None:
    global _missing_table_logged
    if _is_missing_table(exc):
        if not _missing_table_logged:
            _missing_table_logged = True
            log.warning("Modal cost tables are not available; tracking is deferred")
        return
    log.exception("Modal cost recorder failed during %s", operation, exc_info=exc)


async def _load_rates() -> tuple[RateRow, ...]:
    try:
        async with get_session() as session:
            rows = (await session.execute(select(ModalRateModel))).scalars().all()
        if rows:
            return tuple(
                RateRow(
                    id=row.id,
                    provider=row.provider,
                    sku=row.sku,
                    usd_per_sec=row.usd_per_sec,
                    effective_at=row.effective_at,
                )
                for row in rows
            )
    except Exception as exc:
        _record_failure("load rates", exc)
    return DEFAULT_RATES


def _resources_from_row(row: ModalCostSpanModel) -> SpanResources:
    return SpanResources(
        cpu_request=row.cpu_request,
        cpu_limit=row.cpu_limit,
        mem_request_mb=row.mem_request_mb,
        mem_limit_mb=row.mem_limit_mb,
        gpu_type=row.gpu_type,
        gpu_count=row.gpu_count or 0,
        price_multiplier=row.price_multiplier or Decimal(1),
        container_class=(
            "function" if row.component_role == "worker_function" else "sandbox"
        ),
        spec_source=row.spec_source,
        cpu_enforcement_mode=row.cpu_enforcement_mode,
        mem_enforcement_mode=row.mem_enforcement_mode,
    )


async def _close_rows(
    session: Any,
    rows: list[ModalCostSpanModel],
    *,
    finished_at: datetime,
    rates: tuple[RateRow, ...],
    basis: str | None = None,
) -> int:
    closed = 0
    for row in rows:
        end = max(finished_at, row.started_at)
        resources = _resources_from_row(row)
        chosen = select_rates(
            rates,
            row.provider,
            resources.container_class,
            resources.gpu_type,
            row.started_at,
        )
        estimate = estimate_span_cost(row.started_at, end, resources, chosen)
        result = await session.execute(
            update(ModalCostSpanModel)
            .where(
                ModalCostSpanModel.id == row.id,
                ModalCostSpanModel.finished_at.is_(None),
            )
            .values(
                **close_span_values(
                    finished_at=end,
                    estimate=estimate,
                    rate_selection=chosen,
                    basis=basis,
                )
            )
        )
        closed += int(result.rowcount or 0)
    return closed


async def _trial_scope(session: Any, job: Any) -> dict[str, Any]:
    # Scope columns for a worker span, minus trials.attempts on purpose: the
    # worker span opens at claim, BEFORE run_trial_job bumps trials.attempts,
    # so snapshotting it here would disagree with the sandbox/verifier spans
    # (which read the post-bump value). worker_job_attempt is the reliable
    # per-execution key; the informational trial attempt stays None here.
    if job.subject_table != "trials" or not job.subject_id:
        return {}
    trial = (
        await session.execute(
            select(
                TrialModel.experiment_id,
                TrialModel.org_id,
                TrialModel.billed_user_id,
            ).where(TrialModel.id == job.subject_id)
        )
    ).one_or_none()
    if trial is None:
        return {}
    return {
        "trial_id": job.subject_id,
        "experiment_id": trial.experiment_id,
        "org_id": trial.org_id,
        "billed_user_id": trial.billed_user_id,
    }


async def open_worker_span(
    job: Any,
    spec: WorkerBillingSpec | None,
    *,
    started_at: datetime | None = None,
) -> None:
    if not settings.modal_cost_tracking or spec is None:
        return
    try:
        async with get_session() as session:
            exists = await session.scalar(
                select(ModalCostSpanModel.id).where(
                    ModalCostSpanModel.worker_job_id == job.id,
                    ModalCostSpanModel.worker_job_attempt == job.attempts,
                    ModalCostSpanModel.component_role == "worker_function",
                    ModalCostSpanModel.span_ordinal == 0,
                )
            )
            if exists is not None:
                return
            session.add(
                build_span_row(
                    component_role="worker_function",
                    provider=spec.provider,
                    started_at=started_at or datetime.now(timezone.utc),
                    basis="hooks",
                    resources=spec.resources(),
                    worker_job_id=job.id,
                    worker_job_attempt=job.attempts,
                    **(await _trial_scope(session, job)),
                )
            )
    except Exception as exc:
        _record_failure("open worker span", exc)


async def close_worker_span(
    worker_job_id: str,
    worker_job_attempt: int,
    *,
    finished_at: datetime,
) -> None:
    if not settings.modal_cost_tracking:
        return
    try:
        rates = await _load_rates()
        async with get_session() as session:
            rows = (
                (
                    await session.execute(
                        select(ModalCostSpanModel).where(
                            ModalCostSpanModel.worker_job_id == worker_job_id,
                            ModalCostSpanModel.worker_job_attempt == worker_job_attempt,
                            ModalCostSpanModel.component_role == "worker_function",
                            ModalCostSpanModel.finished_at.is_(None),
                        )
                    )
                )
                .scalars()
                .all()
            )
            await _close_rows(session, list(rows), finished_at=finished_at, rates=rates)
    except Exception as exc:
        _record_failure("close worker span", exc)


async def transition_agent_sandbox(
    *,
    worker_job_id: str,
    worker_job_attempt: int,
    trial_id: str,
    attempt: int,
    experiment_id: str | None,
    org_id: str | None,
    billed_user_id: str | None,
    provider: str,
    external_id: str,
    resources: SpanResources,
    observed_at: datetime,
) -> None:
    if not settings.modal_cost_tracking:
        return
    try:
        rates = await _load_rates()
        async with get_session() as session:
            existing = (
                (
                    await session.execute(
                        select(ModalCostSpanModel)
                        .where(
                            ModalCostSpanModel.worker_job_id == worker_job_id,
                            ModalCostSpanModel.worker_job_attempt == worker_job_attempt,
                            ModalCostSpanModel.component_role == "agent_sandbox",
                        )
                        .with_for_update()
                    )
                )
                .scalars()
                .all()
            )
            if any(row.external_id == external_id for row in existing):
                return
            await _close_rows(
                session,
                [row for row in existing if row.finished_at is None],
                finished_at=observed_at,
                rates=rates,
            )
            session.add(
                build_span_row(
                    trial_id=trial_id,
                    experiment_id=experiment_id,
                    org_id=org_id,
                    billed_user_id=billed_user_id,
                    worker_job_id=worker_job_id,
                    worker_job_attempt=worker_job_attempt,
                    attempt=attempt,
                    component_role="agent_sandbox",
                    span_ordinal=max((row.span_ordinal for row in existing), default=-1)
                    + 1,
                    provider=provider,
                    external_id=external_id,
                    started_at=observed_at,
                    basis="hooks",
                    resources=resources,
                )
            )
    except Exception as exc:
        _record_failure("transition agent sandbox", exc)


async def close_agent_sandboxes(
    worker_job_id: str,
    worker_job_attempt: int,
    *,
    finished_at: datetime,
    basis: str | None = None,
) -> None:
    if not settings.modal_cost_tracking:
        return
    try:
        rates = await _load_rates()
        async with get_session() as session:
            rows = (
                (
                    await session.execute(
                        select(ModalCostSpanModel).where(
                            ModalCostSpanModel.worker_job_id == worker_job_id,
                            ModalCostSpanModel.worker_job_attempt == worker_job_attempt,
                            ModalCostSpanModel.component_role == "agent_sandbox",
                            ModalCostSpanModel.finished_at.is_(None),
                        )
                    )
                )
                .scalars()
                .all()
            )
            await _close_rows(
                session,
                list(rows),
                finished_at=finished_at,
                rates=rates,
                basis=basis,
            )
    except Exception as exc:
        _record_failure("close agent sandbox", exc)


async def record_verifier_span(
    *,
    worker_job_id: str,
    worker_job_attempt: int,
    trial_id: str,
    attempt: int,
    experiment_id: str | None,
    org_id: str | None,
    billed_user_id: str | None,
    provider: str,
    resources: SpanResources,
    started_at: datetime,
    finished_at: datetime,
) -> None:
    if not settings.modal_cost_tracking:
        return
    try:
        rates = await _load_rates()
        async with get_session() as session:
            exists = await session.scalar(
                select(ModalCostSpanModel.id).where(
                    ModalCostSpanModel.worker_job_id == worker_job_id,
                    ModalCostSpanModel.worker_job_attempt == worker_job_attempt,
                    ModalCostSpanModel.component_role == "verifier_sandbox",
                    ModalCostSpanModel.span_ordinal == 0,
                )
            )
            if exists is not None:
                return
            end = max(finished_at, started_at)
            chosen = select_rates(
                rates,
                provider,
                resources.container_class,
                resources.gpu_type,
                started_at,
            )
            estimate = estimate_span_cost(started_at, end, resources, chosen)
            session.add(
                build_span_row(
                    trial_id=trial_id,
                    experiment_id=experiment_id,
                    org_id=org_id,
                    billed_user_id=billed_user_id,
                    worker_job_id=worker_job_id,
                    worker_job_attempt=worker_job_attempt,
                    attempt=attempt,
                    component_role="verifier_sandbox",
                    span_ordinal=0,
                    provider=provider,
                    started_at=started_at,
                    finished_at=end,
                    basis="phase_timing",
                    resources=resources,
                    estimate=estimate,
                    rate_selection=chosen,
                )
            )
    except Exception as exc:
        _record_failure("record verifier span", exc)


async def price_unpriced_spans(worker_job_id: str, worker_job_attempt: int) -> None:
    if not settings.modal_cost_tracking:
        return
    try:
        rates = await _load_rates()
        async with get_session() as session:
            rows = (
                (
                    await session.execute(
                        select(ModalCostSpanModel).where(
                            ModalCostSpanModel.worker_job_id == worker_job_id,
                            ModalCostSpanModel.worker_job_attempt == worker_job_attempt,
                            ModalCostSpanModel.finished_at.is_not(None),
                            ModalCostSpanModel.cost_usd.is_(None),
                            ModalCostSpanModel.unpriced_reason.is_(None),
                        )
                    )
                )
                .scalars()
                .all()
            )
            for row in rows:
                resources = _resources_from_row(row)
                chosen = select_rates(
                    rates,
                    row.provider,
                    resources.container_class,
                    resources.gpu_type,
                    row.started_at,
                )
                estimate = estimate_span_cost(
                    row.started_at, row.finished_at, resources, chosen
                )
                await session.execute(
                    update(ModalCostSpanModel)
                    .where(
                        ModalCostSpanModel.id == row.id,
                        ModalCostSpanModel.cost_usd.is_(None),
                        ModalCostSpanModel.unpriced_reason.is_(None),
                    )
                    .values(
                        cost_usd=estimate.cost_usd,
                        unpriced_reason=estimate.unpriced_reason,
                        rate_snapshot=estimate.rate_snapshot,
                        rate_ids=chosen.rate_ids,
                    )
                )
    except Exception as exc:
        _record_failure("price spans", exc)


async def reconcile_compute_cost_spans() -> int:
    if not settings.modal_cost_tracking:
        return 0
    try:
        rates = await _load_rates()
        async with get_session() as session:
            rows = (
                await session.execute(
                    select(
                        ModalCostSpanModel,
                        WorkerJobModel.finished_at,
                        WorkerJobModel.attempts,
                        TrialModel.heartbeat_at,
                    )
                    .join(
                        WorkerJobModel,
                        WorkerJobModel.id == ModalCostSpanModel.worker_job_id,
                    )
                    .outerjoin(TrialModel, TrialModel.id == ModalCostSpanModel.trial_id)
                    .where(
                        ModalCostSpanModel.finished_at.is_(None),
                        WorkerJobModel.status.in_(
                            (
                                WorkerJobStatus.SUCCESS,
                                WorkerJobStatus.FAILED,
                                WorkerJobStatus.CANCELLED,
                                WorkerJobStatus.RETRYING,
                            )
                        ),
                    )
                )
            ).all()
            closed = 0
            for span, job_finished_at, job_attempts, trial_heartbeat_at in rows:
                # The job's finished_at is the LATEST attempt's end. Only apply
                # it to a span from that same attempt. A span left open by an
                # earlier attempt (a hard-killed worker that ran no close path)
                # must not be billed through the later attempt's finished_at, so
                # close it at its own started_at -- a ~zero-duration floor is far
                # safer than over-counting a different attempt's whole runtime.
                is_stale_attempt = (
                    span.worker_job_attempt is not None
                    and job_attempts is not None
                    and span.worker_job_attempt < job_attempts
                )
                if is_stale_attempt:
                    close_at = span.started_at
                else:
                    close_at = job_finished_at or trial_heartbeat_at or span.started_at
                closed += await _close_rows(
                    session,
                    [span],
                    finished_at=close_at,
                    rates=rates,
                    basis="reconciled",
                )

            unpriced = (
                (
                    await session.execute(
                        select(ModalCostSpanModel).where(
                            ModalCostSpanModel.finished_at.is_not(None),
                            ModalCostSpanModel.cost_usd.is_(None),
                            ModalCostSpanModel.unpriced_reason.is_(None),
                        )
                    )
                )
                .scalars()
                .all()
            )
            for span in unpriced:
                resources = _resources_from_row(span)
                chosen = select_rates(
                    rates,
                    span.provider,
                    resources.container_class,
                    resources.gpu_type,
                    span.started_at,
                )
                estimate = estimate_span_cost(
                    span.started_at, span.finished_at, resources, chosen
                )
                await session.execute(
                    update(ModalCostSpanModel)
                    .where(
                        ModalCostSpanModel.id == span.id,
                        ModalCostSpanModel.cost_usd.is_(None),
                        ModalCostSpanModel.unpriced_reason.is_(None),
                    )
                    .values(
                        cost_usd=estimate.cost_usd,
                        unpriced_reason=estimate.unpriced_reason,
                        rate_snapshot=estimate.rate_snapshot,
                        rate_ids=chosen.rate_ids,
                    )
                )
        return closed
    except Exception as exc:
        _record_failure("reconcile spans", exc)
        return 0
