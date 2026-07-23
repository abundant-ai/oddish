import ast
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from oddish.costs import (
    DEFAULT_RATES,
    ESTIMATOR_VERSION,
    RateRow,
    SpanResources,
    build_span_row,
    close_span_values,
    estimate_span_cost,
    normalize_gpu_type,
    select_rates,
)
from oddish.db.models import ModalCostSpanModel

UTC = timezone.utc
T0 = datetime(2026, 7, 1, 12, 0, 0, tzinfo=UTC)

FUNCTION_CPU = Decimal("0.0000131")
FUNCTION_MEM = Decimal("0.00000222")
SANDBOX_CPU = Decimal("0.00003942")
SANDBOX_MEM = Decimal("0.00000667")
H100 = Decimal("0.001097")


def _resources(**overrides: object) -> SpanResources:
    base: dict = dict(
        cpu_request=1.0,
        cpu_limit=None,
        mem_request_mb=3072,
        mem_limit_mb=None,
        gpu_type=None,
        gpu_count=0,
        price_multiplier=Decimal(3),
        container_class="function",
        spec_source="pinned",
    )
    base.update(overrides)
    return SpanResources(**base)


def _sandbox(**overrides: object) -> SpanResources:
    base: dict = dict(
        container_class="sandbox",
        price_multiplier=Decimal(1),
        cpu_request=2.0,
        mem_request_mb=4096,
    )
    base.update(overrides)
    return _resources(**base)


# ---------------------------------------------------------------------------
# Rate selection
# ---------------------------------------------------------------------------


def test_select_rates_picks_newest_at_or_before() -> None:
    old = RateRow(
        provider="modal",
        sku="function:cpu_core_sec",
        usd_per_sec=Decimal("0.0000131"),
        effective_at=datetime(2025, 1, 1, tzinfo=UTC),
        id="rate-old",
    )
    new = RateRow(
        provider="modal",
        sku="function:cpu_core_sec",
        usd_per_sec=Decimal("0.0000200"),
        effective_at=datetime(2026, 1, 1, tzinfo=UTC),
        id="rate-new",
    )
    rates = [new, old]  # order must not matter

    # Span starting between the two effective dates -> the older row.
    sel = select_rates(
        rates, "modal", "function", None, datetime(2025, 6, 1, tzinfo=UTC)
    )
    assert sel.cpu is old

    # Exactly at the boundary -> the newer row (effective_at <= at).
    sel = select_rates(
        rates, "modal", "function", None, datetime(2026, 1, 1, tzinfo=UTC)
    )
    assert sel.cpu is new

    # After both -> still the newest.
    sel = select_rates(
        rates, "modal", "function", None, datetime(2026, 7, 1, tzinfo=UTC)
    )
    assert sel.cpu is new

    # Before every row -> nothing applies.
    sel = select_rates(
        rates, "modal", "function", None, datetime(2024, 1, 1, tzinfo=UTC)
    )
    assert sel.cpu is None


def test_function_vs_sandbox_sku_split() -> None:
    sel_fn = select_rates(DEFAULT_RATES, "modal", "function", None, T0)
    assert sel_fn.cpu is not None and sel_fn.mem is not None
    assert sel_fn.cpu.sku == "function:cpu_core_sec"
    assert sel_fn.cpu.usd_per_sec == FUNCTION_CPU
    assert sel_fn.mem.sku == "function:mem_gib_sec"
    assert sel_fn.mem.usd_per_sec == FUNCTION_MEM

    sel_sb = select_rates(DEFAULT_RATES, "modal", "sandbox", None, T0)
    assert sel_sb.cpu is not None and sel_sb.mem is not None
    assert sel_sb.cpu.sku == "sandbox:cpu_core_sec"
    assert sel_sb.cpu.usd_per_sec == SANDBOX_CPU
    assert sel_sb.mem.usd_per_sec == SANDBOX_MEM


def test_select_rates_exposes_ids_and_values() -> None:
    sel = select_rates(DEFAULT_RATES, "modal", "function", "H100", T0)
    assert sel.gpu is not None and sel.gpu.usd_per_sec == H100
    # DEFAULT_RATES are code constants: chosen ids are None but present.
    assert set(sel.rate_ids) == {
        "function:cpu_core_sec",
        "function:mem_gib_sec",
        "gpu:H100",
    }
    assert all(v is None for v in sel.rate_ids.values())
    assert sel.rate_values["gpu:H100"] == "0.001097"


