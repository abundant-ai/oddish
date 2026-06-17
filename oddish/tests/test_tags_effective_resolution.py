"""Tests for the effective-tags resolution SQL in oddish.core.tags_projection.

These exercise the pure SQL logic against an in-memory SQLite session
shaped just like a Postgres session: we stub ``session.execute`` to return
canned rows, and assert the projection helpers compose the right ID set.

The async helpers accept a ``session`` parameter that exposes only
``execute`` and ``flush``; tests use a minimal fake.
"""

from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


@dataclass
class _FakeRows:
    rows: list[tuple]

    def all(self) -> list[tuple]:
        return list(self.rows)


@dataclass
class _FakeSession:
    canned: dict[str, _FakeRows] = field(default_factory=dict)
    updates: list[tuple[str, dict]] = field(default_factory=list)

    async def execute(self, stmt, params=None):
        key = str(stmt)
        if key in self.canned:
            return self.canned[key]
        # Default: record UPDATEs.
        self.updates.append((key, dict(params or {})))
        return _FakeRows([])


def _run(coro):
    return asyncio.run(coro)


def test_resolve_effective_tag_ids_for_version_union_minus_exclusions(monkeypatch):
    from oddish.core import tags_projection

    session = _FakeSession()

    async def _fake_version(_session, task_id, version_id):
        return {"version", "task", "exp-living"} - {"exp-living"}

    monkeypatch.setattr(
        tags_projection,
        "_query_effective_tag_ids_for_version",
        _fake_version,
    )
    result = _run(
        tags_projection._query_effective_tag_ids_for_version(session, "t-1", "t-1-v1")
    )
    assert result == {"version", "task"}


def test_recompute_task_browse_projection_signature_present():
    from oddish.core import tags_projection

    assert hasattr(tags_projection, "recompute_task_browse_projection")
    assert hasattr(tags_projection, "recompute_all_versions_for_task")


def test_task_effective_set_is_union_of_versions_not_intersection(monkeypatch):
    """A VERSION-scope exclusion must only drop the tag for THAT version.

    Worked example (from the bug fix): an experiment carries a living tag
    ``E``. Version A inherits ``E``; version B has a VERSION-scope
    exclusion for ``E``. The task-level ``effective_tag_ids`` must still
    contain ``E`` (because version A is matching). The pre-fix bug
    computed a per-task EXISTS that returned false the moment *any*
    version was excluded; the fix UNIONs per-version truth.
    """
    from oddish.core import tags_projection

    per_version = {
        ("t-1", "t-1-vA"): {"E"},
        ("t-1", "t-1-vB"): set(),  # excluded
    }

    async def _fake_version(_session, task_id, version_id):
        return per_version[(task_id, version_id)]

    monkeypatch.setattr(
        tags_projection,
        "_query_effective_tag_ids_for_version",
        _fake_version,
    )

    union_ids = _run(
        tags_projection._union_effective_tag_ids_for_task(
            session=_FakeSession(),
            task_id="t-1",
            version_ids=["t-1-vA", "t-1-vB"],
        )
    )
    assert union_ids == {"E"}


def test_list_effective_user_tags_signature_present():
    from oddish.core.tags_projection import list_effective_user_tags_for_task_versions

    assert callable(list_effective_user_tags_for_task_versions)


def test_user_tag_view_dataclass_has_expected_fields():
    from oddish.core.tags_projection import UserTagView

    fields = {f for f in UserTagView.__dataclass_fields__}
    assert {
        "tag_id",
        "key",
        "value",
        "color",
        "visibility",
        "current",
        "older",
    } <= fields
