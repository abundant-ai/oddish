from __future__ import annotations

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import asyncio

from sqlalchemy.sql.elements import TextClause

from oddish.core.dashboard import (
    EXPERIMENTS_UNATTRIBUTED_OWNER,
    UNRESOLVED_EXPERIMENTS_OWNER,
    _build_experiments_author_filter,
    _build_experiments_search_author_filter,
    _experiment_freetext_match,
    _experiment_row_passes_status_filter,
    load_dashboard_experiments,
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
    assert _build_experiments_author_filter(None, None, org_id="org_1") is None


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
    # created_by only applies when github tag and legacy email are absent.
    assert "github_username" in sql
    assert "is null" in sql or "= ''" in sql


def test_author_filter_includes_github_username_fallback() -> None:
    clause = _build_experiments_author_filter("user_123", ("octocat",), org_id="org_1")
    assert clause is not None
    sql = _compile_sql(clause)
    assert "github_username" in sql
    assert "octocat" in sql
    assert " OR " in sql.upper()
    assert "lower(" in sql.lower()


def test_author_filter_github_handles_are_case_insensitive() -> None:
    clause = _build_experiments_author_filter(
        "user_123",
        ("Praxs",),
        org_id="org_1",
    )
    assert clause is not None
    sql = _compile_sql(clause).lower()
    assert "lower(" in sql
    assert "praxs" in sql


def test_author_filter_treats_unknown_user_as_absent_for_created_by() -> None:
    clause = _build_experiments_author_filter("user_123", None, org_id="org_1")
    assert clause is not None
    sql = _compile_sql(clause).lower()
    assert "unknown" in sql


def test_author_filter_created_by_only_when_github_tag_missing() -> None:
    clause = _build_experiments_author_filter("user_123", None, org_id="org_1")
    assert clause is not None
    sql = _compile_sql(clause).lower()
    assert "created_by_user_id" in sql
    assert "github_username" in sql
    assert "is null" in sql or "= ''" in sql


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


def test_author_filter_requires_primary_task_match() -> None:
    clause = _build_experiments_author_filter("user_123", None, org_id="org_1")
    assert clause is not None
    sql = _compile_sql(clause).lower()
    # Primary-owner filter correlates on the oldest linked task id.
    assert "order by" in sql
    assert " limit " in sql
    assert " asc" in sql


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
        experiments_author_emails=("alice@example.com",),
    )
    assert clause is not None
    sql = _compile_sql(clause)
    assert "created_by_user_id" in sql
    assert "alice@example.com" in sql
    assert " OR " in sql.upper()


def test_author_filter_matches_legacy_tasks_user_github_handles() -> None:
    clause = _build_experiments_author_filter(
        "user_123",
        ("dot-agi", "praxs"),
        org_id="org_1",
    )
    assert clause is not None
    sql = _compile_sql(clause).lower()
    assert "praxs" in sql
    assert "dot-agi" in sql
    # Legacy GH Actions runs stamp tasks.user with the handle when tag is absent.
    assert "user" in sql


def test_author_filter_uses_owner_user_id_fast_path() -> None:
    clause = _build_experiments_author_filter("user_123", None, org_id="org_1")
    assert clause is not None
    sql = _compile_sql(clause).lower()
    assert "owner_user_id" in sql


def test_author_filter_unresolved_owner_matches_nothing() -> None:
    clause = _build_experiments_author_filter(
        UNRESOLVED_EXPERIMENTS_OWNER,
        None,
        org_id="org_1",
    )
    assert clause is not None
    sql = _compile_sql(clause).lower()
    assert "false" in sql


def test_author_filter_supports_multiple_legacy_emails() -> None:
    clause = _build_experiments_author_filter(
        "user_123",
        ("dot-agi",),
        org_id="org_1",
        experiments_author_emails=(
            "pratty@abundant.ai",
            "ps4534@nyu.edu",
        ),
    )
    assert clause is not None
    sql = _compile_sql(clause)
    assert "pratty@abundant.ai" in sql
    assert "ps4534@nyu.edu" in sql


def test_author_filter_owner_only_when_legacy_fallback_disabled() -> None:
    clause = _build_experiments_author_filter(
        "user_1",
        ["praxs"],
        org_id="org_1",
        experiments_author_emails=("pratty@abundant.ai",),
        include_legacy_fallback=False,
    )
    sql = _compile_sql(clause)
    assert "owner_user_id = 'user_1'" in sql
    assert "EXISTS" not in sql


