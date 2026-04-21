"""Phase A tests for the unified `worker_jobs` table.

These validate that the schema scaffolding is in place:
- ``WorkerJobKind`` / ``WorkerJobStatus`` enums expose the agreed members
- ``WorkerJobModel`` declares the columns, indexes, and types we'll
  depend on in later phases
- Enum values are the uppercase Postgres-native spellings we migrate to

No DB connection is required; these are metadata-level assertions that
catch the easy regression where someone edits the model without editing
the migration (or vice versa).
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from oddish.db import (  # noqa: E402
    WorkerJobKind,
    WorkerJobModel,
    WorkerJobStatus,
)


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


def test_worker_job_kind_members():
    assert {k.value for k in WorkerJobKind} == {
        "TRIAL",
        "ANALYSIS",
        "VERDICT",
        "QA_REVIEW",
        "TASK_EXPAND",
    }


def test_worker_job_status_members():
    assert {s.value for s in WorkerJobStatus} == {
        "QUEUED",
        "RUNNING",
        "RETRYING",
        "SUCCESS",
        "FAILED",
        "CANCELLED",
        "BLOCKED",
    }


def test_worker_job_status_string_compare():
    # str-Enum lets handlers / SQL write the bare string and still type-check.
    assert WorkerJobStatus.RUNNING == "RUNNING"
    assert WorkerJobKind.TRIAL == "TRIAL"


# ---------------------------------------------------------------------------
# Model / table metadata
# ---------------------------------------------------------------------------


def _column_names() -> set[str]:
    return {c.name for c in WorkerJobModel.__table__.columns}


def test_worker_jobs_tablename():
    assert WorkerJobModel.__tablename__ == "worker_jobs"


def test_worker_jobs_has_scheduling_columns():
    cols = _column_names()
    # These are the columns the future dispatcher / cleanup / cancel
    # paths rely on. Dropping any of them needs a coordinated migration.
    required = {
        "id",
        "kind",
        "status",
        "queue_key",
        "priority",
        "subject_table",
        "subject_id",
        "parent_job_id",
        "payload",
        "attempts",
        "max_attempts",
        "next_retry_at",
        "available_after",
        "current_worker_id",
        "current_queue_slot",
        "modal_function_call_id",
        "claimed_at",
        "heartbeat_at",
        "stale_reaped_at",
        "heartbeat_failure_count",
        "last_heartbeat_error",
        "last_heartbeat_error_at",
        "error_message",
        "result_summary",
        "created_at",
        "started_at",
        "finished_at",
        "org_id",
    }
    missing = required - cols
    assert not missing, f"worker_jobs is missing columns: {sorted(missing)}"


def test_worker_jobs_nullability_matches_plan():
    table = WorkerJobModel.__table__
    # Required (NOT NULL).
    for name in (
        "id",
        "kind",
        "status",
        "queue_key",
        "priority",
        "payload",
        "attempts",
        "max_attempts",
        "available_after",
        "heartbeat_failure_count",
        "created_at",
    ):
        assert not table.c[name].nullable, f"expected NOT NULL: {name}"

    # Must be nullable -- set by the dispatcher only after claim /
    # handler completion / cleanup / etc.
    for name in (
        "subject_table",
        "subject_id",
        "parent_job_id",
        "current_worker_id",
        "current_queue_slot",
        "modal_function_call_id",
        "claimed_at",
        "heartbeat_at",
        "stale_reaped_at",
        "last_heartbeat_error",
        "last_heartbeat_error_at",
        "error_message",
        "result_summary",
        "started_at",
        "finished_at",
        "next_retry_at",
        "org_id",
    ):
        assert table.c[name].nullable, f"expected NULL: {name}"


def test_worker_jobs_parent_self_fk():
    parent_col = WorkerJobModel.__table__.c["parent_job_id"]
    fks = list(parent_col.foreign_keys)
    assert len(fks) == 1
    fk = fks[0]
    assert fk.column.table.name == "worker_jobs"
    assert fk.column.name == "id"
    assert fk.ondelete == "SET NULL"


def test_worker_jobs_has_required_indexes():
    expected = {
        "idx_worker_jobs_claim",
        "idx_worker_jobs_heartbeat",
        "idx_worker_jobs_subject",
        "idx_worker_jobs_parent",
        "idx_worker_jobs_org",
    }
    got = {idx.name for idx in WorkerJobModel.__table__.indexes}
    missing = expected - got
    assert not missing, f"worker_jobs missing indexes: {sorted(missing)}"


def test_worker_jobs_partial_claim_index_is_scoped():
    claim_idx = next(
        idx
        for idx in WorkerJobModel.__table__.indexes
        if idx.name == "idx_worker_jobs_claim"
    )
    # Partial index predicate lives in dialect_options['postgresql']['where'].
    where_clause = str(
        claim_idx.dialect_options["postgresql"]["where"]
    )
    assert "QUEUED" in where_clause and "RETRYING" in where_clause


def test_worker_jobs_partial_heartbeat_index_is_scoped():
    hb_idx = next(
        idx
        for idx in WorkerJobModel.__table__.indexes
        if idx.name == "idx_worker_jobs_heartbeat"
    )
    where_clause = str(hb_idx.dialect_options["postgresql"]["where"])
    assert "RUNNING" in where_clause


def test_worker_jobs_enum_names():
    # The PG type names ('worker_job_kind' / 'worker_job_status') are the
    # names the migration creates. Mismatched names cause alembic to try
    # to create a fresh type on first use instead of using the existing one.
    kind_col = WorkerJobModel.__table__.c["kind"]
    status_col = WorkerJobModel.__table__.c["status"]
    assert kind_col.type.name == "worker_job_kind"  # type: ignore[attr-defined]
    assert status_col.type.name == "worker_job_status"  # type: ignore[attr-defined]
