"""Saved tag-filter CRUD tests."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


def _run(coro):
    return asyncio.run(coro)


class _StubResult:
    def __init__(self, rows=None, scalar=None):
        self._rows = rows or []
        self._scalar = scalar

    def all(self):
        return self._rows

    def scalar(self):
        return self._scalar

    @property
    def rowcount(self):
        return len(self._rows)


class _FakeSession:
    def __init__(self, rows=None, scalar=None):
        self._rows = rows or []
        self._scalar = scalar
        self.executed = []

    async def execute(self, stmt, params=None):
        self.executed.append((str(stmt), dict(params or {})))
        return _StubResult(rows=self._rows, scalar=self._scalar)

    async def scalar(self, stmt, params=None):
        return self._scalar

    async def flush(self):
        pass


def test_create_saved_filter_inserts_row():
    from oddish.core.tags.saved_filters import create_saved_tag_filter_core

    s = _FakeSession()
    _run(
        create_saved_tag_filter_core(
            s,
            org_id="org-1",
            owner_user_id="u-1",
            name="My flaky filter",
            filter_ast={"all": ["tag-1"], "any": ["tag-2"], "none": []},
            visibility="PRIVATE",
        )
    )
    assert any("INSERT INTO saved_tag_filters" in q for q, _ in s.executed)


def test_list_saved_filters_returns_owner_and_org_visible():
    from oddish.core.tags.saved_filters import list_saved_tag_filters_core

    rows = [
        ("f-1", "Mine", '{"all":[],"any":[],"none":[]}', "PRIVATE", "u-1"),
        ("f-2", "Shared", '{"all":[],"any":[],"none":[]}', "ORG", "u-2"),
    ]
    s = _FakeSession(rows=rows)
    items = _run(list_saved_tag_filters_core(s, org_id="org-1", actor_user_id="u-1"))
    assert len(items) == 2
