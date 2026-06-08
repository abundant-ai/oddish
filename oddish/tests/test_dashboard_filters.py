from __future__ import annotations

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from oddish.core.dashboard import (
    _build_experiments_author_filter,
    _experiment_row_passes_status_filter,
)


def _row(**overrides):
    base = {
        "active_trials": 0,
        "retrying_trials": 0,
        "verdict_needs_review": 0,
        "verdict_pending": 0,
        "verdict_failed": 0,
        "failed_trials": 0,
    }
    base.update(overrides)
    return base


def test_retrying_experiment_status_filter_matches_retrying_trials() -> None:
    assert _experiment_row_passes_status_filter(
        _row(active_trials=3, retrying_trials=1),
        status_filter="retrying",
    )


def test_retrying_experiment_status_filter_ignores_other_active_trials() -> None:
    assert not _experiment_row_passes_status_filter(
        _row(active_trials=3, retrying_trials=0),
        status_filter="retrying",
    )


# ---------------------------------------------------------------------------
# Owner filter (dashboard "Org / Mine" toggle + member picker)
# ---------------------------------------------------------------------------


def _compile_sql(clause) -> str:
    from sqlalchemy.dialects import postgresql

    return str(
        clause.compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )


def test_author_filter_absent_when_no_user() -> None:
    assert (
        _build_experiments_author_filter(None, None, org_id="org_1") is None
    )


def test_author_filter_matches_created_by_and_scopes_org() -> None:
    clause = _build_experiments_author_filter("user_123", None, org_id="org_1")
    assert clause is not None
    sql = _compile_sql(clause).lower()
    assert "exists" in sql
    assert "created_by_user_id" in sql
    assert "user_123" in sql
    # Org scoping is applied for defense-in-depth on the EXISTS side.
    assert "org_id" in sql and "org_1" in sql
    # Soft-deleted experiment links must be excluded.
    assert "deleted_at" in sql
    # Without a github username, the tag fallback is not added.
    assert "github_username" not in sql


def test_author_filter_includes_github_username_fallback() -> None:
    clause = _build_experiments_author_filter(
        "user_123", ("octocat",), org_id="org_1"
    )
    assert clause is not None
    sql = _compile_sql(clause)
    assert "github_username" in sql
    assert "octocat" in sql
    # Owner match is an OR of the resolved user id and the github tag.
    assert " OR " in sql.upper()


def test_author_filter_supports_multiple_github_handles() -> None:
    clause = _build_experiments_author_filter(
        "user_123",
        ("dot-agi", "praxs"),
        org_id="org_1",
    )
    assert clause is not None
    sql = _compile_sql(clause)
    assert "dot-agi" in sql
    assert "praxs" in sql
    assert " IN " in sql.upper()


def test_author_filter_requires_latest_task_match() -> None:
    clause = _build_experiments_author_filter("user_123", None, org_id="org_1")
    assert clause is not None
    sql = _compile_sql(clause).lower()
    # Latest-task filter correlates on the newest linked task id, not any member task.
    assert "order by" in sql
    assert " limit " in sql


def test_author_filter_without_org_scope_omits_org_predicate() -> None:
    clause = _build_experiments_author_filter("user_123", None, org_id=None)
    assert clause is not None
    sql = _compile_sql(clause).lower()
    assert "created_by_user_id" in sql
    assert "org_id" not in sql


def test_author_filter_includes_tasks_user_email_fallback() -> None:
    clause = _build_experiments_author_filter(
        "user_123",
        None,
        org_id="org_1",
        experiments_author_email="alice@example.com",
    )
    assert clause is not None
    sql = _compile_sql(clause)
    assert "created_by_user_id" in sql
    assert "alice@example.com" in sql
    assert " OR " in sql.upper()