# ---------------------------------------------------------------------------
# GPU name normalization
# ---------------------------------------------------------------------------


def test_normalize_gpu_type() -> None:
    assert normalize_gpu_type("H100") == "H100"
    assert normalize_gpu_type("h100") == "H100"
    assert normalize_gpu_type("H100:2") == "H100"  # count travels separately
    assert normalize_gpu_type("H100!") == "H100"  # Modal exact-type marker
    assert normalize_gpu_type("H100!:2") == "H100"
    assert normalize_gpu_type("A10G") == "A10"  # harbor/AWS alias
    assert normalize_gpu_type("a100") == "A100-40GB"  # Modal bare a100 = 40GB
    assert normalize_gpu_type("A100-80GB") == "A100-80GB"
    assert normalize_gpu_type("A100_80GB") == "A100-80GB"
    assert normalize_gpu_type("L40S") == "L40S"
    assert normalize_gpu_type("rtx pro 6000") == "RTX_PRO_6000"
    assert normalize_gpu_type("any") is None
    assert normalize_gpu_type("ANY") is None
    assert normalize_gpu_type("warpcore9000") is None
    assert normalize_gpu_type("") is None
    assert normalize_gpu_type(None) is None


# ---------------------------------------------------------------------------
# Estimation
# ---------------------------------------------------------------------------


def test_worked_example_two_hour_worker_exact_decimal() -> None:
    # 2h worker at 1 core / 3072MB nonpreemptible:
    # 7200 * (1*0.0000131 + 3*0.00000222) * 3 == 0.426816, exactly.
    sel = select_rates(DEFAULT_RATES, "modal", "function", None, T0)
    res = estimate_span_cost(T0, T0 + timedelta(hours=2), _resources(), sel)
    assert res.unpriced_reason is None
    assert res.cost_usd == Decimal("0.426816")
    assert res.cost_usd == Decimal(7200) * (
        Decimal(1) * FUNCTION_CPU + Decimal(3) * FUNCTION_MEM
    ) * Decimal(3)
    # And it is a Decimal, not a float.
    assert isinstance(res.cost_usd, Decimal)


def test_multiplier_applies_only_to_function_cpu_mem_never_gpu() -> None:
    sel = select_rates(DEFAULT_RATES, "modal", "function", "H100", T0)
    res = estimate_span_cost(
        T0,
        T0 + timedelta(seconds=100),
        _resources(gpu_type="H100", gpu_count=2),
        sel,
    )
    expected = (
        Decimal(100)
        * (Decimal(1) * FUNCTION_CPU + Decimal(3) * FUNCTION_MEM)
        * Decimal(3)
        + Decimal(100) * Decimal(2) * H100
    )
    assert res.cost_usd == expected
    # The GPU term is NOT tripled: tripling it would add 2*100*H100*2 more.
    tripled_gpu = Decimal(100) * (
        Decimal(1) * FUNCTION_CPU + Decimal(3) * FUNCTION_MEM
    ) * Decimal(3) + Decimal(100) * Decimal(2) * H100 * Decimal(3)
    assert res.cost_usd != tripled_gpu


def test_sandbox_prices_at_sandbox_rates_without_multiplier() -> None:
    sel = select_rates(DEFAULT_RATES, "modal", "sandbox", None, T0)
    res = estimate_span_cost(T0, T0 + timedelta(seconds=60), _sandbox(), sel)
    assert res.cost_usd == Decimal(60) * (
        Decimal(2) * SANDBOX_CPU + Decimal(4) * SANDBOX_MEM
    )


def test_mem_mib_to_gib_is_exact_divide_by_1024() -> None:
    sel = select_rates(DEFAULT_RATES, "modal", "sandbox", None, T0)
    res = estimate_span_cost(
        T0,
        T0 + timedelta(seconds=1024),
        _sandbox(cpu_request=None, mem_request_mb=512),
        sel,
    )
    # 1024s * (512/1024 GiB) * rate == 512 * rate, exactly.
    assert res.cost_usd == Decimal(512) * SANDBOX_MEM
    assert res.rate_snapshot["billed_mem_gib"] == "0.5"


