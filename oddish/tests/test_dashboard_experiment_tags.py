"""Dashboard experiments tag filtering — predicates, empty-page rules, hydration."""

from __future__ import annotations

import asyncio
import sys
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


def test_experiment_tag_predicates_buckets():
    import re

    from oddish.core.dashboard import _experiment_tag_predicates
    from oddish.core.tags.filter_ast import ResolvedTagFilter

    # Two single-id groups (the value-less-tag / single-token shape) plus
    # any/none -- mirrors the pre-``all_groups`` flat-id behavior.
    resolved = ResolvedTagFilter(
        all_ids=["a", "b"],
        any_ids=["c"],
        none_ids=["d"],
        all_groups=[["a"], ["b"]],
    )
    clauses = _experiment_tag_predicates(resolved)
    # AND bucket: one (EXPERIMENT-scope OR member-task TASK-scope) match per
    # group -- each is two EXISTS ORed together; ANY: one over the set; NONE:
    # the negation of that same two-EXISTS match.
    assert len(clauses) == 4
    sql = " ".join(str(c) for c in clauses)
    assert sql.count("EXISTS") == 8
    assert re.search(r"NOT\s*\(", sql)
    assert "task_experiments" in sql
    assert "tag_assignments" in sql
    assert "merged_into_id" in sql
    # Bind names must be unique ACROSS clauses or they collide when ANDed.
    # Each clause's own name legitimately repeats within it now (used by
    # both the EXPERIMENT-scope and member-task TASK-scope EXISTS arms).
    names = re.findall(r":(\w+)", sql)
    assert len(set(names)) == len(clauses)


def test_experiment_tag_predicates_ors_within_a_bare_key_group():
    """A bare key's fanned-out ids land in ONE ``all_groups`` entry -- the
    EXISTS must match ANY of them (OR within the key), not require all of
    them (which would always be empty, the bug this branch is fixing)."""
    from oddish.core.dashboard import _experiment_tag_predicates
    from oddish.core.tags.filter_ast import ResolvedTagFilter

    resolved = ResolvedTagFilter(
        all_ids=["re", "opt"],
        any_ids=[],
        none_ids=[],
        all_groups=[["re", "opt"]],
    )
    clauses = _experiment_tag_predicates(resolved)
    assert len(clauses) == 1
    sql = str(clauses[0])
    assert "NOT (" not in sql
    assert "= ANY(" in sql


def test_experiment_tag_predicates_empty_filter():
    from oddish.core.dashboard import _experiment_tag_predicates
    from oddish.core.tags.filter_ast import ResolvedTagFilter

    assert _experiment_tag_predicates(ResolvedTagFilter([], [], [])) == []


def test_direct_tags_for_targets_maps_rows():
    from oddish.core.tags.projection import list_direct_tags_for_targets

    class _S:
        def __init__(self):
            self.params = None

        async def execute(self, stmt, params=None):
            self.params = params

            class _R:
                def all(self_inner):
                    return [
                        ("e1", "tg1", "infra", None, "red", "PUBLIC"),
                        ("e1", "tg2", "flaky", None, None, "PRIVATE"),
                        ("e2", "tg1", "infra", None, "red", "PUBLIC"),
                    ]

            return _R()

    s = _S()
    out = asyncio.run(
        list_direct_tags_for_targets(
            s, scope="EXPERIMENT", target_ids=["e1", "e2", "e3"]
        )
    )
    assert s.params == {"scope": "EXPERIMENT", "target_ids": ["e1", "e2", "e3"]}
    assert set(out) == {"e1", "e2"}
    assert [t.key for t in out["e1"]] == ["infra", "flaky"]
    first = out["e1"][0]
    assert (first.tag_id, first.color, first.visibility) == ("tg1", "red", "PUBLIC")
    assert first.current is True and first.older is False


def test_direct_tags_for_targets_empty_input():
    from oddish.core.tags.projection import list_direct_tags_for_targets

    out = asyncio.run(
        list_direct_tags_for_targets(object(), scope="EXPERIMENT", target_ids=[])
    )
    assert out == {}


