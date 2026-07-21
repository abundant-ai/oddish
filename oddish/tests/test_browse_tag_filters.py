"""Tests for the browse-filter AST and name resolution."""

from __future__ import annotations

import asyncio
import sys
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


def _run(coro):
    return asyncio.run(coro)


class _NameResolveSession:
    """Fakes the tags lookup; rows are (id, normalized_key, normalized_value, resolved_id)."""

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
            (tag_id, name, None, tag_id)
            for name, tag_id in self.mapping.items()
            if name in wanted_names or tag_id in wanted_ids
        ]
        return _R(rows)


def test_tag_filter_ast_default_empty():
    from oddish.core.tags.filter_ast import TagFilterAST

    f = TagFilterAST()
    assert f.is_empty() is True


def test_tag_filter_ast_normalizes_to_lower():
    from oddish.core.tags.filter_ast import TagFilterAST

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
    from oddish.core.tags.filter_ast import TagFilterAST
    from oddish.core.tags.naming import normalize_tag_key

    f = TagFilterAST(all=["  Flaky Trial  "], any_=["SWE Bench v2.0"])
    assert f.normalized_all == [normalize_tag_key("  Flaky Trial  ")] == ["flaky-trial"]
    assert (
        f.normalized_any == [normalize_tag_key("SWE Bench v2.0")] == ["swe-bench-v2.0"]
    )


