"""Tests for the /tags router DTOs."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


def test_tag_create_request_fields():
    from oddish.schemas import TagCreateRequest

    req = TagCreateRequest(key="flaky", visibility="PRIVATE")
    assert req.key == "flaky"
    assert req.visibility == "PRIVATE"


def test_tag_list_item_fields():
    from oddish.schemas import TagListItem

    item = TagListItem(
        id="t-1",
        key="flaky",
        value=None,
        color="#ff0",
        visibility="PRIVATE",
        state="ACTIVE",
        usage_count=3,
        row_version=1,
        owner_user_id="u-1",
    )
    assert item.usage_count == 3


def test_tag_assign_request_validates_scope():
    from oddish.schemas import TagAssignRequest
    import pytest

    TagAssignRequest(tag_id="t-1", scope="TASK", target_id="task-1")
    with pytest.raises(ValueError):
        TagAssignRequest(tag_id="t-1", scope="BOGUS", target_id="task-1")


def test_tag_filter_ast_dto_round_trip():
    from oddish.schemas import TagFilterASTDTO

    dto = TagFilterASTDTO(all=["a"], any_=["b"], none=["c"])
    assert dto.model_dump() == {"all": ["a"], "any_": ["b"], "none": ["c"]}


def test_saved_tag_filter_create_request_fields():
    from oddish.schemas import SavedTagFilterCreateRequest

    req = SavedTagFilterCreateRequest(
        name="My filter",
        filter_ast={"all": ["tag-1"], "any": [], "none": []},
        visibility="PRIVATE",
    )
    assert req.visibility == "PRIVATE"


def test_tags_router_routes_registered():
    import pytest

    tags_router = pytest.importorskip("backend.api.routers.tags")

    paths = {r.path for r in tags_router.router.routes}
    for path in {
        "/tags",
        "/tags/{tag_id}",
        "/tags/{tag_id}/merge",
        "/tags/{tag_id}/archive",
        "/tags/{tag_id}/grants",
        "/tags/assign",
        "/tags/unassign",
        "/tags/exclude",
        "/tags/unexclude",
        "/tag-policy",
        "/tag-filters",
        "/tag-filters/{filter_id}",
        "/tasks/{task_id}/tags",
        "/experiments/{experiment_id}/tags",
        "/tag-policy/profanity-reports",
    }:
        assert path in paths, f"missing route: {path}"