def test_user_tag_view_payload_shape():
    from oddish.core.dashboard import _user_tag_view_payload
    from oddish.core.tags.projection import UserTagView

    view = UserTagView(
        tag_id="tg9",
        key="urgent",
        value=None,
        color="blue",
        visibility="PUBLIC",
        current=True,
        older=False,
    )
    assert _user_tag_view_payload(view) == {
        "tag_id": "tg9",
        "key": "urgent",
        "value": None,
        "color": "blue",
        "visibility": "PUBLIC",
        "current": True,
        "older": False,
    }


def test_attach_user_tags_to_task_payloads():
    from oddish.core.dashboard import _attach_user_tags_to_task_payloads
    from oddish.core.tags.projection import UserTagView

    payloads = [{"id": "t1", "user_tags": []}, {"id": "t2", "user_tags": []}]
    by_task = {
        "t1": [
            UserTagView(
                tag_id="tg9",
                key="urgent",
                value=None,
                color="blue",
                visibility="PUBLIC",
                current=False,
                older=True,
            )
        ]
    }
    _attach_user_tags_to_task_payloads(payloads, by_task)
    assert payloads[0]["user_tags"] == [
        {
            "tag_id": "tg9",
            "key": "urgent",
            "value": None,
            "color": "blue",
            "visibility": "PUBLIC",
            "current": False,
            "older": True,
        }
    ]
    assert payloads[1]["user_tags"] == []


def test_unknown_positive_token_returns_empty_page():
    from oddish.core.dashboard import _has_unknown_positive_tokens
    from oddish.core.tags.filter_ast import TagFilterAST

    # resolve_names_to_ids reports unknown RAW tokens; positive buckets gate.
    ast = TagFilterAST(all=["ghost"], any_=[], none=[])
    assert _has_unknown_positive_tokens(ast, unknown={"ghost"}) is True
    ast2 = TagFilterAST(all=[], any_=[], none=["ghost"])
    assert _has_unknown_positive_tokens(ast2, unknown={"ghost"}) is False
    ast3 = TagFilterAST(all=["real"], any_=["ghost"], none=[])
    assert _has_unknown_positive_tokens(ast3, unknown={"ghost"}) is True
    assert _has_unknown_positive_tokens(ast3, unknown=set()) is False


# ---------------------------------------------------------------------------
# DB-backed: bare-key OR-within-group semantics on the experiments page.
# Mirrors the tasks-page fix (test_browse_tag_filters.py) -- same bug class,
# different consumer of ``ResolvedTagFilter`` (_experiment_tag_predicates
# AND-chains raw ids instead of using ``all_groups``).
# ---------------------------------------------------------------------------


def _org() -> str:
    return f"exptagreg-{uuid.uuid4().hex[:8]}"


async def _make_experiment(session, *, org_id: str, name: str):
    from oddish.db.models import ExperimentModel

    experiment = ExperimentModel(name=name, org_id=org_id)
    session.add(experiment)
    await session.flush()
    return experiment


async def _tag_experiment(session, *, tag_id: str, experiment_id: str, org_id: str):
    from oddish.core.tags.service import assign_tag_core

    await assign_tag_core(
        session,
        tag_id=tag_id,
        scope="EXPERIMENT",
        target_id=experiment_id,
        task_id=None,
        org_id=org_id,
        actor_user_id=None,
        sync_projection=False,
    )


@pytest.mark.asyncio
async def test_dashboard_experiments_bare_key_matches_any_experiment_under_key(
    session,
):
    """THE MOTIVATING BUG: ``experiments_tags="category"`` (a bare key) must
    return every experiment tagged under ANY value of that key, not zero --
    the old AND-of-all-ids reading required an experiment to hold every
    category value simultaneously, which silently emptied the page."""
    from oddish.core.dashboard import load_dashboard_experiments
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

    re_exp = await _make_experiment(session, org_id=org_id, name=f"re-{org_id}")
    opt_exp = await _make_experiment(session, org_id=org_id, name=f"opt-{org_id}")
    await _tag_experiment(
        session, tag_id=re_id, experiment_id=re_exp.id, org_id=org_id
    )
    await _tag_experiment(
        session, tag_id=opt_id, experiment_id=opt_exp.id, org_id=org_id
    )
    await session.flush()

    rows, _ = await load_dashboard_experiments(
        session,
        org_id=org_id,
        experiments_limit=50,
        experiments_offset=0,
        experiments_query=None,
        experiments_status="",
        experiments_tags="category",
    )
    ids = {row["id"] for row in rows}
    assert ids == {re_exp.id, opt_exp.id}