def test_fractional_second_durations() -> None:
    sel = select_rates(DEFAULT_RATES, "modal", "sandbox", None, T0)
    res = estimate_span_cost(
        T0,
        T0 + timedelta(seconds=1, microseconds=500_000),
        _sandbox(cpu_request=1.0, mem_request_mb=1024),
        sel,
    )
    assert res.cost_usd == Decimal("1.5") * (SANDBOX_CPU + SANDBOX_MEM)
    assert res.rate_snapshot["duration_sec"] == "1.5"


def test_unknown_gpu_yields_null_cost_with_reason() -> None:
    # gpu_count > 0 with gpu_type None (e.g. "any"): never guess.
    sel = select_rates(DEFAULT_RATES, "modal", "function", None, T0)
    res = estimate_span_cost(
        T0,
        T0 + timedelta(seconds=10),
        _resources(gpu_type=None, gpu_count=1),
        sel,
    )
    assert res.cost_usd is None
    assert res.unpriced_reason == "unknown_gpu"

    # Same when the type has no rate row.
    sel = select_rates(DEFAULT_RATES, "modal", "function", "SPARKLE9", T0)
    res = estimate_span_cost(
        T0,
        T0 + timedelta(seconds=10),
        _resources(gpu_type="SPARKLE9", gpu_count=1),
        sel,
    )
    assert res.cost_usd is None
    assert res.unpriced_reason == "unknown_gpu"


def test_no_resources_reason_when_spec_empty() -> None:
    sel = select_rates(DEFAULT_RATES, "modal", "sandbox", None, T0)
    res = estimate_span_cost(
        T0,
        T0 + timedelta(seconds=10),
        _sandbox(cpu_request=None, mem_request_mb=None, spec_source="unknown"),
        sel,
    )
    assert res.cost_usd is None
    assert res.unpriced_reason == "no_resources"


def test_modal_default_prices_at_default_request() -> None:
    # Unpinned task: harbor passes None -> Modal's default request of
    # 0.125 core / 128 MiB (= 0.125 GiB) is the billing floor.
    sel = select_rates(DEFAULT_RATES, "modal", "sandbox", None, T0)
    res = estimate_span_cost(
        T0,
        T0 + timedelta(hours=1),
        _sandbox(cpu_request=None, mem_request_mb=None, spec_source="modal_default"),
        sel,
    )
    expected = Decimal(3600) * (
        Decimal("0.125") * SANDBOX_CPU + Decimal("0.125") * SANDBOX_MEM
    )
    assert res.cost_usd == expected
    assert res.unpriced_reason is None
    assert res.rate_snapshot["billed_cpu_cores"] == "0.125"
    assert res.rate_snapshot["billed_mem_gib"] == "0.125"


def test_unrated_provider_records_no_rate() -> None:
    sel = select_rates(DEFAULT_RATES, "daytona", "sandbox", None, T0)
    res = estimate_span_cost(T0, T0 + timedelta(seconds=30), _sandbox(), sel)
    assert res.cost_usd is None
    assert res.unpriced_reason == "no_rate"


def test_negative_duration_clamps_to_zero_cost() -> None:
    sel = select_rates(DEFAULT_RATES, "modal", "function", None, T0)
    res = estimate_span_cost(T0, T0 - timedelta(seconds=5), _resources(), sel)
    assert res.cost_usd == Decimal(0)


# ---------------------------------------------------------------------------
# Builder + close helper
# ---------------------------------------------------------------------------


