"""Regression coverage for the trial-import idempotency key length.

``POST /trials/import/init`` builds ``trials.idempotency_key`` as
``import:{source_trial_id}:{experiment_id}``. The source trial id is itself
``{task_id}-{index}``, and Harbor task ids were widened to 128 chars
(``long_task_ids_001``) without widening the *derived* ``idempotency_key``
column, which stayed at ``VARCHAR(64)``. Long task names therefore produced a
key longer than 64 chars; Postgres rejects (does not truncate) the over-length
value, so the insert 500'd deterministically for long-named tasks while short
ones succeeded.

These tests are intentionally DB-free: they pin the column width and the pure
key-builder so the regression can't silently come back without a Postgres
fixture.
"""

from __future__ import annotations

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from oddish.core.ingest.trial_imports import (  # noqa: E402
    _import_idempotency_key,
)
from oddish.db import TRIAL_IDEMPOTENCY_KEY_MAX_LENGTH, TrialModel  # noqa: E402

# An 8-char experiment id (``generate_id`` returns ``str(uuid4())[:8]``).
_EXPERIMENT_ID = "63167da9"

# The exact task ids from the bug report, with their observed pre-fix outcome
# against ``VARCHAR(64)``. ``True`` == the import succeeded; ``False`` == 500.
_REPORTED_TASKS = {
    "implement_ugrep-6029d17d": True,
    "market_risk_garch_evt_tailrisk-e78bff41": True,
    "implement_editorconfig_checker-99acb256": True,
    "nestjs_nest_interceptor_status_override-e91266bd": False,
    "prettier_prettier_expression_statement_parentheses-f68e1b83": False,
}


def test_column_is_wide_enough_for_composed_keys() -> None:
    """The ORM column must match the widened width, with headroom over the
    longest realistic composed key (``import:`` + a max-length trial id +
    ``:`` + an experiment id)."""
    assert (
        TrialModel.__table__.c.idempotency_key.type.length
        == TRIAL_IDEMPOTENCY_KEY_MAX_LENGTH
    )
    # trials.id is VARCHAR(160); a source trial id can be that long.
    longest_source_trial_id = "t" * 160
    composed = f"import:{longest_source_trial_id}:{_EXPERIMENT_ID}"
    assert len(composed) <= TRIAL_IDEMPOTENCY_KEY_MAX_LENGTH


def test_reported_tasks_now_fit_and_stay_idempotent() -> None:
    """Every task from the report — including the two that used to 500 — now
    produces a composed (idempotency-preserving) key that fits the column."""
    for task_id, worked_before in _REPORTED_TASKS.items():
        source_trial_id = f"{task_id}-0"  # first imported trial of the task
        key = _import_idempotency_key(source_trial_id, _EXPERIMENT_ID)

        # Still the composed, deterministic form (not the uuid fallback) so the
        # unique index keeps rejecting accidental re-imports.
        assert key == f"import:{source_trial_id}:{_EXPERIMENT_ID}"
        assert len(key) <= TRIAL_IDEMPOTENCY_KEY_MAX_LENGTH

        # Sanity-check that the table mirrors the reality the fix addresses:
        # the failing tasks really did exceed the old VARCHAR(64) limit.
        assert (len(key) <= 64) == worked_before


def test_falls_back_to_unique_key_without_external_id() -> None:
    key = _import_idempotency_key(None, _EXPERIMENT_ID)
    assert key.startswith("import-")
    assert len(key) <= TRIAL_IDEMPOTENCY_KEY_MAX_LENGTH


def test_pathological_external_id_does_not_overflow() -> None:
    """An external id longer than the column can't be made idempotent without
    overflowing, so the builder drops idempotency and returns a unique key that
    still fits rather than letting the insert 500."""
    key = _import_idempotency_key("x" * 1000, _EXPERIMENT_ID)
    assert key.startswith("import-")
    assert len(key) <= TRIAL_IDEMPOTENCY_KEY_MAX_LENGTH


def test_composed_key_is_deterministic() -> None:
    a = _import_idempotency_key("some-task-3", _EXPERIMENT_ID)
    b = _import_idempotency_key("some-task-3", _EXPERIMENT_ID)
    assert a == b == f"import:some-task-3:{_EXPERIMENT_ID}"