@pytest.mark.asyncio
async def test_dashboard_experiments_all_across_different_keys_still_ands(session):
    """OR-within-a-key must not weaken cross-key AND: an experiment must have
    SOME category AND SOME topic to match ``experiments_tags="category,topic"``,
    even though either key alone is now an OR across its fanned-out values."""
    from oddish.core.dashboard import load_dashboard_experiments
    from oddish.core.tags.service import create_tag_core

    org_id = _org()
    category_id = await create_tag_core(
        session,
        key="category",
        value="reverse-engineering",
        org_id=org_id,
        actor_user_id=None,
        policy={},
        is_admin=True,
    )
    topic_id = await create_tag_core(
        session,
        key="topic",
        value="compilers",
        org_id=org_id,
        actor_user_id=None,
        policy={},
        is_admin=True,
    )
    await session.flush()

    both = await _make_experiment(session, org_id=org_id, name=f"both-{org_id}")
    category_only = await _make_experiment(
        session, org_id=org_id, name=f"cat-only-{org_id}"
    )
    topic_only = await _make_experiment(
        session, org_id=org_id, name=f"topic-only-{org_id}"
    )
    await _tag_experiment(session, tag_id=category_id, experiment_id=both.id, org_id=org_id)
    await _tag_experiment(session, tag_id=topic_id, experiment_id=both.id, org_id=org_id)
    await _tag_experiment(
        session, tag_id=category_id, experiment_id=category_only.id, org_id=org_id
    )
    await _tag_experiment(
        session, tag_id=topic_id, experiment_id=topic_only.id, org_id=org_id
    )
    await session.flush()

    rows, _ = await load_dashboard_experiments(
        session,
        org_id=org_id,
        experiments_limit=50,
        experiments_offset=0,
        experiments_query=None,
        experiments_status="",
        experiments_tags="category,topic",
    )
    ids = {row["id"] for row in rows}
    assert ids == {both.id}


@pytest.mark.asyncio
async def test_dashboard_experiments_value_less_tag_regression(session):
    """Regression guard: a bare-key token for a plain (value-less) tag --
    the pre-derived-tags shape, where a key always resolved to exactly one
    id -- must keep matching exactly that tag, unchanged."""
    from oddish.core.dashboard import load_dashboard_experiments
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

    flaky_exp = await _make_experiment(session, org_id=org_id, name=f"flaky-{org_id}")
    other_exp = await _make_experiment(session, org_id=org_id, name=f"other-{org_id}")
    await _tag_experiment(
        session, tag_id=flaky_id, experiment_id=flaky_exp.id, org_id=org_id
    )
    await session.flush()

    rows, _ = await load_dashboard_experiments(
        session,
        org_id=org_id,
        experiments_limit=50,
        experiments_offset=0,
        experiments_query=None,
        experiments_status="",
        experiments_tags="Flaky Trial",
    )
    ids = {row["id"] for row in rows}
    assert ids == {flaky_exp.id}


# ---------------------------------------------------------------------------
# Finding 1: derived tags (category:*/topic:*/org:*) live only at TASK scope
# (see core/tags/derived.py) but the shared /tags dropdown offers them on the
# experiments page too. Filtering an experiment by such a tag must reach
# through task_experiments to the member task's TASK-scope assignment,
# instead of silently returning zero rows for a tag the dropdown just
# offered as real.
# ---------------------------------------------------------------------------


async def _make_task(session, *, org_id: str, name: str):
    from oddish.db.models import TaskModel

    task = TaskModel(name=name, org_id=org_id, user="tester", task_path=f"s3://tasks/{name}")
    session.add(task)
    await session.flush()
    return task