def test_build_span_row_maps_all_fields() -> None:
    resources = _sandbox(cpu_limit=4.0, mem_limit_mb=8192, gpu_type="H100", gpu_count=1)
    sel = select_rates(DEFAULT_RATES, "modal", "sandbox", "H100", T0)
    finished = T0 + timedelta(seconds=90)
    est = estimate_span_cost(T0, finished, resources, sel)
    row = build_span_row(
        component_role="agent_sandbox",
        provider="modal",
        started_at=T0,
        basis="hooks",
        resources=resources,
        trial_id="trial-abc",
        experiment_id="exp-1",
        org_id="org-1",
        billed_user_id="user-1",
        worker_job_id="wj-1",
        worker_job_attempt=2,
        attempt=3,
        span_ordinal=1,
        external_id="sb-123",
        finished_at=finished,
        estimate=est,
        rate_selection=sel,
    )
    assert isinstance(row, ModalCostSpanModel)
    assert row.trial_id == "trial-abc"
    assert row.experiment_id == "exp-1"
    assert row.org_id == "org-1"
    assert row.billed_user_id == "user-1"
    assert row.worker_job_id == "wj-1"
    assert row.worker_job_attempt == 2
    assert row.attempt == 3
    assert row.component_role == "agent_sandbox"
    assert row.span_ordinal == 1
    assert row.provider == "modal"
    assert row.external_id == "sb-123"
    assert row.started_at == T0
    assert row.finished_at == finished
    assert row.basis == "hooks"
    assert row.spec_source == "pinned"
    assert row.cpu_request == 2.0
    assert row.cpu_limit == 4.0
    assert row.mem_request_mb == 4096
    assert row.mem_limit_mb == 8192
    assert row.gpu_type == "H100"
    assert row.gpu_count == 1
    assert row.price_multiplier == Decimal(1)
    assert row.cost_usd == est.cost_usd
    assert row.unpriced_reason is None
    assert row.rate_snapshot == est.rate_snapshot
    assert row.rate_ids == sel.rate_ids
    assert row.cost_source == "estimated"
    assert row.estimator_version == ESTIMATOR_VERSION


def test_build_span_row_open_span_has_no_cost_fields() -> None:
    row = build_span_row(
        component_role="agent_sandbox",
        provider="modal",
        started_at=T0,
        basis="hooks",
        resources=_sandbox(),
        worker_job_id="wj-1",
        worker_job_attempt=1,
    )
    assert row.finished_at is None
    assert row.cost_usd is None
    assert row.unpriced_reason is None
    assert row.rate_snapshot is None
    assert row.rate_ids is None
    assert row.span_ordinal == 0
    assert row.estimator_version == ESTIMATOR_VERSION


def test_close_span_values_shape() -> None:
    resources = _sandbox()
    sel = select_rates(DEFAULT_RATES, "modal", "sandbox", None, T0)
    finished = T0 + timedelta(seconds=45)
    est = estimate_span_cost(T0, finished, resources, sel)

    values = close_span_values(finished_at=finished, estimate=est, rate_selection=sel)
    assert values == {
        "finished_at": finished,
        "cost_usd": est.cost_usd,
        "unpriced_reason": None,
        "rate_snapshot": est.rate_snapshot,
        "rate_ids": sel.rate_ids,
        "estimator_version": ESTIMATOR_VERSION,
    }

    # A reconciliation close overrides basis; a normal close leaves it out.
    values = close_span_values(finished_at=finished, estimate=est, basis="reconciled")
    assert values["basis"] == "reconciled"
    assert "rate_ids" not in values


# ---------------------------------------------------------------------------
# DEFAULT_RATES <-> migration seed drift guard
# ---------------------------------------------------------------------------


def test_default_rates_mirror_migration_seed_exactly() -> None:
    migration = (
        Path(__file__).resolve().parents[1]
        / "alembic"
        / "versions"
        / "modal_costs_001_add_modal_costs.py"
    )
    tree = ast.parse(migration.read_text())
    seed = None
    for node in ast.walk(tree):
        value = None
        if isinstance(node, ast.Assign):
            names = [t.id for t in node.targets if isinstance(t, ast.Name)]
            value = node.value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names = [node.target.id]
            value = node.value
        else:
            continue
        if "SEED_RATES" in names and value is not None:
            seed = ast.literal_eval(value)
    assert seed is not None, "SEED_RATES not found in migration"
    expected = tuple(
        (row.provider, row.sku, str(row.usd_per_sec)) for row in DEFAULT_RATES
    )
    assert tuple(tuple(entry) for entry in seed) == expected
    # Fallback rows are code constants: no DB row id to reference.
    assert all(row.id is None for row in DEFAULT_RATES)
