"""A migration that takes ACCESS EXCLUSIVE on a hot table without a bounded
wait either deadlocks against live traffic or queues every query on that table
behind its pending lock -- and it runs against PRODUCTION on merge to main.
prod_schema_repair_001 (#891) failed three consecutive pushes this way.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_GUARD_PATH = (
    Path(__file__).resolve().parents[1] / "scripts" / "migration_lock_guard.py"
)
_spec = importlib.util.spec_from_file_location("migration_lock_guard", _GUARD_PATH)
assert _spec and _spec.loader
guard = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(guard)


@pytest.fixture
def migration(tmp_path):
    def _write(body: str) -> Path:
        path = tmp_path / "mig.py"
        path.write_text("import sqlalchemy as sa\nfrom alembic import op\n\n" + body)
        return path

    return _write


def test_drop_column_on_hot_table_without_lock_timeout_is_flagged(migration):
    # The exact statement that deadlocked prod.
    path = migration(
        'def upgrade():\n    op.drop_column("tasks", "pre_trial_finished_at")\n'
    )
    violations = guard.check(path)
    assert violations
    assert "drop_column on hot table `tasks`" in violations[0]


def test_lock_timeout_satisfies_the_guard(migration):
    path = migration(
        "def upgrade():\n"
        "    op.execute(\"SET LOCAL lock_timeout = '5s'\")\n"
        '    op.execute("LOCK TABLE tasks IN ACCESS EXCLUSIVE MODE")\n'
        '    op.drop_column("tasks", "pre_trial_finished_at")\n'
    )
    assert guard.check(path) == []


def test_cold_table_is_not_flagged(migration):
    path = migration(
        'def upgrade():\n    op.drop_column("prompts", "active_version")\n'
    )
    assert guard.check(path) == []


def test_raw_alter_table_is_flagged(migration):
    # Raw SQL reaches the same lock without going through an op.* helper.
    path = migration(
        'def upgrade():\n    op.execute(sa.text("ALTER TABLE trials ADD COLUMN x INT"))\n'
    )
    violations = guard.check(path)
    assert violations
    assert "raw ALTER TABLE on hot table `trials`" in violations[0]


def test_create_index_concurrently_is_exempt(migration):
    # CONCURRENTLY does not block writes, so it needs no bounded wait.
    path = migration(
        "def upgrade():\n"
        '    op.create_index("ix_a", "trials", ["a"], postgresql_concurrently=True)\n'
    )
    assert guard.check(path) == []


def test_plain_create_index_on_hot_table_is_flagged(migration):
    path = migration('def upgrade():\n    op.create_index("ix_a", "trials", ["a"])\n')
    violations = guard.check(path)
    assert violations
    assert "create_index on hot table `trials`" in violations[0]
