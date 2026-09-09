"""Regression coverage for the Thunder fallback schema migration."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


_MIGRATION_PATH = (
    Path(__file__).resolve().parents[1]
    / "alembic/versions/thunder_fallback_001_add_reroute_state.py"
)
_SPEC = importlib.util.spec_from_file_location(
    "thunder_fallback_001_add_reroute_state",
    _MIGRATION_PATH,
)
assert _SPEC is not None and _SPEC.loader is not None
_MIGRATION = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MIGRATION)

_COLUMNS = {
    "reroute_from_environment",
    "reroute_reason",
    "reroute_pending_teardown",
}


def test_migration_is_linear_from_current_upstream_head():
    assert _MIGRATION.down_revision == "delivery_qa_work_001"


def test_upgrade_is_noop_when_initial_schema_already_has_columns(monkeypatch):
    statements: list[str] = []
    monkeypatch.setattr(_MIGRATION.op, "get_bind", lambda: object())
    monkeypatch.setattr(_MIGRATION, "_columns", lambda _bind, _table: _COLUMNS)
    monkeypatch.setattr(_MIGRATION.op, "execute", statements.append)
    monkeypatch.setattr(
        _MIGRATION.op,
        "add_column",
        lambda *_args, **_kwargs: pytest.fail("existing column was added again"),
    )

    _MIGRATION.upgrade()

    assert len(statements) == 2
    assert "DROP CONSTRAINT IF EXISTS ck_worker_jobs_execution_lane" in statements[0]
    assert "'thunder_trial'" in statements[1]


def test_upgrade_adds_only_missing_columns(monkeypatch):
    added: list[str] = []
    monkeypatch.setattr(_MIGRATION.op, "get_bind", lambda: object())
    monkeypatch.setattr(_MIGRATION.op, "execute", lambda _statement: None)
    monkeypatch.setattr(
        _MIGRATION,
        "_columns",
        lambda _bind, _table: {"reroute_from_environment"},
    )
    monkeypatch.setattr(
        _MIGRATION.op,
        "add_column",
        lambda _table, column: added.append(column.name),
    )

    _MIGRATION.upgrade()

    assert added == ["reroute_reason", "reroute_pending_teardown"]


def test_downgrade_drops_only_existing_columns(monkeypatch):
    dropped: list[str] = []
    statements: list[str] = []
    monkeypatch.setattr(_MIGRATION.op, "get_bind", lambda: object())
    monkeypatch.setattr(_MIGRATION.op, "execute", statements.append)
    monkeypatch.setattr(
        _MIGRATION,
        "_columns",
        lambda _bind, _table: {
            "reroute_from_environment",
            "reroute_pending_teardown",
        },
    )
    monkeypatch.setattr(
        _MIGRATION.op,
        "drop_column",
        lambda _table, column: dropped.append(column),
    )

    _MIGRATION.downgrade()

    assert dropped == ["reroute_pending_teardown", "reroute_from_environment"]
    assert len(statements) == 3
    assert "WHERE execution_lane = 'thunder_trial'" in statements[0]
    assert "DROP CONSTRAINT IF EXISTS ck_worker_jobs_execution_lane" in statements[1]
    assert "'thunder_trial'" not in statements[2]
