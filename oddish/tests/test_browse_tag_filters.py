"""Tests for the browse-filter AST and name resolution."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


def _run(coro):
    return asyncio.run(coro)


class _NameResolveSession:
    """Fakes the tags lookup; rows are (id, normalized_key, resolved_id)."""

    def __init__(self, mapping):
        # normalized_key -> tag id (resolved id == own id; no merges here)
        self.mapping = mapping

    async def execute(self, stmt, params=None):
        wanted_names = (params or {}).get("names") or []
        wanted_ids = (params or {}).get("ids") or []

        class _R:
            def __init__(self_inner, rows):
                self_inner.rows = rows

            def all(self_inner):
                return self_inner.rows

        rows = [
            (tag_id, name, tag_id)
            for name, tag_id in self.mapping.items()
            if name in wanted_names or tag_id in wanted_ids
        ]
        return _R(rows)


def test_tag_filter_ast_default_empty():
    from oddish.core.tag_filter_ast import TagFilterAST

    f = TagFilterAST()
    assert f.is_empty() is True


def test_tag_filter_ast_normalizes_to_lower():
    from oddish.core.tag_filter_ast import TagFilterAST

    f = TagFilterAST(all=["Flaky"], any_=["regression"], none=["WIP"])
    assert f.normalized_all == ["flaky"]
    assert f.normalized_any == ["regression"]
    assert f.normalized_none == ["wip"]


def test_tag_filter_ast_uses_shared_normalizer_for_whitespace():
    """Filter inputs MUST go through the same normalizer used at tag
    creation (``oddish.core.tag_naming.normalize_tag_key``), otherwise a
    user-typed name like ``"Flaky Trial"`` resolves to ``flaky trial``
    while the persisted ``tags.normalized_key`` is ``flaky-trial`` and
    the join misses.
    """
    from oddish.core.tag_filter_ast import TagFilterAST
    from oddish.core.tag_naming import normalize_tag_key

    f = TagFilterAST(all=["  Flaky Trial  "], any_=["SWE Bench v2.0"])
    assert f.normalized_all == [normalize_tag_key("  Flaky Trial  ")] == ["flaky-trial"]
    assert (
        f.normalized_any == [normalize_tag_key("SWE Bench v2.0")] == ["swe-bench-v2.0"]
    )


def test_resolve_names_to_ids_returns_ids_and_unknown(monkeypatch):
    from oddish.core.tag_filter_ast import (
        TagFilterAST,
        resolve_names_to_ids,
    )

    session = _NameResolveSession(
        {"flaky": "tag-flaky", "regression": "tag-regression"}
    )
    resolved, unknown = _run(
        resolve_names_to_ids(
            session,
            org_id="org-1",
            ast=TagFilterAST(all=["flaky"], any_=["regression", "ghost"]),
        )
    )
    assert resolved.all_ids == ["tag-flaky"]
    assert resolved.any_ids == ["tag-regression"]
    assert resolved.none_ids == []
    assert unknown == {"ghost"}


def test_resolve_accepts_tag_ids_as_tokens():
    """The dashboard picker and saved filters send tag IDS, not names —
    the resolver must match ``tags.id`` exactly before falling back to
    the normalized-name lookup.
    """
    from oddish.core.tag_filter_ast import (
        TagFilterAST,
        resolve_names_to_ids,
    )

    session = _NameResolveSession({"flaky": "tag-flaky", "wip": "tag-wip"})
    resolved, unknown = _run(
        resolve_names_to_ids(
            session,
            org_id="org-1",
            # id token + name token mixed, like a saved filter edited by hand
            ast=TagFilterAST(all=["tag-flaky"], none=["WIP"]),
        )
    )
    assert resolved.all_ids == ["tag-flaky"]
    assert resolved.none_ids == ["tag-wip"]
    assert unknown == set()


def test_apply_filter_returns_three_text_predicates():
    from oddish.core.tag_filter_ast import (
        ResolvedTagFilter,
        build_filter_predicates,
    )

    res = ResolvedTagFilter(all_ids=["a", "b"], any_ids=["c", "d"], none_ids=["e"])
    predicates = build_filter_predicates(res)
    sql_strs = [str(p) for p in predicates]
    # AND => @>
    assert any("@>" in s for s in sql_strs)
    # OR => &&
    assert any("&&" in s for s in sql_strs)
    # NOT => NOT (.. && ..)
    assert any("NOT (" in s and "&&" in s for s in sql_strs)


def test_apply_filter_returns_empty_predicates_for_empty_filter():
    from oddish.core.tag_filter_ast import (
        ResolvedTagFilter,
        build_filter_predicates,
    )

    res = ResolvedTagFilter(all_ids=[], any_ids=[], none_ids=[])
    predicates = build_filter_predicates(res)
    assert predicates == []


def test_user_tag_ref_dto_and_task_browse_item_field():
    from oddish.schemas import (
        UserTagRef,
        TaskBrowseItem,
        TaskResponse,
        TaskStatusResponse,
    )

    ref = UserTagRef(
        tag_id="t1",
        key="flaky",
        value=None,
        color=None,
        visibility="PRIVATE",
        current=True,
        older=False,
    )
    assert ref.tag_id == "t1"
    assert "user_tags" in TaskBrowseItem.model_fields
    assert "user_tags" in TaskResponse.model_fields
    assert "user_tags" in TaskStatusResponse.model_fields

    # The version switcher's VERSION-scope editor hydrates from these.
    from oddish.schemas import TaskVersionSummary

    assert "user_tags" in TaskVersionSummary.model_fields


def test_build_task_status_response_populates_user_tags(monkeypatch):
    """Regression: adding ``user_tags`` to the DTO is meaningless if the
    response builder never sets it.
    """
    from oddish.core import helpers
    from oddish.schemas import UserTagRef

    async def _fake_list(session, *, task_ids, public_only=False):
        return {
            tid: [
                UserTagRef(
                    tag_id="t-1",
                    key="flaky",
                    value=None,
                    color=None,
                    visibility="PRIVATE",
                    current=True,
                    older=False,
                )
            ]
            for tid in task_ids
        }

    monkeypatch.setattr(
        helpers, "list_effective_user_tags_for_task_versions", _fake_list
    )

    resp = _run(
        helpers._hydrate_user_tags_for_task(  # type: ignore[attr-defined]
            session=None, task_id="task-1"
        )
    )
    assert resp and isinstance(resp[0], UserTagRef)
    assert resp[0].key == "flaky"


def test_browse_tasks_core_accepts_tag_filter_params(monkeypatch):
    """Make sure the keyword arguments are wired through without import errors."""
    import inspect

    from oddish.core import endpoints

    sig = inspect.signature(endpoints.browse_tasks_core)
    assert "tags_all" in sig.parameters
    assert "tags_any" in sig.parameters
    assert "tags_none" in sig.parameters


def test_browse_tasks_core_unknown_positive_tag_returns_empty_page():
    """A positive (AND/OR) filter for a tag that doesn't exist can never
    match any task, so browse short-circuits to an empty page — not a 400
    (type-ahead in the dashboard search sends partial names) and not
    silently-unfiltered rows.
    """
    from oddish.core import endpoints

    class _Session:
        async def execute(self, stmt, params=None):
            class _R:
                def all(self_inner):
                    return []  # resolver finds nothing

            return _R()

        async def connection(self):
            return None

        async def scalar(self, stmt, params=None):
            return None

    resp = _run(
        endpoints.browse_tasks_core(
            _Session(),
            org_id="org-1",
            limit=25,
            offset=0,
            tags_all=["ghost"],
        )
    )
    assert resp.items == []
    assert resp.has_more is False


def test_browse_tasks_core_imports_user_tag_ref():
    """Regression: ``UserTagRef`` is referenced inside ``browse_tasks_core``
    when populating the response items, so it MUST be imported at module
    scope (the pre-fix version omitted the import)."""
    from oddish.core import endpoints
    from oddish.schemas import UserTagRef

    assert getattr(endpoints, "UserTagRef", None) is UserTagRef


def test_tasks_browse_endpoint_accepts_tag_query_params():
    import pytest

    pytest.importorskip("backend.api.routers.tasks")
    import inspect
    from backend.api.routers import tasks as tasks_router

    sig = inspect.signature(tasks_router.browse_tasks)
    assert "tags" in sig.parameters
    assert "tags_any" in sig.parameters
    assert "tags_none" in sig.parameters


def test_standalone_server_tasks_browse_accepts_tag_query_params():
    import inspect

    from oddish.server import browse_tasks as standalone_browse

    sig = inspect.signature(standalone_browse)
    for name in ("tags", "tags_any", "tags_none"):
        assert name in sig.parameters


# ---------------------------------------------------------------------------
# Author filter for the task browser (the github:/author:/user: qualifier)
# ---------------------------------------------------------------------------


def _compile_sql(clause) -> str:
    from sqlalchemy.dialects import postgresql

    return str(
        clause.compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    ).lower()


def test_browse_author_filter_none_when_empty():
    from oddish.core.endpoints import _build_browse_author_filter

    assert _build_browse_author_filter((), (), ()) is None


def test_browse_author_filter_matches_handle_case_insensitively():
    from oddish.core.endpoints import _build_browse_author_filter

    sql = _compile_sql(_build_browse_author_filter((), ("Bob",), ()))
    # Matches the existing index expressions exactly so the planner can use them:
    # idx_tasks_org_lower_github_tag_live on lower((tags ->> 'github_username'))
    # and idx_tasks_org_lower_user_live on lower("user").
    assert "lower((tasks.tags ->> 'github_username')) in ('bob')" in sql
    assert "lower(tasks.\"user\") in ('bob')" in sql


def test_browse_author_filter_matches_created_by_user_ids():
    from oddish.core.endpoints import _build_browse_author_filter

    sql = _compile_sql(_build_browse_author_filter(("user_a", "user_b"), (), ()))
    assert "created_by_user_id in ('user_a', 'user_b')" in sql


def test_browse_author_filter_matches_legacy_email_on_user():
    from oddish.core.endpoints import _build_browse_author_filter

    sql = _compile_sql(_build_browse_author_filter((), (), ("ada@x.com",)))
    # Emails only match the legacy ``user`` string, never the github tag.
    assert "lower(tasks.\"user\") in ('ada@x.com')" in sql
    assert "github_username" not in sql


def test_browse_author_filter_ors_all_identities():
    from oddish.core.endpoints import _build_browse_author_filter

    sql = _compile_sql(
        _build_browse_author_filter(("user_a",), ("bob",), ("ada@x.com",))
    )
    assert "created_by_user_id" in sql
    assert "github_username" in sql
    assert " or " in sql  # the three branches are OR'd
    # The legacy ``user`` match covers both the handle and the email.
    assert "bob" in sql and "ada@x.com" in sql


class _BrowseCaptureSession:
    """Drives ``browse_tasks_core`` to its empty-page result, returning tag rows
    for the resolver's text() query and an empty page for the paged select.
    Records statements so the page query can be compiled and asserted on.
    """

    def __init__(self, tag_rows=None):
        self.statements: list[object] = []
        self._tag_rows = tag_rows or []

    async def execute(self, statement, params=None):
        from sqlalchemy.sql.elements import TextClause

        self.statements.append(statement)
        if isinstance(statement, TextClause):

            class _R:
                def __init__(self_inner, rows):
                    self_inner.rows = rows

                def all(self_inner):
                    return self_inner.rows

            return _R(self._tag_rows)

        class _Mappings:
            def all(self_inner):
                return []

        class _Result:
            def mappings(self_inner):
                return _Mappings()

        return _Result()


def _page_query_text(session) -> str:
    from sqlalchemy.dialects import postgresql

    compiled = session.statements[-1].compile(dialect=postgresql.dialect())
    # The github_username JSON key + tag-id array are bind params, not inlined,
    # so fold the bound values into the searchable text.
    return (
        str(compiled).lower()
        + " "
        + " ".join(str(v) for v in compiled.params.values()).lower()
    )


def test_browse_author_filter_restricts_page_query():
    from oddish.core.endpoints import browse_tasks_core

    session = _BrowseCaptureSession()
    _run(
        browse_tasks_core(
            session,
            org_id="org-1",
            author_github_usernames=("bob",),
        )
    )
    page = _page_query_text(session)
    assert "github_username" in page  # author predicate applied to the page query
    assert "bob" in page


def test_browse_author_ands_with_tag():
    from oddish.core.endpoints import browse_tasks_core

    session = _BrowseCaptureSession(tag_rows=[("tag-flaky", "tag-flaky", "tag-flaky")])
    _run(
        browse_tasks_core(
            session,
            org_id="org-1",
            tags_all=["tag-flaky"],
            author_github_usernames=("bob",),
        )
    )
    page = _page_query_text(session)
    # Both predicates land on the same page query (ANDed via the ranked subquery).
    assert "github_username" in page and "bob" in page  # author
    assert "effective_tag_ids" in page and "tag-flaky" in page  # tag:
