"""Unit tests for the dashboard experiments-list author enrichment.

Covers ``api.routers.dashboard._enrich_experiment_authors``: the hosted layer
promotes an experiment owner's canonical org-member name into the ``author``
field so the dashboard Author column matches the cost page. No database is
touched -- a tiny fake session returns the batch-loaded ``UserModel`` rows.
"""

from __future__ import annotations

import pytest

from api.routers.dashboard import _enrich_experiment_authors
from models import UserModel


def _user(**overrides) -> UserModel:
    base = {
        "id": "user_1",
        "org_id": "org_1",
        "email": "charles@abundant.ai",
        "github_username": "charlesyhuang",
        "clerk_user_id": "user_clerk",
        "role": "member",
        "is_active": True,
    }
    base.update(overrides)
    return UserModel(**base)


class _FakeScalars:
    def __init__(self, users: list[UserModel]) -> None:
        self._users = users

    def scalars(self):
        return iter(self._users)


class _FakeSession:
    """Returns a fixed set of users for any ``execute`` (ignores the WHERE)."""

    def __init__(self, users: list[UserModel]) -> None:
        self._users = users
        self.calls = 0

    async def execute(self, _stmt):
        self.calls += 1
        return _FakeScalars(self._users)


@pytest.mark.asyncio
async def test_enrich_promotes_member_name() -> None:
    dashboard = {
        "experiments": [
            {
                "id": "e1",
                "owner_user_id": "user_1",
                "author": {"name": "charlesyhuang", "source": "github"},
            }
        ]
    }
    session = _FakeSession([_user(id="user_1", name="Charles Huang")])

    await _enrich_experiment_authors(session, dashboard)

    assert dashboard["experiments"][0]["author"] == {
        "name": "Charles Huang",
        "source": "member",
    }


@pytest.mark.asyncio
async def test_enrich_leaves_author_when_no_member_name() -> None:
    """An owner whose UserModel has no display name keeps the core author."""
    dashboard = {
        "experiments": [
            {
                "id": "e1",
                "owner_user_id": "user_1",
                "author": {"name": "charlesyhuang", "source": "github"},
            }
        ]
    }
    session = _FakeSession([_user(id="user_1", name=None)])

    await _enrich_experiment_authors(session, dashboard)

    assert dashboard["experiments"][0]["author"] == {
        "name": "charlesyhuang",
        "source": "github",
    }


@pytest.mark.asyncio
async def test_enrich_skips_rows_without_owner_id() -> None:
    """No owner id (incl. the None-d-out sentinel) -> never queries, no change."""
    dashboard = {
        "experiments": [
            {
                "id": "e1",
                "owner_user_id": None,
                "author": {"name": "someone@example.com", "source": "api"},
            }
        ]
    }
    session = _FakeSession([_user(id="user_1", name="Charles Huang")])

    await _enrich_experiment_authors(session, dashboard)

    assert session.calls == 0
    assert dashboard["experiments"][0]["author"] == {
        "name": "someone@example.com",
        "source": "api",
    }


@pytest.mark.asyncio
async def test_enrich_no_experiments_is_noop() -> None:
    dashboard: dict = {"experiments": []}
    session = _FakeSession([_user(id="user_1", name="Charles Huang")])

    await _enrich_experiment_authors(session, dashboard)

    assert session.calls == 0
    assert dashboard == {"experiments": []}
