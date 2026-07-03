"""Unit tests for the dashboard experiments-list author enrichment.

Covers ``api.routers.dashboard._enrich_experiment_authors``: the hosted layer
promotes canonical org-member names into the ``author`` field (from the
experiment's ``owner_user_id``) and the ``last_runner`` field (from the latest
trial's ``billed_user_id``, exposed as ``last_runner_user_id``) so the
dashboard columns match the cost page. No database is touched -- a tiny fake
session returns the batch-loaded ``UserModel`` rows.
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
    original_row = {
        "id": "e1",
        "owner_user_id": "user_1",
        "author": {"name": "charlesyhuang", "source": "github"},
    }
    dashboard = {"experiments": [original_row]}
    session = _FakeSession([_user(id="user_1", name="Charles Huang")])

    await _enrich_experiment_authors(session, dashboard)

    assert dashboard["experiments"][0]["author"] == {
        "name": "Charles Huang",
        "source": "member",
    }
    # The original row object (shared with the core's experiments cache) must
    # NOT be mutated: enrichment replaces rows with shallow copies so the
    # cached github/api fallback survives for later requests in the TTL.
    assert original_row["author"] == {"name": "charlesyhuang", "source": "github"}
    assert dashboard["experiments"][0] is not original_row


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


@pytest.mark.asyncio
async def test_enrich_overrides_last_runner_from_billed_user() -> None:
    """A resolvable ``last_runner_user_id`` replaces the task-based runner.

    Repro of the APPEND-to-shared-task bug: the task was created by Rishi, so
    the core's task-based ``last_runner`` says @RishiDesai, but the latest
    trial was billed to Charles -- the column must show Charles.
    """
    original_row = {
        "id": "e1",
        "owner_user_id": None,
        "last_runner_user_id": "user_charles",
        "author": {"name": "rishi@abundant.ai", "source": "api"},
        "last_runner": {"name": "RishiDesai", "source": "github"},
    }
    dashboard = {"experiments": [original_row]}
    session = _FakeSession([_user(id="user_charles", name="Charles Huang")])

    await _enrich_experiment_authors(session, dashboard)

    row = dashboard["experiments"][0]
    assert row["last_runner"] == {"name": "Charles Huang", "source": "member"}
    # Author had no owner id to resolve, so the core value stands.
    assert row["author"] == {"name": "rishi@abundant.ai", "source": "api"}
    # Cached row object untouched.
    assert original_row["last_runner"] == {"name": "RishiDesai", "source": "github"}
    assert row is not original_row


@pytest.mark.asyncio
async def test_enrich_leaves_last_runner_when_unresolvable() -> None:
    """NULL id (legacy trials) or a nameless user keeps the task-based runner."""
    dashboard = {
        "experiments": [
            {
                "id": "e1",
                "owner_user_id": None,
                "last_runner_user_id": None,
                "last_runner": {"name": "RishiDesai", "source": "github"},
            },
            {
                "id": "e2",
                "owner_user_id": None,
                "last_runner_user_id": "user_nameless",
                "last_runner": {"name": "someone@example.com", "source": "api"},
            },
        ]
    }
    session = _FakeSession([_user(id="user_nameless", name=None)])

    await _enrich_experiment_authors(session, dashboard)

    assert dashboard["experiments"][0]["last_runner"] == {
        "name": "RishiDesai",
        "source": "github",
    }
    assert dashboard["experiments"][1]["last_runner"] == {
        "name": "someone@example.com",
        "source": "api",
    }


@pytest.mark.asyncio
async def test_enrich_author_and_last_runner_different_members() -> None:
    """One row can get both overrides, each resolved to its own member."""
    original_row = {
        "id": "e1",
        "owner_user_id": "user_rishi",
        "last_runner_user_id": "user_charles",
        "author": {"name": "RishiDesai", "source": "github"},
        "last_runner": {"name": "RishiDesai", "source": "github"},
    }
    dashboard = {"experiments": [original_row]}
    session = _FakeSession(
        [
            _user(id="user_rishi", name="Rishi Desai", email="rishi@abundant.ai"),
            _user(id="user_charles", name="Charles Huang"),
        ]
    )

    await _enrich_experiment_authors(session, dashboard)

    row = dashboard["experiments"][0]
    assert row["author"] == {"name": "Rishi Desai", "source": "member"}
    assert row["last_runner"] == {"name": "Charles Huang", "source": "member"}
    # One shallow copy carries both overrides; the cached original is intact.
    assert original_row["author"] == {"name": "RishiDesai", "source": "github"}
    assert original_row["last_runner"] == {"name": "RishiDesai", "source": "github"}
