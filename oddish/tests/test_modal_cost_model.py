import sqlalchemy as sa

from oddish.db.models import ModalCostSpanModel, ModalRateModel


def test_span_table_name_and_columns() -> None:
    assert ModalCostSpanModel.__tablename__ == "modal_costs"
    cols = set(ModalCostSpanModel.__table__.columns.keys())
    assert {
        "id",
        "trial_id",
        "experiment_id",
        "org_id",
        "billed_user_id",
        "worker_job_id",
        "worker_job_attempt",
        "attempt",
        "component_role",
        "span_ordinal",
        "provider",
        "external_id",
        "started_at",
        "finished_at",
        "basis",
        "spec_source",
        "cpu_request",
        "cpu_limit",
        "mem_request_mb",
        "mem_limit_mb",
        "gpu_type",
        "gpu_count",
        "price_multiplier",
        "rate_snapshot",
        "rate_ids",
        "cost_usd",
        "unpriced_reason",
        "cost_source",
        "estimator_version",
        "created_at",
        "updated_at",
        "deleted_at",
    } <= cols


def test_scope_columns_have_no_foreign_keys() -> None:
    # A hard FK to trials/experiments risks the migration lock that
    # deadlocked prod (same rationale as analysis_costs).
    for name in (
        "trial_id",
        "experiment_id",
        "org_id",
        "billed_user_id",
        "worker_job_id",
    ):
        assert ModalCostSpanModel.__table__.c[name].foreign_keys == set()


def test_span_unique_key_is_job_attempt_role_ordinal() -> None:
    uniques = [
        c
        for c in ModalCostSpanModel.__table__.constraints
        if isinstance(c, sa.UniqueConstraint)
    ]
    by_name = {c.name: c for c in uniques}
    uq = by_name["uq_modal_costs_job_attempt_role_ordinal"]
    assert [col.name for col in uq.columns] == [
        "worker_job_id",
        "worker_job_attempt",
        "component_role",
        "span_ordinal",
    ]


def test_provider_external_id_partial_unique_index() -> None:
    idx = {i.name: i for i in ModalCostSpanModel.__table__.indexes}[
        "uq_modal_costs_provider_external_id"
    ]
    assert idx.unique
    assert [col.name for col in idx.columns] == ["provider", "external_id"]
    assert "external_id IS NOT NULL" in str(idx.dialect_options["postgresql"]["where"])


def test_open_span_partial_index() -> None:
    idx = {i.name: i for i in ModalCostSpanModel.__table__.indexes}[
        "ix_modal_costs_open_spans"
    ]
    assert not idx.unique
    assert "finished_at IS NULL" in str(idx.dialect_options["postgresql"]["where"])


def test_scope_and_finished_at_indexes_exist() -> None:
    names = {i.name for i in ModalCostSpanModel.__table__.indexes}
    assert {
        "ix_modal_costs_trial_id",
        "ix_modal_costs_experiment_id",
        "ix_modal_costs_org_id",
        "ix_modal_costs_finished_at",
    } <= names


def test_check_constraints_present() -> None:
    names = {
        c.name
        for c in ModalCostSpanModel.__table__.constraints
        if isinstance(c, sa.CheckConstraint)
    }
    assert {
        "ck_modal_costs_span_order",
        "ck_modal_costs_cpu_request_nonneg",
        "ck_modal_costs_cpu_limit_nonneg",
        "ck_modal_costs_mem_request_nonneg",
        "ck_modal_costs_mem_limit_nonneg",
        "ck_modal_costs_gpu_count_nonneg",
    } <= names


def test_money_columns_are_numeric_not_float() -> None:
    cost = ModalCostSpanModel.__table__.c.cost_usd.type
    assert isinstance(cost, sa.Numeric) and not isinstance(cost, sa.Float)
    assert (cost.precision, cost.scale) == (14, 6)
    multiplier = ModalCostSpanModel.__table__.c.price_multiplier.type
    assert isinstance(multiplier, sa.Numeric)
    assert not isinstance(multiplier, sa.Float)


def test_external_id_is_nullable_unbounded_text() -> None:
    col = ModalCostSpanModel.__table__.c.external_id
    assert col.nullable
    assert isinstance(col.type, sa.Text)
    job = ModalCostSpanModel.__table__.c.worker_job_id
    assert job.nullable
    assert isinstance(job.type, sa.Text)


def test_rate_table_shape() -> None:
    assert ModalRateModel.__tablename__ == "modal_rates"
    cols = set(ModalRateModel.__table__.columns.keys())
    assert {
        "id",
        "provider",
        "sku",
        "usd_per_sec",
        "effective_at",
        "note",
        "created_at",
        "updated_at",
        "deleted_at",
    } <= cols
    rate = ModalRateModel.__table__.c.usd_per_sec.type
    assert isinstance(rate, sa.Numeric) and not isinstance(rate, sa.Float)
    assert (rate.precision, rate.scale) == (16, 10)


def test_rate_natural_key_unique() -> None:
    uniques = {
        c.name: c
        for c in ModalRateModel.__table__.constraints
        if isinstance(c, sa.UniqueConstraint)
    }
    uq = uniques["uq_modal_rates_provider_sku_effective"]
    assert [col.name for col in uq.columns] == ["provider", "sku", "effective_at"]