def test_author_filter_keeps_legacy_fallback_by_default() -> None:
    clause = _build_experiments_author_filter(
        "user_1",
        ["praxs"],
        org_id="org_1",
    )
    sql = _compile_sql(clause)
    assert "EXISTS" in sql
    assert "owner_user_id IS NULL" in sql
    assert "owner_user_id = 'user_1'" in sql


def test_author_filter_never_matches_unattributed_sentinel() -> None:
    clause = _build_experiments_author_filter(
        EXPERIMENTS_UNATTRIBUTED_OWNER,
        [],
        org_id="org_1",
    )
    assert "false" in _compile_sql(clause).lower()


# ---------------------------------------------------------------------------
# Search-author filter (the github:/author:/user: qualifier)
# ---------------------------------------------------------------------------


def test_search_author_filter_empty_matches_nothing() -> None:
    clause = _build_experiments_search_author_filter((), (), org_id="org_1")
    assert _compile_sql(clause).lower().strip() == "false"


def test_experiment_freetext_matches_name_author_and_tag() -> None:
    # A bare (un-prefixed) word matches the experiment name/id, OR any task's
    # author (legacy user / github_username tag), OR a tag name.
    sql = _compile_sql(_experiment_freetext_match("alice", org_id="org_1")).lower()
    assert "experiments.name ilike" in sql
    assert "experiments.id ilike" in sql
    # author branch over the experiment's live tasks
    assert "task_experiments" in sql and 'tasks."user" ilike' in sql
    assert "github_username" in sql
    # tag-name branch over EXPERIMENT-scope assignments
    assert "tag_assignments" in sql and "tags.key ilike" in sql
    assert "alice" in sql  # the needle is bound into every branch
    assert "org_1" in sql  # author EXISTS stays org-scoped
    # User-typed wildcards stay literal (escape_like + ESCAPE clause).
    assert "escape" in sql


def test_search_author_filter_matches_handle_via_primary_task() -> None:
    clause = _build_experiments_search_author_filter((), ("Bob",), org_id="org_1")
    sql = _compile_sql(clause)
    assert "EXISTS" in sql
    assert "github_username" in sql
    assert "lower(" in sql.lower()
    assert "bob" in sql.lower()  # case-insensitive
    assert "org_1" in sql
    assert "deleted_at" in sql  # soft-deleted task links excluded


def test_search_author_filter_matches_unattributed_sentinel() -> None:
    # Regression: the owner backfill stamps owner_user_id='__unattributed__' on
    # experiments whose author is not an active org member (external GitHub
    # contributors). A github:<handle> search must still reach them via the
    # primary-task fallback -- gating on owner_user_id IS NULL alone dropped
    # every such experiment once the backfill had converged.
    clause = _build_experiments_search_author_filter(
        (), ("rstar327",), org_id="org_1"
    )
    sql = _compile_sql(clause)
    assert "'__unattributed__'" in sql
    assert "owner_user_id IS NULL" in sql
    assert "EXISTS" in sql
    assert "rstar327" in sql


def test_search_author_filter_unions_handle_collisions() -> None:
    clause = _build_experiments_search_author_filter(
        ("user_a", "user_b"), ("bob",), org_id="org_1"
    )
    sql = _compile_sql(clause)
    # Both colliding users' experiments are reachable via the indexed owner seek.
    assert "user_a" in sql and "user_b" in sql
    assert "owner_user_id IN" in sql.upper().replace("  ", " ") or (
        "user_a" in sql and "user_b" in sql
    )


def test_search_author_filter_matches_legacy_email() -> None:
    clause = _build_experiments_search_author_filter(
        (), (), org_id="org_1", emails=("ada@x.com",)
    )
    sql = _compile_sql(clause)
    assert "ada@x.com" in sql
    assert "EXISTS" in sql


def test_search_author_filter_owner_seek_unions_with_fallback() -> None:
    # A resolved member is reachable both via the indexed owner seek AND via the
    # primary-task fallback (for their NULL/sentinel-owned experiments).
    clause = _build_experiments_search_author_filter(
        ("user_a",), ("bob",), org_id="org_1"
    )
    sql = _compile_sql(clause)
    assert "user_a" in sql  # indexed owner seek
    assert "EXISTS" in sql  # primary-task fallback always emitted
    assert "'__unattributed__'" in sql  # ... covering sentinel-owned rows too


