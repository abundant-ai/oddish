"""Dashboard experiments tag filtering — predicates, empty-page rules, hydration."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


def test_experiment_tag_predicates_buckets():
    from oddish.core.dashboard import _experiment_tag_predicates
    from oddish.core.tag_filter_ast import ResolvedTagFilter

    resolved = ResolvedTagFilter(all_ids=["a", "b"], any_ids=["c"], none_ids=["d"])
    clauses = _experiment_tag_predicates(resolved)
    # AND bucket: one EXISTS per id; ANY: one over the set; NONE: one NOT EXISTS.
    assert len(clauses) == 4
    sql = " ".join(str(c) for c in clauses)
    assert sql.count("EXISTS") == 4
    assert "NOT EXISTS" in sql
    assert "tag_assignments" in sql
    assert "merged_into_id" in sql
    # Bind names must be unique across clauses or they collide when ANDed.
    import re

    names = re.findall(r":(\w+)", sql)
    assert len(names) == len(set(names))


def test_experiment_tag_predicates_empty_filter():
    from oddish.core.dashboard import _experiment_tag_predicates
    from oddish.core.tag_filter_ast import ResolvedTagFilter

    assert _experiment_tag_predicates(ResolvedTagFilter([], [], [])) == []


def test_direct_tags_for_targets_maps_rows():
    from oddish.core.tags_projection import list_direct_tags_for_targets

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
    from oddish.core.tags_projection import list_direct_tags_for_targets

    out = asyncio.run(list_direct_tags_for_targets(object(), scope="EXPERIMENT", target_ids=[]))
    assert out == {}


def test_user_tag_view_payload_shape():
    from oddish.core.dashboard import _user_tag_view_payload
    from oddish.core.tags_projection import UserTagView

    view = UserTagView(
        tag_id="tg9", key="urgent", value=None, color="blue",
        visibility="PUBLIC", current=True, older=False,
    )
    assert _user_tag_view_payload(view) == {
        "tag_id": "tg9", "key": "urgent", "value": None, "color": "blue",
        "visibility": "PUBLIC", "current": True, "older": False,
    }


def test_unknown_positive_token_returns_empty_page():
    from oddish.core.dashboard import _has_unknown_positive_tokens
    from oddish.core.tag_filter_ast import TagFilterAST

    # resolve_names_to_ids reports unknown RAW tokens; positive buckets gate.
    ast = TagFilterAST(all=["ghost"], any_=[], none=[])
    assert _has_unknown_positive_tokens(ast, unknown={"ghost"}) is True
    ast2 = TagFilterAST(all=[], any_=[], none=["ghost"])
    assert _has_unknown_positive_tokens(ast2, unknown={"ghost"}) is False
    ast3 = TagFilterAST(all=["real"], any_=["ghost"], none=[])
    assert _has_unknown_positive_tokens(ast3, unknown={"ghost"}) is True
    assert _has_unknown_positive_tokens(ast3, unknown=set()) is False