async def _link_task_to_experiment(session, *, task_id: str, experiment_id: str):
    from oddish.db.models import task_experiments

    await session.execute(
        task_experiments.insert().values(task_id=task_id, experiment_id=experiment_id)
    )
    await session.flush()


async def _tag_task_derived(session, *, tag_id: str, task_id: str, org_id: str):
    from oddish.core.tags.service import assign_tag_core

    await assign_tag_core(
        session,
        tag_id=tag_id,
        scope="TASK",
        target_id=task_id,
        task_id=task_id,
        org_id=org_id,
        actor_user_id=None,
        source="DERIVED",
        sync_projection=False,
    )


@pytest.mark.asyncio
async def test_dashboard_experiments_match_via_member_task_derived_tag(session):
    """THE MOTIVATING BUG: a derived tag (e.g. ``category:reverse-engineering``)
    is only ever assigned at TASK scope, never EXPERIMENT scope, yet the
    shared tag dropdown offers it on the experiments page too. Filtering
    experiments by it must find experiments whose member tasks carry it --
    not silently return zero rows for a tag the dropdown just offered as
    real and resolvable."""
    from oddish.core.dashboard import load_dashboard_experiments
    from oddish.core.tags.service import create_tag_core

    org_id = _org()
    category_id = await create_tag_core(
        session,
        key="category",
        value="reverse-engineering",
        org_id=org_id,
        actor_user_id=None,
        policy={},
        is_admin=True,
    )
    await session.flush()

    task = await _make_task(session, org_id=org_id, name=f"re-task-{org_id}")
    await _tag_task_derived(session, tag_id=category_id, task_id=task.id, org_id=org_id)

    matching_exp = await _make_experiment(session, org_id=org_id, name=f"re-exp-{org_id}")
    other_exp = await _make_experiment(session, org_id=org_id, name=f"other-exp-{org_id}")
    await _link_task_to_experiment(
        session, task_id=task.id, experiment_id=matching_exp.id
    )
    await session.flush()

    rows, _ = await load_dashboard_experiments(
        session,
        org_id=org_id,
        experiments_limit=50,
        experiments_offset=0,
        experiments_query=None,
        experiments_status="",
        experiments_tags="category:reverse-engineering",
    )
    ids = {row["id"] for row in rows}
    assert matching_exp.id in ids
    assert other_exp.id not in ids


@pytest.mark.asyncio
async def test_dashboard_experiments_freetext_matches_via_member_task_derived_tag(
    session,
):
    """Same gap as the ``tag:`` filter fix above, but in the free-text search
    box: ``_experiment_freetext_match``'s own tag matching was still scoped
    to EXPERIMENT-scope assignments only, so a bare word matching a derived
    tag's key (assigned only at TASK scope) silently returned nothing."""
    from oddish.core.dashboard import load_dashboard_experiments
    from oddish.core.tags.service import create_tag_core

    org_id = _org()
    category_id = await create_tag_core(
        session,
        key="category",
        value="reverse-engineering",
        org_id=org_id,
        actor_user_id=None,
        policy={},
        is_admin=True,
    )
    await session.flush()

    task = await _make_task(session, org_id=org_id, name=f"re-task-ft-{org_id}")
    await _tag_task_derived(session, tag_id=category_id, task_id=task.id, org_id=org_id)

    matching_exp = await _make_experiment(session, org_id=org_id, name=f"re-exp-ft-{org_id}")
    other_exp = await _make_experiment(session, org_id=org_id, name=f"other-exp-ft-{org_id}")
    await _link_task_to_experiment(
        session, task_id=task.id, experiment_id=matching_exp.id
    )
    await session.flush()

    rows, _ = await load_dashboard_experiments(
        session,
        org_id=org_id,
        experiments_limit=50,
        experiments_offset=0,
        experiments_query="category",
        experiments_status="",
    )
    ids = {row["id"] for row in rows}
    assert matching_exp.id in ids
    assert other_exp.id not in ids
