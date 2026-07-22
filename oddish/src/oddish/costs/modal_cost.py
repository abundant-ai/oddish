"""Pure pricing helpers for Modal compute-cost estimation.

No DB or network IO here so the logic is unit-testable in isolation (mirrors
:mod:`oddish.analyze.analysis_cost`): plain values and dataclasses in, an
unpersisted ORM row out. Sessions and transactions belong to the caller.

Estimation model — gross list price, deliberately a floor::

    cost = dur * (cpu_cores * cpu_rate + mem_gib * mem_rate) * multiplier
         + dur * gpu_count * gpu_rate

* ``dur`` is fractional seconds; Modal bills per second with no minimum
  increment.
* CPU and memory bill at the *request* value. Modal bills actual usage above
  the request (burst), so request-based estimates are accepted, documented
  floors — for sandboxes and for the worker's scalar CPU alike.
* ``multiplier`` is the nonpreemptible surcharge (3 for the worker function,
  1 for sandboxes). It applies ONLY to the cpu+mem terms, never to GPU;
  sandbox skus already carry the higher sandbox rate.
* MiB -> GiB is an exact ``/ 1024`` in Decimal.
* All money math is Decimal end to end — no floats.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Literal, Sequence

from oddish.db.models import ModalCostSpanModel

ESTIMATOR_VERSION = 1

ContainerClass = Literal["function", "sandbox"]

# Modal's default request when a task pins nothing (harbor passes None and
# Modal applies its minimum request, billed with burst above it).
MODAL_DEFAULT_CPU_REQUEST = 0.125
MODAL_DEFAULT_MEM_REQUEST_MB = 128

_MIB_PER_GIB = Decimal(1024)
_MICROS_PER_SEC = Decimal(1_000_000)


@dataclass(frozen=True)
class RateRow:
    """One rate-card row (mirror of ``modal_rates``).

    ``id`` is the ``modal_rates.id`` of a DB row, or ``None`` for the
    code-constant fallback rows in :data:`DEFAULT_RATES`.
    """

    provider: str
    sku: str
    usd_per_sec: Decimal
    effective_at: datetime
    id: str | None = None


_SEED_EFFECTIVE_AT = datetime(2025, 1, 1, tzinfo=timezone.utc)

# Fallback rate card used when the modal_rates table is empty or missing.
# Mirrors the migration seed rows (modal_costs_001) exactly — drift between
# the two is unit-tested. Verified against modal.com/pricing on 2026-07-22.
DEFAULT_RATES: tuple[RateRow, ...] = tuple(
    RateRow(
        provider=provider,
        sku=sku,
        usd_per_sec=Decimal(usd_per_sec),
        effective_at=_SEED_EFFECTIVE_AT,
    )
    for provider, sku, usd_per_sec in (
        ("modal", "function:cpu_core_sec", "0.0000131"),
        ("modal", "function:mem_gib_sec", "0.00000222"),
        ("modal", "sandbox:cpu_core_sec", "0.00003942"),
        ("modal", "sandbox:mem_gib_sec", "0.00000667"),
        ("modal", "gpu:B300", "0.001972"),
        ("modal", "gpu:B200", "0.001736"),
        ("modal", "gpu:H200", "0.001261"),
        ("modal", "gpu:H100", "0.001097"),
        ("modal", "gpu:RTX_PRO_6000", "0.000842"),
        ("modal", "gpu:A100-80GB", "0.000694"),
        ("modal", "gpu:A100-40GB", "0.000583"),
        ("modal", "gpu:L40S", "0.000542"),
        ("modal", "gpu:A10", "0.000306"),
        ("modal", "gpu:L4", "0.000222"),
        ("modal", "gpu:T4", "0.000164"),
    )
)


@dataclass(frozen=True)
class SpanResources:
    """Billable resource spec captured at span open.

    ``price_multiplier`` is 1 for sandboxes and 3 for the nonpreemptible
    worker function. ``gpu_type`` should already be normalized via
    :func:`normalize_gpu_type` so the stored row and the rate lookup agree.
    """

    cpu_request: float | None
    cpu_limit: float | None
    mem_request_mb: int | None
    mem_limit_mb: int | None
    gpu_type: str | None
    gpu_count: int
    price_multiplier: Decimal
    container_class: ContainerClass
    spec_source: str  # "pinned" | "override" | "modal_default" | "unknown"


# Modal billing names as they appear in the gpu:* skus.
_CANONICAL_GPUS = (
    "B300",
    "B200",
    "H200",
    "H100",
    "RTX_PRO_6000",
    "A100-80GB",
    "A100-40GB",
    "L40S",
    "A10",
    "L4",
    "T4",
)

# Separator-insensitive uppercase key -> Modal billing name. Covers harbor /
# AWS-style aliases (A10G) and Modal's bare "a100" (the 40GB variant).
_GPU_ALIASES: dict[str, str] = {
    **{name.replace("-", "_").upper(): name for name in _CANONICAL_GPUS},
    "A10G": "A10",
    "A100": "A100-40GB",
    "A100_40": "A100-40GB",
    "A100_80": "A100-80GB",
}


def normalize_gpu_type(raw: str | None) -> str | None:
    """Map a harbor/Modal accelerator name to its Modal billing name.

    Count suffixes are stripped ("H100:2" -> "H100" — the count travels
    separately), as is Modal's exact-type "!" marker. "any" and unknown names
    return ``None``: pricing then records an ``unpriced_reason`` instead of
    guessing a rate.
    """
    if raw is None:
        return None
    name = raw.strip().split(":", 1)[0].rstrip("!").strip()
    if not name:
        return None
    key = name.upper().replace("-", "_").replace(" ", "_")
    if key == "ANY":
        return None
    return _GPU_ALIASES.get(key)


@dataclass(frozen=True)
class RateSelection:
    """Rates chosen for one span: per needed sku, the newest row with
    ``effective_at <= at``. ``None`` where no applicable row exists."""

    provider: str
    container_class: ContainerClass
    cpu: RateRow | None
    mem: RateRow | None
    gpu: RateRow | None

    def _chosen(self) -> tuple[RateRow, ...]:
        return tuple(row for row in (self.cpu, self.mem, self.gpu) if row is not None)

    @property
    def rate_ids(self) -> dict[str, str | None]:
        """sku -> modal_rates.id of the chosen rows (None = code fallback)."""
        return {row.sku: row.id for row in self._chosen()}

    @property
    def rate_values(self) -> dict[str, str]:
        """sku -> usd_per_sec as strings (Decimal-exact, JSONB-safe)."""
        return {row.sku: str(row.usd_per_sec) for row in self._chosen()}


def _newest_at_or_before(
    rates: Sequence[RateRow], provider: str, sku: str, at: datetime
) -> RateRow | None:
    best: RateRow | None = None
    for row in rates:
        if row.provider != provider or row.sku != sku:
            continue
        if row.effective_at > at:
            continue
        if best is None or row.effective_at > best.effective_at:
            best = row
    return best


def select_rates(
    rates: Sequence[RateRow],
    provider: str,
    container_class: ContainerClass,
    gpu_type: str | None,
    at: datetime,
) -> RateSelection:
    """Pick the applicable rate rows for a span starting at ``at``.

    Rate-card semantics: a price change appends a row with a later
    ``effective_at``, and a span prices at the newest row effective at or
    before its start — so old estimates stay reproducible.
    """
    gpu: RateRow | None = None
    if gpu_type is not None:
        gpu = _newest_at_or_before(rates, provider, f"gpu:{gpu_type}", at)
    return RateSelection(
        provider=provider,
        container_class=container_class,
        cpu=_newest_at_or_before(
            rates, provider, f"{container_class}:cpu_core_sec", at
        ),
        mem=_newest_at_or_before(rates, provider, f"{container_class}:mem_gib_sec", at),
        gpu=gpu,
    )


@dataclass(frozen=True)
class EstimateResult:
    """Priced (or explicitly unpriced) span. Exactly one of ``cost_usd`` /
    ``unpriced_reason`` is set."""

    cost_usd: Decimal | None
    unpriced_reason: str | None
    rate_snapshot: dict[str, object]


def _duration_seconds(started_at: datetime, finished_at: datetime) -> Decimal:
    """Exact fractional-second duration (clamped at zero, never negative)."""
    delta = finished_at - started_at
    micros = (delta.days * 86_400 + delta.seconds) * 1_000_000 + delta.microseconds
    if micros <= 0:
        return Decimal(0)
    return Decimal(micros) / _MICROS_PER_SEC


def estimate_span_cost(
    started_at: datetime,
    finished_at: datetime,
    resources: SpanResources,
    rate_selection: RateSelection,
) -> EstimateResult:
    """Price a closed span. Never guesses: a missing GPU rate or an empty
    resource spec yields ``cost_usd=None`` with an ``unpriced_reason``.

    ``spec_source == "modal_default"`` with no cpu/mem/gpu at all prices at
    Modal's default request (0.125 core, 128 MiB) instead of going unpriced —
    still a floor, since Modal bills burst above the request.
    """
    duration = _duration_seconds(started_at, finished_at)

    cpu = resources.cpu_request
    mem_mb = resources.mem_request_mb
    gpu_count = resources.gpu_count or 0

    if cpu is None and mem_mb is None and gpu_count <= 0:
        if resources.spec_source == "modal_default":
            cpu = MODAL_DEFAULT_CPU_REQUEST
            mem_mb = MODAL_DEFAULT_MEM_REQUEST_MB
        else:
            return EstimateResult(
                cost_usd=None,
                unpriced_reason="no_resources",
                rate_snapshot=_snapshot(duration, resources, rate_selection),
            )

    snapshot = _snapshot(duration, resources, rate_selection, cpu=cpu, mem_mb=mem_mb)

    # GPU requested but no billable rate resolved (gpu_type None/unknown, or
    # no rate row for the type): never guess a GPU price.
    if gpu_count > 0 and rate_selection.gpu is None:
        return EstimateResult(
            cost_usd=None, unpriced_reason="unknown_gpu", rate_snapshot=snapshot
        )

    # cpu/mem present but the provider has no rate card (e.g. daytona before
    # rates exist): record the span, leave it unpriced.
    if (cpu is not None and rate_selection.cpu is None) or (
        mem_mb is not None and rate_selection.mem is None
    ):
        return EstimateResult(
            cost_usd=None, unpriced_reason="no_rate", rate_snapshot=snapshot
        )

    base = Decimal(0)
    if cpu is not None and rate_selection.cpu is not None:
        base += Decimal(str(cpu)) * rate_selection.cpu.usd_per_sec
    if mem_mb is not None and rate_selection.mem is not None:
        base += (Decimal(mem_mb) / _MIB_PER_GIB) * rate_selection.mem.usd_per_sec

    cost = duration * base * resources.price_multiplier
    if gpu_count > 0 and rate_selection.gpu is not None:
        cost += duration * Decimal(gpu_count) * rate_selection.gpu.usd_per_sec

    return EstimateResult(cost_usd=cost, unpriced_reason=None, rate_snapshot=snapshot)


def _snapshot(
    duration: Decimal,
    resources: SpanResources,
    rate_selection: RateSelection,
    *,
    cpu: float | None = None,
    mem_mb: int | None = None,
) -> dict[str, object]:
    """Auditable record of the inputs an estimate used (all Decimal values as
    strings so JSONB round-trips exactly)."""
    return {
        "rates": rate_selection.rate_values,
        "price_multiplier": str(resources.price_multiplier),
        "duration_sec": str(duration),
        "billed_cpu_cores": None if cpu is None else str(Decimal(str(cpu))),
        "billed_mem_gib": (
            None if mem_mb is None else str(Decimal(mem_mb) / _MIB_PER_GIB)
        ),
        "billed_gpu_count": resources.gpu_count or 0,
    }


def build_span_row(
    *,
    component_role: str,
    provider: str,
    started_at: datetime,
    basis: str,
    resources: SpanResources,
    trial_id: str | None = None,
    experiment_id: str | None = None,
    org_id: str | None = None,
    billed_user_id: str | None = None,
    worker_job_id: str | None = None,
    worker_job_attempt: int | None = None,
    attempt: int | None = None,
    span_ordinal: int = 0,
    external_id: str | None = None,
    finished_at: datetime | None = None,
    estimate: EstimateResult | None = None,
    rate_selection: RateSelection | None = None,
) -> ModalCostSpanModel:
    """Construct an unpersisted ``modal_costs`` row (caller's transaction).

    Open spans pass no ``estimate``; a closed-at-birth span (e.g. the worker
    span written at outcome time) passes both ``finished_at`` and the
    ``estimate``/``rate_selection`` so cost fields land in the same insert.
    """
    row = ModalCostSpanModel(
        trial_id=trial_id,
        experiment_id=experiment_id,
        org_id=org_id,
        billed_user_id=billed_user_id,
        worker_job_id=worker_job_id,
        worker_job_attempt=worker_job_attempt,
        attempt=attempt,
        component_role=component_role,
        span_ordinal=span_ordinal,
        provider=provider,
        external_id=external_id,
        started_at=started_at,
        finished_at=finished_at,
        basis=basis,
        spec_source=resources.spec_source,
        cpu_request=resources.cpu_request,
        cpu_limit=resources.cpu_limit,
        mem_request_mb=resources.mem_request_mb,
        mem_limit_mb=resources.mem_limit_mb,
        gpu_type=resources.gpu_type,
        gpu_count=resources.gpu_count,
        price_multiplier=resources.price_multiplier,
        cost_source="estimated",
        estimator_version=ESTIMATOR_VERSION,
    )
    if estimate is not None:
        row.cost_usd = estimate.cost_usd
        row.unpriced_reason = estimate.unpriced_reason
        row.rate_snapshot = estimate.rate_snapshot
    if rate_selection is not None:
        row.rate_ids = rate_selection.rate_ids
    return row


def close_span_values(
    *,
    finished_at: datetime,
    estimate: EstimateResult,
    rate_selection: RateSelection | None = None,
    basis: str | None = None,
) -> dict[str, object]:
    """Field values for the CAS close:
    ``UPDATE modal_costs SET <these> WHERE id = :id AND finished_at IS NULL``.

    ``basis`` is included only when the close changes it (reconciled/reaped
    closes override the open row's basis; a normal hook close leaves it).
    """
    values: dict[str, object] = {
        "finished_at": finished_at,
        "cost_usd": estimate.cost_usd,
        "unpriced_reason": estimate.unpriced_reason,
        "rate_snapshot": estimate.rate_snapshot,
        "estimator_version": ESTIMATOR_VERSION,
    }
    if rate_selection is not None:
        values["rate_ids"] = rate_selection.rate_ids
    if basis is not None:
        values["basis"] = basis
    return values