def test_resolve_names_to_ids_returns_ids_and_unknown(monkeypatch):
    from oddish.core.tags.filter_ast import (
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
    from oddish.core.tags.filter_ast import (
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
    from oddish.core.tags.filter_ast import (
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
    from oddish.core.tags.filter_ast import (
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
    import importlib

    from oddish.core.endpoints import browse_tasks_core
    from oddish.schemas import UserTagRef

    # ``browse_tasks_core`` now lives in a submodule of the ``endpoints``
    # package; assert the name is in scope wherever it is actually defined.
    module = importlib.import_module(browse_tasks_core.__module__)
    assert getattr(module, "UserTagRef", None) is UserTagRef


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


def test_browse_freetext_matches_name_author_and_tag():
    from oddish.core.endpoints import _task_freetext_match

    sql = _compile_sql(_task_freetext_match("alice"))
    assert "tasks.name ilike" in sql
    assert 'tasks."user" ilike' in sql  # legacy author string
    assert "github_username" in sql  # github_username tag
    # tag-name branch over the row's effective_tag_ids
    assert "effective_tag_ids" in sql and "tags.key ilike" in sql
    assert "alice" in sql
    # User-typed wildcards stay literal (escape_like + ESCAPE clause).
    assert "escape" in sql


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

    session = _BrowseCaptureSession(
        tag_rows=[("tag-flaky", "tag-flaky", None, "tag-flaky")]
    )
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


# ---------------------------------------------------------------------------
# key:value token resolution -- derived tags (core/tags/derived.py) all share
# one normalized_key per category ("category", "topic", "org"), with the
# discriminator only in normalized_value. resolve_names_to_ids must split a
# ``key:value`` token (the exact form the dashboard picker emits, see
# tag-filter-dropdown.tsx's tagToken) on the first ':' and match both halves,
# and a bare token (no ':') must still resolve as it always did.
# ---------------------------------------------------------------------------


def _org() -> str:
    return f"tagreg-{uuid.uuid4().hex[:8]}"


async def _make_task_with_version(session, *, org_id: str, name: str):
    from oddish.db.models import TaskModel, TaskVersionModel

    task = TaskModel(
        name=name, org_id=org_id, user="tester", task_path=f"s3://tasks/{name}"
    )
    session.add(task)
    await session.flush()
    version = TaskVersionModel(
        id=f"{task.id}-v1", task_id=task.id, version=1, task_path=task.task_path
    )
    session.add(version)
    await session.flush()
    task.current_version_id = version.id
    await session.flush()
    return task


@pytest.mark.asyncio
async def test_browse_tasks_core_filters_by_derived_category_token(session):
    """THE MOTIVATING QUERY: derive `category:reverse-engineering` from
    task.toml, then filter through browse_tasks_core with the exact token
    the dashboard picker emits. This is the read path the whole feature —
    filtering the dashboard by derived category/topic/org tags — depends on.
    """
    from oddish.core.endpoints import browse_tasks_core
    from oddish.core.tags.derived import rebuild_derived_tags
    from oddish.schemas import TaskMetadata

    org_id = _org()
    match = await _make_task_with_version(session, org_id=org_id, name=f"re-{org_id}")
    other = await _make_task_with_version(session, org_id=org_id, name=f"opt-{org_id}")

    await rebuild_derived_tags(
        session,
        task_id=match.id,
        org_id=org_id,
        metadata=TaskMetadata(category="Reverse Engineering"),
    )
    await rebuild_derived_tags(
        session,
        task_id=other.id,
        org_id=org_id,
        metadata=TaskMetadata(category="optimization"),
    )
    await session.flush()

    resp = await browse_tasks_core(
        session, org_id=org_id, tags_all=["category:reverse-engineering"]
    )
    ids = {item.id for item in resp.items}
    assert match.id in ids
    assert other.id not in ids


@pytest.mark.asyncio
async def test_resolve_key_value_token_matches_exact_pair(session):
    """A key:value token resolves to the ONE tag with that exact pair, even
    when several tags share the key -- the fix for the bug where every
    category token collided on normalized_key alone.
    """
    from oddish.core.tags.filter_ast import TagFilterAST, resolve_names_to_ids
    from oddish.core.tags.service import create_tag_core

    org_id = _org()
    re_id = await create_tag_core(
        session,
        key="category",
        value="reverse-engineering",
        org_id=org_id,
        actor_user_id=None,
        policy={},
        is_admin=True,
    )
    opt_id = await create_tag_core(
        session,
        key="category",
        value="optimization",
        org_id=org_id,
        actor_user_id=None,
        policy={},
        is_admin=True,
    )
    await session.flush()

    resolved, unknown = await resolve_names_to_ids(
        session,
        org_id=org_id,
        ast=TagFilterAST(all=["category:reverse-engineering"]),
    )
    assert resolved.all_ids == [re_id]
    assert opt_id not in resolved.all_ids
    assert unknown == set()


@pytest.mark.asyncio
async def test_resolve_bare_token_still_matches_value_less_tag(session):
    """Regression guard: a bare token with no ':' must keep resolving a
    plain (value-less) tag exactly as it did before key:value support.
    """
    from oddish.core.tags.filter_ast import TagFilterAST, resolve_names_to_ids
    from oddish.core.tags.service import create_tag_core

    org_id = _org()
    flaky_id = await create_tag_core(
        session,
        key="Flaky Trial",
        value=None,
        org_id=org_id,
        actor_user_id=None,
        policy={},
        is_admin=True,
    )
    await session.flush()

    resolved, unknown = await resolve_names_to_ids(
        session, org_id=org_id, ast=TagFilterAST(all=["Flaky Trial"])
    )
    assert resolved.all_ids == [flaky_id]
    assert unknown == set()


@pytest.mark.asyncio
async def test_resolve_bare_token_matches_every_tag_sharing_the_key(session):
    """Decided semantics: when a key fans out into several value tags (every
    derived category), a bare key token ("category") matches ALL of them --
    "show me everything categorized" -- rather than an arbitrary one (the
    pre-fix flat-dict by_name bug, last-row-wins).
    """
    from oddish.core.tags.filter_ast import TagFilterAST, resolve_names_to_ids
    from oddish.core.tags.service import create_tag_core

    org_id = _org()
    re_id = await create_tag_core(
        session,
        key="category",
        value="reverse-engineering",
        org_id=org_id,
        actor_user_id=None,
        policy={},
        is_admin=True,
    )
    opt_id = await create_tag_core(
        session,
        key="category",
        value="optimization",
        org_id=org_id,
        actor_user_id=None,
        policy={},
        is_admin=True,
    )
    await session.flush()

    resolved, unknown = await resolve_names_to_ids(
        session, org_id=org_id, ast=TagFilterAST(any_=["category"])
    )
    assert set(resolved.any_ids) == {re_id, opt_id}
    assert unknown == set()


@pytest.mark.asyncio
async def test_ml_training_underscore_and_hyphen_collapse_to_same_tag(session):
    """Spec test that was never written: 'ml_training' and 'ml-training'
    resolve to the SAME tag id -- the slug collapse (slugify in
    core/task_metadata.py) happens at the tag level via derived_tag_pairs,
    not just inside the slugify() helper in isolation.
    """
    from oddish.core.tags.derived import rebuild_derived_tags
    from oddish.db.models import TagAssignmentModel, TagAssignmentSource
    from oddish.schemas import TaskMetadata
    from sqlalchemy import select

    org_id = _org()
    task_a = await _make_task_with_version(session, org_id=org_id, name=f"a-{org_id}")
    task_b = await _make_task_with_version(session, org_id=org_id, name=f"b-{org_id}")

    await rebuild_derived_tags(
        session,
        task_id=task_a.id,
        org_id=org_id,
        metadata=TaskMetadata(category="ml_training"),
    )
    await rebuild_derived_tags(
        session,
        task_id=task_b.id,
        org_id=org_id,
        metadata=TaskMetadata(category="ml-training"),
    )
    await session.flush()

    tag_id_a = (
        await session.execute(
            select(TagAssignmentModel.tag_id).where(
                TagAssignmentModel.target_id == task_a.id,
                TagAssignmentModel.source == TagAssignmentSource.DERIVED,
            )
        )
    ).scalar_one()
    tag_id_b = (
        await session.execute(
            select(TagAssignmentModel.tag_id).where(
                TagAssignmentModel.target_id == task_b.id,
                TagAssignmentModel.source == TagAssignmentSource.DERIVED,
            )
        )
    ).scalar_one()
    assert tag_id_a == tag_id_b