# ---------------------------------------------------------------------------
# Composition: the search filter ANDs with the owner (Org/Mine) + tag filters.
# Driven through load_dashboard_experiments with a fake session that returns an
# empty experiment page (early return) so we can compile the page query and
# assert which predicates were ANDed onto it -- no live DB required.
# ---------------------------------------------------------------------------


class _FakeMappings:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return list(self._rows)


class _FakeResult:
    def __init__(self, *, first=None, all_rows=None, mappings_rows=None):
        self._first = first
        self._all = all_rows or []
        self._mappings_rows = mappings_rows or []

    def first(self):
        return self._first

    def all(self):
        return list(self._all)

    def mappings(self):
        return _FakeMappings(self._mappings_rows)


class _CapturingExpSession:
    """Returns: unowned-experiments probe = present, tag resolution rows for any
    text() query, and an empty experiment page (-> early return). Records every
    statement so the page query (the last one) can be compiled and asserted on.
    """

    def __init__(self, tag_rows):
        self.statements: list[object] = []
        self._tag_rows = tag_rows
        self._select_count = 0

    async def execute(self, statement, *args, **kwargs):
        self.statements.append(statement)
        if isinstance(statement, TextClause):
            return _FakeResult(all_rows=self._tag_rows)
        self._select_count += 1
        if self._select_count == 1:
            return _FakeResult(first=(1,))  # unowned-experiments probe -> True
        return _FakeResult(mappings_rows=[])  # experiment page -> early return


def test_search_author_ands_with_mine_as_intersection() -> None:
    session = _CapturingExpSession(tag_rows=[])
    asyncio.run(
        load_dashboard_experiments(
            session,
            org_id="org_1",
            experiments_limit=10,
            experiments_offset=0,
            experiments_query=None,
            experiments_status="all",
            experiments_tags=None,
            experiments_author_user_id="me_user",  # Mine
            experiments_search_author_user_ids=("bob_user",),
            experiments_search_author_github_usernames=("bob",),
        )
    )
    page_sql = _compile_sql(session.statements[-1]).lower()
    # Both author predicates are ANDed onto the one page query, so Mine AND the
    # searched author is an intersection (empty unless you ARE bob) -- intended.
    assert "me_user" in page_sql  # Mine owner control
    assert "bob_user" in page_sql  # searched author (owner seek)
    assert "bob" in page_sql  # searched author (handle fallback)


def test_search_author_ands_with_tag() -> None:
    from sqlalchemy.dialects import postgresql

    session = _CapturingExpSession(tag_rows=[("tag_xyz", "mytag", "tag_xyz")])
    asyncio.run(
        load_dashboard_experiments(
            session,
            org_id="org_1",
            experiments_limit=10,
            experiments_offset=0,
            experiments_query=None,
            experiments_status="all",
            experiments_tags="tag_xyz",
            experiments_author_user_id=None,  # Org, so author predicate is search-only
            experiments_search_author_github_usernames=("bob",),
        )
    )
    # The tag clause binds a Python list, which literal_binds can't render, so
    # compile with bound params and inspect the values instead. (The JSON key
    # 'github_username' is itself a bind param, hence checked in `bound`.)
    compiled = session.statements[-1].compile(dialect=postgresql.dialect())
    sql = str(compiled).lower()
    bound = " ".join(str(v) for v in compiled.params.values()).lower()
    assert "github_username" in bound  # searched-author predicate present
    assert "bob" in bound  # ... bound to the handle
    assert "tag_assignments" in sql  # tag: filter present on the same query
    assert "tag_xyz" in bound  # ... bound to the resolved tag id


def test_search_author_works_with_org_selected() -> None:
    session = _CapturingExpSession(tag_rows=[])
    asyncio.run(
        load_dashboard_experiments(
            session,
            org_id="org_1",
            experiments_limit=10,
            experiments_offset=0,
            experiments_query=None,
            experiments_status="all",
            experiments_tags=None,
            experiments_author_user_id=None,  # Org (no owner predicate)
            experiments_search_author_github_usernames=("bob",),
        )
    )
    page_sql = _compile_sql(session.statements[-1]).lower()
    assert "bob" in page_sql  # github:bob is the only author predicate
    assert "exists" in page_sql  # via the primary-task fallback
